"""Order-bound buyer QR delivery. The buyer's official page collects its login ticket."""
import uuid
from urllib.parse import urlencode
from fastapi import HTTPException
from sqlalchemy import select, update, case
from .db import TerminalState
from .models import Activation, AuthorizationTask, Order, QRSession, Lease

CONFIRMED = ('OWNER_CONFIRMED', 'BUYER_REPORTED_LOGIN')


def view(task, now):
    return {'id': task.id, 'status': task.status, 'expire_at': task.expire_at,
            'remaining_seconds': max(0, task.expire_at - now),
            'member_confirmation_recorded': task.status in CONFIRMED,
            'buyer_reported_login': task.status == 'BUYER_REPORTED_LOGIN',
            'buyer_login_verified': False, 'remote_session_revocation_supported': False}


def snapshot(service, db, activation):
    task = db.scalar(select(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id,
        AuthorizationTask.provider == 'quark_reference').order_by(
            case((AuthorizationTask.status.in_(('WAIT_USER', *CONFIRMED)), 0), else_=1),
            AuthorizationTask.created_at.desc(), AuthorizationTask.id.desc()))
    if not task:
        return None
    if task.status == 'WAIT_USER' and service.clock() >= task.expire_at:
        task.status, task.credentials_encrypted = 'EXPIRED', None
    return view(task, service.clock())


def submit(service, db, activation, raw):
    token = service.providers['quark_reference']._token(raw)
    if activation.status != 'pending':
        raise HTTPException(409, 'activation_already_completed')
    # Normalize the login identity: changing optional URL parameters cannot replay the QR across orders.
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'leasegate:quark:532:' + token))
    existing = db.get(AuthorizationTask, task_id)
    if existing:
        if existing.activation_id != activation.id:
            raise HTTPException(409, 'quark_qr_already_assigned')
        if existing.status != 'WAIT_USER' or service.clock() >= existing.expire_at:
            raise HTTPException(410, 'quark_qr_unavailable')
        return view(existing, service.clock())
    now = service.clock()
    db.execute(update(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id,
        AuthorizationTask.status.in_(['CREATED', 'WAIT_USER'])).values(status='CANCELLED', credentials_encrypted=None))
    db.execute(update(QRSession).where(QRSession.activation_id == activation.id,
        QRSession.status.in_(['CREATED', 'SCANNED'])).values(status='CANCELLED'))
    canonical = 'https://su.quark.cn/4_eMHBJ?' + urlencode({'token': token, 'client_id': '532', 'ssb': 'weblogin'})
    task = AuthorizationTask(id=task_id, order_id=activation.order_id, activation_id=activation.id,
        provider='quark_reference', status='WAIT_USER', created_at=now,
        expire_at=min(activation.expire_at, now + service.settings.qr_ttl),
        credentials_encrypted=service.vault.encrypt({'raw': canonical}, 'task:' + task_id))
    db.add(task)
    service.audit(db, 'quark.buyer_qr_submitted', task_id, 'buyer')
    return view(task, now)


def locked_task(service, db, task_id):
    initial = db.get(AuthorizationTask, task_id)
    if not initial or initial.provider != 'quark_reference':
        raise HTTPException(404, 'quark_request_not_found')
    activation = db.scalar(select(Activation).where(Activation.id == initial.activation_id).with_for_update())
    task = db.scalar(select(AuthorizationTask).where(AuthorizationTask.id == task_id).with_for_update())
    if task.status == 'WAIT_USER' and service.clock() >= task.expire_at:
        task.status, task.credentials_encrypted = 'EXPIRED', None
        raise TerminalState(410, 'quark_qr_unavailable')
    return task, activation


def qr_payload(service, db, task_id):
    task, _ = locked_task(service, db, task_id)
    if task.status != 'WAIT_USER' or not task.credentials_encrypted:
        raise HTTPException(410, 'quark_qr_unavailable')
    return service.vault.decrypt(task.credentials_encrypted, 'task:' + task.id)['raw']


def member_confirmed(service, db, task_id):
    task, activation = locked_task(service, db, task_id)
    if task.status in CONFIRMED:
        lease = db.scalar(select(Lease).where(Lease.order_id == task.order_id))
        service.active_lease(db, lease.id)
        return lease
    if task.status != 'WAIT_USER' or activation.status != 'pending':
        raise HTTPException(409, 'quark_request_not_pending')
    order = db.scalar(select(Order).where(Order.id == task.order_id).with_for_update())
    # This lease controls this site's order service, never a Quark browser session.
    lease = service.new_lease(db, order, activation, 'local')
    task.status, task.credentials_encrypted, task.expire_at = 'OWNER_CONFIRMED', None, lease.expire_at
    service.audit(db, 'quark.member_confirmation_recorded', task.id, 'admin',
                  buyer_login_verified=False, external_session_revocable=False)
    return lease


def owned_task(service, db, activation, task_id):
    task = db.get(AuthorizationTask, task_id)
    if not task or task.provider != 'quark_reference' or task.activation_id != activation.id:
        raise HTTPException(404, 'quark_request_not_found')
    return task


def cancel(service, db, activation, task_id):
    task = owned_task(service, db, activation, task_id)
    if task.status != 'WAIT_USER':
        raise HTTPException(409, 'quark_request_not_pending')
    task.status, task.credentials_encrypted = 'CANCELLED', None
    service.audit(db, 'quark.request_cancelled', task.id, 'buyer')
    return view(task, service.clock())


def report_login(service, db, activation, task_id):
    task = owned_task(service, db, activation, task_id)
    if task.status not in CONFIRMED:
        raise HTTPException(409, 'quark_member_confirmation_required')
    lease = db.scalar(select(Lease).where(Lease.order_id == task.order_id))
    service.active_lease(db, lease.id)
    if task.status != 'BUYER_REPORTED_LOGIN':
        task.status = 'BUYER_REPORTED_LOGIN'
        service.audit(db, 'quark.buyer_login_reported', task.id, 'buyer', buyer_login_verified=False)
    return view(task, service.clock())
