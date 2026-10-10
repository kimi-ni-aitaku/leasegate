import json
import logging
import smtplib
import time
from email.message import EmailMessage
import httpx
import redis
from sqlalchemy import select
from fastapi import HTTPException
from .config import load_settings
from .db import Database
from .models import Delivery, Order, Lease, Activation, AuthorizationTask, ProviderRevocation
from .security import sign
from .service import Service

logger = logging.getLogger('leasegate.worker')


def send_delivery(service, payload, delivery_id):
    settings = service.settings
    if settings.delivery_url:
        body = json.dumps({**payload, 'delivery_id': delivery_id}, separators=(',', ':'))
        timestamp = str(service.clock())
        signature = sign(settings.delivery_secret, timestamp + '.' + body)
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(settings.delivery_url, content=body,
                                   headers={'Content-Type': 'application/json', 'X-Delivery-Timestamp': timestamp,
                                            'X-Delivery-Signature': signature, 'Idempotency-Key': delivery_id})
            response.raise_for_status()
    elif settings.smtp_host:
        # buyer_id is an email address only for this transport; callback can resolve platform buyer IDs.
        if '@' not in payload['buyer_id'] or '\n' in payload['buyer_id'] or '\r' in payload['buyer_id']:
            raise ValueError('invalid_delivery_recipient')
        message = EmailMessage()
        message['Subject'], message['From'], message['To'] = '订单已付款：激活你的服务', settings.smtp_from, payload['buyer_id']
        message['Message-ID'] = f'<{delivery_id}@leasegate>'
        message.set_content(f'卡密：{payload["code"]}\n激活链接：{payload["activation_url"]}\n请勿转发此一次性凭据。')
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    else:
        return False  # dev mailbox, consumed by the demo order endpoint
    return True


def dispatch_deliveries(service):
    with service.database.transaction() as db:
        messages = db.scalars(select(Delivery).where(Delivery.status == 'pending', Delivery.next_attempt_at <= service.clock())
                             .with_for_update(skip_locked=not service.database.sqlite).limit(10)).all()
        for message in messages:
            message.attempts += 1
            cdk_expiry = db.get(Order, message.order_id).created_at + service.settings.cdk_ttl
            if service.clock() >= cdk_expiry:
                message.status, message.payload_encrypted = 'expired', None
                service.audit(db, 'delivery.expired', message.id)
                continue
            try:
                payload = service.vault.decrypt(message.payload_encrypted, 'delivery:' + message.order_id)
                if send_delivery(service, payload, message.id):
                    message.status, message.sent_at, message.payload_encrypted = 'sent', service.clock(), None
                    service.audit(db, 'delivery.sent', message.id)
                else:
                    message.next_attempt_at = service.clock() + 30
            except Exception:
                # Deliberately omit exception strings, response bodies and credentials.
                message.next_attempt_at = service.clock() + min(3600, 2 ** min(message.attempts, 11))
                service.audit(db, 'delivery.retry', message.id, attempt=message.attempts)


def process_oauth(service):
    with service.database.transaction() as db:
        task_ids = db.execute(select(AuthorizationTask.id, AuthorizationTask.activation_id).where(AuthorizationTask.provider == 'oauth',
                          AuthorizationTask.status.in_(['WAIT_USER', 'AUTHORIZED']), AuthorizationTask.next_poll_at <= service.clock())
                          .limit(10)).all()
    for task_id, activation_id in task_ids:
        with service.database.transaction() as db:
            activation = db.scalar(select(Activation).where(Activation.id == activation_id).with_for_update())
            task = db.scalar(select(AuthorizationTask).where(AuthorizationTask.id == task_id).with_for_update())
            if task.status not in ('WAIT_USER', 'AUTHORIZED') or task.next_poll_at > service.clock():
                continue
            if service.clock() >= task.expire_at and task.status == 'WAIT_USER':
                task.status, task.credentials_encrypted = 'EXPIRED', None
                continue
            try:
                credentials = service.vault.decrypt(task.credentials_encrypted, 'task:' + task.id)
                if task.status == 'WAIT_USER':
                    result = service.providers['oauth'].get_authorization_status(credentials)
                    error = result.get('error')
                    task.next_poll_at = service.clock() + task.poll_interval
                    if error == 'authorization_pending':
                        continue
                    if error == 'slow_down':
                        task.poll_interval += 5
                        task.next_poll_at = service.clock() + task.poll_interval
                        continue
                    if error:
                        task.status = 'EXPIRED' if error == 'expired_token' else 'FAILED'
                        task.error_code = 'provider_authorization_denied'
                        task.credentials_encrypted = None
                        continue
                    credentials = service.providers['oauth'].exchange_authorization(result)
                    task.status, task.credentials_encrypted = 'AUTHORIZED', service.vault.encrypt(credentials, 'task:' + task.id)
                order = db.scalar(select(Order).where(Order.id == task.order_id).with_for_update())
                lease = service.new_lease(db, order, activation, 'oauth', credentials)
                task.status, task.credentials_encrypted = 'EXCHANGED', None
                service.audit(db, 'provider.exchanged', task.id, provider='oauth')
            except HTTPException as exc:
                if exc.status_code == 409:
                    task.next_poll_at = service.clock() + 5
                else:
                    task.status, task.error_code = 'FAILED', 'provider_configuration_error'
            except Exception:
                task.next_poll_at = service.clock() + max(5, task.poll_interval)
                task.error_code = 'provider_retry'
                service.audit(db, 'provider.retry', task.id)


def process_revocations(service):
    with service.database.transaction() as db:
        jobs = db.scalars(select(ProviderRevocation).where(ProviderRevocation.status == 'pending', ProviderRevocation.next_attempt_at <= service.clock())
                          .with_for_update(skip_locked=not service.database.sqlite).limit(10)).all()
        for job in jobs:
            job.attempts += 1
            try:
                result = service.providers[job.provider].revoke_authorization(service.vault.decrypt(job.credentials_encrypted, 'revoke:' + job.lease_id))
                job.status = 'revoked' if result['revoked'] else 'local_only'
                job.credentials_encrypted = None
                service.audit(db, 'provider.revoke.' + job.status, job.lease_id)
            except Exception:
                job.next_attempt_at = service.clock() + min(3600, 2 ** min(job.attempts, 11))
                service.audit(db, 'provider.revoke.retry', job.lease_id, attempt=job.attempts)


def tick(service):
    expired = service.expire_due()
    process_oauth(service)
    dispatch_deliveries(service)
    process_revocations(service)
    return expired


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = load_settings()
    service = Service(settings, Database(settings.database_url), redis.Redis.from_url(settings.redis_url))
    service.seed()
    while True:
        try:
            expired = tick(service)
            if expired:
                logger.info('expired_leases=%d', expired)
        except Exception:
            logger.error('worker_tick_failed; retrying; credentials suppressed')
        time.sleep(1)


if __name__ == '__main__':
    main()
