"""Reusable cards, an encrypted account pool, and durable QR authorization jobs."""
from urllib.parse import urlencode
from sqlalchemy import select, func
from fastapi import HTTPException
from .db import TerminalState
from .models import VIPAccount, VIPCard, VIPPortalSession, VIPBinding, VIPLoginTask, uid
from .security import digest, random_token
from .providers.quark_cookie import parse_cookie
from .providers.vip_gateway import VIPGateway

RUNNING = ('QUEUED', 'PENDING', 'UNCERTAIN', 'WAIT_VERIFY', 'VERIFY_PENDING')
OCCUPIED = ('reserved', 'active', 'revoke_pending', 'remote_unconfirmed')


class VIPService:
    def __init__(self, service, gateway=None):
        self.service = service
        self.db, self.vault, self.clock = service.database, service.vault, service.clock
        self.gateway = gateway or VIPGateway(service.settings)

    def audit(self, db, event, record, **data):
        self.service.audit(db, 'vip.' + event, record, **data)

    def account_view(self, account):
        return {k: getattr(account, k) for k in
                ('id', 'alias', 'credential_type', 'capacity', 'status', 'checked_at', 'created_at')}

    def import_accounts(self, items):
        # Validate the entire batch before writing; a repeated credential cannot create extra capacity.
        records, hashes, aliases = [], set(), set()
        for item in items:
            secret = item.credential.get_secret_value()
            if item.credential_type == 'cookie':
                secret = parse_cookie(secret)['cookie']
                secret = '; '.join(sorted(secret.split('; ')))
            fingerprint = digest(item.credential_type + ':' + secret)
            if fingerprint in hashes or item.alias in aliases:
                raise HTTPException(409, 'vip_duplicate_account')
            hashes.add(fingerprint)
            aliases.add(item.alias)
            records.append((item, secret, fingerprint))
        with self.db.transaction() as db:
            if db.scalar(select(VIPAccount.id).where(
                    (VIPAccount.alias.in_(aliases)) | (VIPAccount.credential_hash.in_(hashes)))):
                raise HTTPException(409, 'vip_duplicate_account')
            accounts = []
            for item, secret, fingerprint in records:
                account_id = uid()
                account = VIPAccount(id=account_id, alias=item.alias,
                    credential_type=item.credential_type, credential_hash=fingerprint,
                    credentials_encrypted=self.vault.encrypt({'credential': secret}, 'vip-account:' + account_id),
                    capacity=item.capacity, status='unverified', created_at=self.clock())
                db.add(account)
                self.audit(db, 'account.imported', account_id, actor='admin')
                accounts.append(self.account_view(account))
            return accounts

    def credentials(self, account):
        return {'account_id': account.id, 'credential_type': account.credential_type,
                **self.vault.decrypt(account.credentials_encrypted, 'vip-account:' + account.id)}

    def check_account(self, account_id):
        with self.db.transaction() as db:
            account = db.get(VIPAccount, account_id)
            if not account:
                raise HTTPException(404, 'vip_account_not_found')
            payload = self.credentials(account)
            fingerprint = account.credential_hash
        result = self.gateway.call('check', payload, 'check:' + account_id + ':' + random_token())
        limit = result.get('device_limit')
        subject = result.get('account_subject')
        valid = (result.get('authenticated') is True and result.get('membership_valid') is True
                 and result.get('target_identity_supported') is True
                 and type(limit) is int and 1 <= limit <= 100
                 and isinstance(subject, str) and 1 <= len(subject) <= 512)
        with self.db.transaction() as db:
            account = db.scalar(select(VIPAccount).where(VIPAccount.id == account_id).with_for_update())
            if account.credential_hash != fingerprint:
                raise HTTPException(409, 'vip_account_changed')
            if valid:
                subject_hash = digest('quark-subject:' + subject)
                duplicate = db.scalar(select(VIPAccount.id).where(VIPAccount.subject_hash == subject_hash,
                                                                 VIPAccount.id != account.id))
                valid = not duplicate and (account.subject_hash is None or account.subject_hash == subject_hash)
                if valid:
                    account.subject_hash = subject_hash
            # A check must never undo an administrator's disable action.
            if account.status != 'disabled':
                account.status = 'ready' if valid else 'unavailable'
            if valid:
                account.capacity = min(account.capacity, limit)
            account.checked_at = self.clock()
            self.audit(db, 'account.checked', account.id, actor='admin', available=valid)
            return self.account_view(account)

    def create_cards(self, data):
        with self.db.transaction() as db:
            result = []
            for _ in range(data.count):
                card_id, code, now = uid(), 'VG-' + random_token(), self.clock()
                card = VIPCard(id=card_id, code_hash=digest(code), label=data.label,
                    duration_seconds=data.duration_seconds, valid_until=now + data.valid_seconds,
                    max_scans=data.max_scans, used_scans=0, max_devices=data.max_devices,
                    status='issued', created_at=now)
                db.add(card)
                result.append({'id': card_id, 'code': code, 'activation_url':
                    self.service.settings.public_origin + '/qr#' + urlencode({'card': code})})
                self.audit(db, 'card.issued', card_id, actor='admin')
            return result

    def card_view(self, card):
        expired = self.clock() >= (card.expire_at or card.valid_until)
        status = 'expired' if expired and card.status != 'disabled' else card.status
        return {'id': card.id, 'label': card.label, 'status': status, 'activated_at': card.activated_at,
                'expire_at': card.expire_at, 'valid_until': card.valid_until, 'max_devices': card.max_devices,
                'remaining_scans': card.max_scans - card.used_scans,
                'remaining_seconds': max(0, card.expire_at - self.clock()) if card.expire_at else None}

    def eligible(self, card):
        if card.status == 'disabled':
            raise HTTPException(403, 'vip_card_disabled')
        if self.clock() >= (card.expire_at or card.valid_until):
            card.status = 'expired'
            raise TerminalState(410, 'vip_card_expired')

    def open_card(self, code):
        with self.db.transaction() as db:
            card = db.scalar(select(VIPCard).where(VIPCard.code_hash == digest(code)).with_for_update())
            if not card:
                raise HTTPException(403, 'vip_card_invalid')
            self.eligible(card)
            secret = random_token()
            expires = max(card.expire_at or 0, card.valid_until + card.duration_seconds)
            db.add(VIPPortalSession(card_id=card.id, secret_hash=digest(secret), expire_at=expires))
            self.audit(db, 'card.opened', card.id, actor='buyer')
            return self.card_view(card), secret, expires

    def owned_card(self, db, secret, active=True):
        session = db.scalar(select(VIPPortalSession).where(VIPPortalSession.secret_hash == digest(secret or '')))
        if not session or self.clock() >= session.expire_at:
            raise HTTPException(401, 'vip_card_session_required')
        card = db.scalar(select(VIPCard).where(VIPCard.id == session.card_id).with_for_update())
        if active:
            self.eligible(card)
        return card

    def task_view(self, task):
        return {k: getattr(task, k) for k in ('id', 'status', 'error_code', 'challenge_kind', 'expire_at')} | {
            'buyer_login_verified': bool(task.login_verified)}

    def snapshot(self, secret):
        with self.db.transaction() as db:
            card = self.owned_card(db, secret, active=False)
            tasks = db.scalars(select(VIPLoginTask).where(VIPLoginTask.card_id == card.id)
                .order_by(VIPLoginTask.sequence.desc()).limit(10)).all()
            return {'card': self.card_view(card), 'tasks': [self.task_view(t) for t in tasks],
                    'server_time': self.clock(), 'available': self.gateway.configured}

    def submit(self, secret, raw):
        if not self.gateway.configured:
            raise HTTPException(503, 'vip_service_unavailable')
        token = self.service.providers['quark_reference']._token(raw)
        canonical = 'https://su.quark.cn/4_eMHBJ?' + urlencode({'client_id': '532', 'token': token})
        qr_hash = digest('quark:532:' + token)
        with self.db.transaction() as db:
            card = self.owned_card(db, secret)
            existing = db.scalar(select(VIPLoginTask).where(VIPLoginTask.qr_hash == qr_hash))
            if existing:
                if existing.card_id != card.id:
                    raise HTTPException(409, 'vip_qr_already_assigned')
                return self.task_view(existing)
            if card.used_scans >= card.max_scans:
                raise HTTPException(409, 'vip_scan_limit')
            if db.scalar(select(VIPLoginTask.id).where(VIPLoginTask.card_id == card.id,
                                                      VIPLoginTask.status.in_(RUNNING))):
                raise HTTPException(409, 'vip_login_pending')
            task_id, now = uid(), self.clock()
            sequence = (db.scalar(select(func.max(VIPLoginTask.sequence)).where(VIPLoginTask.card_id == card.id)) or 0) + 1
            task = VIPLoginTask(id=task_id, card_id=card.id, sequence=sequence, qr_hash=qr_hash,
                payload_encrypted=self.vault.encrypt({'raw': canonical}, 'vip-task:' + task_id),
                status='QUEUED', login_verified=0, claim_until=0, next_attempt_at=now,
                created_at=now, expire_at=min(now + self.service.settings.qr_ttl, card.expire_at or card.valid_until))
            db.add(task)
            self.audit(db, 'login.submitted', task_id, actor='buyer')
            return self.task_view(task)

    def answer(self, secret, task_id, answer):
        with self.db.transaction() as db:
            card = self.owned_card(db, secret)
            task = db.scalar(select(VIPLoginTask).where(VIPLoginTask.id == task_id,
                VIPLoginTask.card_id == card.id).with_for_update())
            if not task:
                raise HTTPException(404, 'vip_task_not_found')
            if task.status != 'WAIT_VERIFY' or task.claim_until > self.clock():
                raise HTTPException(409, 'vip_verification_not_pending')
            if self.clock() >= task.expire_at:
                raise HTTPException(410, 'vip_qr_expired')
            payload = self.vault.decrypt(task.payload_encrypted, 'vip-task:' + task.id)
            payload['answer'] = answer
            task.payload_encrypted = self.vault.encrypt(payload, 'vip-task:' + task.id)
            task.status, task.next_attempt_at = 'VERIFY_PENDING', self.clock()
            self.audit(db, 'verification.submitted', task.id, actor='buyer')
            return self.task_view(task)

    def release_failed(self, db, task):
        binding = db.get(VIPBinding, task.binding_id) if task.binding_id else None
        # A previous successful login on this binding remains occupied.
        if binding and binding.status == 'reserved':
            binding.status = 'released'

    def reserve(self, db, card, task, target):
        device_hash = digest('quark-device:' + target)
        devices = set(db.scalars(select(VIPBinding.device_hash).where(VIPBinding.card_id == card.id,
            VIPBinding.status != 'released')).all())
        if device_hash not in devices and len(devices) >= card.max_devices:
            raise HTTPException(409, 'vip_device_limit')
        existing = db.scalar(select(VIPBinding).join(VIPAccount).where(VIPBinding.card_id == card.id,
            VIPBinding.device_hash == device_hash, VIPBinding.status == 'active', VIPAccount.status == 'ready'))
        if existing:
            return existing
        # Each account row serializes capacity reservations across cards and workers.
        usage = select(func.count()).select_from(VIPBinding).where(VIPBinding.account_id == VIPAccount.id,
            VIPBinding.status.in_(OCCUPIED)).correlate(VIPAccount).scalar_subquery()
        accounts = db.scalars(select(VIPAccount).where(VIPAccount.status == 'ready', usage < VIPAccount.capacity)
            .order_by(usage * 1.0 / VIPAccount.capacity, VIPAccount.id).limit(10)
            .with_for_update(skip_locked=not self.db.sqlite)).all()
        for account in accounts:
            occupied = db.scalar(select(func.count()).select_from(VIPBinding).where(
                VIPBinding.account_id == account.id, VIPBinding.status.in_(OCCUPIED)))
            if occupied >= account.capacity:
                continue
            binding = VIPBinding(id=uid(), card_id=card.id, account_id=account.id,
                device_hash=device_hash, status='reserved', revoke_supported=0,
                next_attempt_at=0, created_at=self.clock())
            db.add(binding)
            db.flush()
            self.audit(db, 'device.reserved', binding.id)
            return binding
        raise HTTPException(409, 'vip_inventory_empty')

    def process_task(self, task_id):
        # Claim before I/O; never hold database locks during upstream requests.
        with self.db.transaction() as db:
            task = db.scalar(select(VIPLoginTask).where(VIPLoginTask.id == task_id).with_for_update())
            if not task or task.status not in RUNNING or task.next_attempt_at > self.clock() or task.claim_until > self.clock():
                return
            if task.status == 'QUEUED' and self.clock() >= task.expire_at:
                task.status, task.payload_encrypted = 'EXPIRED', None
                return
            claim = random_token()
            task.claim_token, task.claim_until = claim, self.clock() + 60
            payload = self.vault.decrypt(task.payload_encrypted, 'vip-task:' + task.id)
            queued = task.status == 'QUEUED'
        try:
            target = self.gateway.inspect(payload['raw'], task_id + ':inspect') if queued else None
            with self.db.transaction() as db:
                task = db.get(VIPLoginTask, task_id)
                card = db.scalar(select(VIPCard).where(VIPCard.id == task.card_id).with_for_update())
                if task.claim_token != claim:
                    return
                if queued:
                    self.eligible(card)
                    if self.clock() >= task.expire_at:
                        raise HTTPException(410, 'vip_qr_expired')
                    binding = self.reserve(db, card, task, target)
                    task.binding_id = binding.id
                    task.status = 'PENDING'  # Persist before the possibly consequential request.
                    operation = 'authorize'
                else:
                    binding = db.get(VIPBinding, task.binding_id)
                    operation = 'answer' if task.status == 'VERIFY_PENDING' else 'status'
                    if operation == 'answer':
                        task.status = 'PENDING'  # An interrupted answer is resolved through status, never guessed/replayed.
                account = db.get(VIPAccount, binding.account_id)
                outbound = {'task_id': task_id, 'provider': 'quark', 'raw': payload['raw'],
                            'target_hash': binding.device_hash, **self.credentials(account)}
                if operation == 'answer':
                    outbound['answer'] = payload.get('answer', '')
                    payload.pop('answer', None)
                    task.payload_encrypted = self.vault.encrypt(payload, 'vip-task:' + task.id)
                key = task_id + ':' + account.id
            result = self.gateway.call(operation, outbound, key)
            self.finish(task_id, claim, result)
        except HTTPException as exc:
            with self.db.transaction() as db:
                task = db.get(VIPLoginTask, task_id)
                if task.claim_token != claim:
                    return
                task.claim_token, task.claim_until = None, 0
                if task.status == 'QUEUED':
                    task.status = 'EXPIRED' if exc.status_code == 410 else 'FAILED'
                    task.error_code = exc.detail if exc.detail in ('vip_device_limit', 'vip_inventory_empty',
                        'target_device_unverifiable', 'vip_card_disabled', 'vip_card_expired', 'vip_qr_expired') else 'vip_service_unavailable'
                    task.payload_encrypted = None
                    self.release_failed(db, task)
                else:
                    # Upstream may have accepted a timed-out request. Retain the reservation and reconcile.
                    task.status, task.error_code = 'UNCERTAIN', 'vip_confirmation_pending'
                    task.next_attempt_at = self.clock() + 10
                self.audit(db, 'login.retry' if task.status == 'UNCERTAIN' else 'login.failed', task.id)

    def finish(self, task_id, claim, result):
        with self.db.transaction() as db:
            task = db.get(VIPLoginTask, task_id)
            card = db.scalar(select(VIPCard).where(VIPCard.id == task.card_id).with_for_update())
            if task.claim_token != claim:
                return
            binding = db.get(VIPBinding, task.binding_id)
            state = result.get('state')
            task.claim_token, task.claim_until = None, 0
            task.next_attempt_at = self.clock() + 5
            if state == 'authorized':
                target, session_ref = result.get('target_id'), result.get('session_ref')
                if (not isinstance(target, str) or digest('quark-device:' + target) != binding.device_hash
                        or not isinstance(session_ref, str) or not 8 <= len(session_ref) <= 4096
                        or type(result.get('login_verified')) is not bool
                        or type(result.get('revoke_supported')) is not bool):
                    task.status, task.error_code = 'UNCERTAIN', 'vip_confirmation_pending'
                    return
                binding.session_encrypted = self.vault.encrypt({'session_ref': session_ref}, 'vip-binding:' + binding.id)
                binding.revoke_supported = int(result['revoke_supported'])
                task.payload_encrypted, task.challenge_kind = None, None
                if card.status in ('disabled', 'expired') or self.clock() >= (card.expire_at or card.valid_until) or self.clock() >= task.expire_at:
                    binding.status = 'revoke_pending'
                    task.status = 'FAILED'
                    task.error_code = ('vip_card_disabled' if card.status == 'disabled' else
                        'vip_qr_expired' if self.clock() >= task.expire_at and self.clock() < (card.expire_at or card.valid_until)
                        else 'vip_card_expired')
                    return
                binding.status = 'active'
                if card.activated_at is None:
                    card.activated_at, card.expire_at = self.clock(), self.clock() + card.duration_seconds
                card.status, card.used_scans = 'active', card.used_scans + 1
                task.status, task.error_code, task.login_verified = 'AUTHORIZED', None, int(result['login_verified'])
                self.audit(db, 'login.authorized', task.id, buyer_login_verified=bool(task.login_verified))
            elif state == 'challenge' and result.get('kind') in ('sms', 'owner_confirmation', 'captcha'):
                task.status, task.challenge_kind, task.error_code = 'WAIT_VERIFY', result['kind'], None
            elif state == 'pending':
                task.status, task.error_code = 'PENDING', None
            elif state == 'account_unavailable':
                db.get(VIPAccount, binding.account_id).status = 'unavailable'
                self.release_failed(db, task)
                task.binding_id, task.status, task.error_code = None, 'QUEUED', 'vip_account_switching'
                self.audit(db, 'account.switched', task.id)
            elif state in ('rejected', 'expired', 'not_authorized'):
                self.release_failed(db, task)
                task.status = 'EXPIRED' if state == 'expired' else 'FAILED'
                task.error_code = 'vip_qr_expired' if state == 'expired' else 'vip_authorization_rejected'
                task.payload_encrypted, task.challenge_kind = None, None
                self.audit(db, 'login.failed', task.id)
            else:
                task.status, task.error_code = 'UNCERTAIN', 'vip_confirmation_pending'

    def expire_and_revoke(self):
        with self.db.transaction() as db:
            due = db.scalars(select(VIPCard).where(VIPCard.status.in_(('issued', 'active')),
                ((VIPCard.expire_at.is_not(None)) & (VIPCard.expire_at <= self.clock())) |
                ((VIPCard.expire_at.is_(None)) & (VIPCard.valid_until <= self.clock())))
                .limit(1000).with_for_update(skip_locked=not self.db.sqlite)).all()
            for card in due:
                if self.clock() >= (card.expire_at or card.valid_until):
                    card.status = 'expired'
            bindings = db.scalars(select(VIPBinding).join(VIPCard).where(
                VIPCard.status.in_(('expired', 'disabled')), VIPBinding.status == 'active')
                .limit(1000).with_for_update(skip_locked=not self.db.sqlite)).all()
            for binding in bindings:
                binding.status = 'revoke_pending'
            ids = db.scalars(select(VIPBinding.id).where(VIPBinding.status.in_(('revoke_pending', 'remote_unconfirmed')),
                VIPBinding.next_attempt_at <= self.clock()).limit(20)).all()
        for binding_id in ids:
            with self.db.transaction() as db:
                binding = db.scalar(select(VIPBinding).where(VIPBinding.id == binding_id).with_for_update())
                if binding.next_attempt_at > self.clock() or binding.status not in ('revoke_pending', 'remote_unconfirmed'):
                    continue
                if not binding.revoke_supported:
                    binding.status, binding.next_attempt_at = 'remote_unconfirmed', self.clock() + 3600
                    continue
                account = db.get(VIPAccount, binding.account_id)
                payload = {**self.credentials(account),
                    **self.vault.decrypt(binding.session_encrypted, 'vip-binding:' + binding.id)}
                binding.next_attempt_at = self.clock() + 60
            try:
                result = self.gateway.call('revoke', payload, 'revoke:' + binding_id)
            except HTTPException:
                result = {}
            with self.db.transaction() as db:
                binding = db.get(VIPBinding, binding_id)
                if result.get('revoked') is True:
                    binding.status, binding.session_encrypted = 'revoked', None
                    self.audit(db, 'device.revoked', binding.id)
                else:
                    binding.status = 'remote_unconfirmed'
                    self.audit(db, 'device.revocation_pending', binding.id)

    def tick(self):
        self.expire_and_revoke()
        with self.db.transaction() as db:
            ids = db.scalars(select(VIPLoginTask.id).where(VIPLoginTask.status.in_(RUNNING),
                VIPLoginTask.next_attempt_at <= self.clock(), VIPLoginTask.claim_until <= self.clock())
                .order_by(VIPLoginTask.created_at).limit(10)).all()
        for task_id in ids:
            self.process_task(task_id)
