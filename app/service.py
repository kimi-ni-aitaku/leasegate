import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlencode
from sqlalchemy import select, update, delete
from fastapi import HTTPException
from redis.exceptions import RedisError
from .models import Order, CDK, Activation, QRSession, AuthorizationTask, Resource, Lease, Session, RevokedJTI, Audit, Delivery, ProviderRevocation, WebhookNonce, uid
from .security import random_token, digest, sign, Vault, encode_access, decode_access
from .db import TerminalState
from .qr import QRParser
from .providers.local import LocalProvider
from .providers.oauth import OAuthProvider
from .providers.quark_reference import UnsupportedProvider, QuarkReferenceProvider


PRODUCTS = {'test-60s': {'duration': 60, 'amount': Decimal('0.01')},
            'service-24h': {'duration': 86400, 'amount': Decimal('9.90')}}


class Service:
    def __init__(self, settings, database, redis_client, clock=None, oauth_client=None):
        self.settings, self.database, self.redis = settings, database, redis_client
        self.clock = clock or (lambda: int(time.time()))
        self.vault = Vault(settings.encryption_key)
        self.parser = QRParser(settings)
        self.providers = {'local': LocalProvider(self.parser), 'oauth': OAuthProvider(settings, self.parser, oauth_client),
                          'unsupported': UnsupportedProvider(), 'quark_reference': QuarkReferenceProvider()}

    def audit(self, db, event, entity_id, actor='system', **detail):
        db.add(Audit(event=event, entity_id=entity_id, actor=actor, detail=json.dumps(detail), created_at=self.clock()))

    def seed(self):
        with self.database.transaction() as db:
            for provider in ['local'] + (['oauth'] if self.providers['oauth'].configured else []):
                if not db.scalar(select(Resource).where(Resource.provider == provider)):
                    db.add(Resource(provider=provider, capacity=20, active_leases=0, health='healthy', status='available'))

    def create_paid_order(self, db, data):
        existing = db.scalar(select(Order).where(Order.external_order_no == data['external_order_no']).with_for_update())
        if existing:
            if (existing.buyer_id, existing.product_id, existing.amount) != (data['buyer_id'], data['product_id'], Decimal(str(data['amount']))):
                raise HTTPException(409, 'order_idempotency_conflict')
            return existing, None
        product = PRODUCTS.get(data['product_id'])
        if not product or Decimal(str(data['amount'])) != product['amount']:
            raise HTTPException(422, 'invalid_product_or_amount')
        if data['product_id'] == 'test-60s' and not self.settings.demo_enabled:
            raise HTTPException(422, 'test_product_disabled')
        now, order_id, code = self.clock(), uid(), 'LG-' + random_token()
        order = Order(id=order_id, order_no='ORD-' + random_token()[:16], external_order_no=data['external_order_no'],
                      buyer_id=data['buyer_id'], product_id=data['product_id'], amount=product['amount'],
                      duration_seconds=product['duration'], status='paid', paid_at=now, created_at=now)
        db.add(order)
        db.flush()
        db.add(CDK(code_hash=digest(code), order_id=order.id, valid_until=now + self.settings.cdk_ttl, max_uses=1, used_count=0, status='issued'))
        payload = {'order_id': order.id, 'buyer_id': order.buyer_id, 'code': code,
                   'activation_url': f'{self.settings.public_origin}/activate#' + urlencode({'code': code})}
        db.add(Delivery(order_id=order.id, payload_encrypted=self.vault.encrypt(payload, 'delivery:' + order.id), next_attempt_at=now))
        self.audit(db, 'order.paid', order.id)
        return order, payload

    def redeem(self, db, code):
        cdk = db.scalar(select(CDK).where(CDK.code_hash == digest(code)).with_for_update())
        if not cdk:
            raise HTTPException(403, 'invalid_cdk')
        if self.clock() >= cdk.valid_until:
            cdk.status = 'expired'
            raise TerminalState(410, 'cdk_expired')
        if cdk.status != 'issued' or cdk.used_count >= cdk.max_uses:
            raise HTTPException(409, 'cdk_already_used')
        order = db.scalar(select(Order).where(Order.id == cdk.order_id).with_for_update())
        if order.status != 'paid':
            raise HTTPException(409, 'order_not_eligible')
        owner = random_token()
        activation = Activation(order_id=order.id, owner_hash=digest(owner), created_at=self.clock(),
                                expire_at=min(cdk.valid_until, self.clock() + 1800), status='pending')
        db.add(activation)
        db.flush()
        cdk.used_count += 1
        cdk.status, cdk.activated_at = 'used', self.clock()
        self.audit(db, 'activation.created', activation.id, 'buyer')
        return activation, owner

    def owned_activation(self, db, owner, allow_expired=False):
        activation = db.scalar(select(Activation).where(Activation.owner_hash == digest(owner or '')).with_for_update())
        if not activation:
            raise HTTPException(401, 'activation_session_required')
        if self.clock() >= activation.expire_at and activation.status != 'claimed' and not allow_expired:
            raise HTTPException(410, 'activation_expired')
        return activation

    def create_qr(self, db, activation):
        if activation.status != 'pending':
            raise HTTPException(409, 'activation_already_completed')
        if db.scalar(select(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id, AuthorizationTask.provider == 'oauth',
                                                    AuthorizationTask.status.in_(['WAIT_USER', 'AUTHORIZED']))):
            raise HTTPException(409, 'official_authorization_already_pending')
        # QR regeneration cancels every older proof, leaving only one active authorization path.
        now = self.clock()
        db.execute(update(QRSession).where(QRSession.activation_id == activation.id, QRSession.status.in_(['CREATED', 'SCANNED'])).values(status='CANCELLED'))
        db.execute(update(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id, AuthorizationTask.status.in_(['CREATED', 'WAIT_USER'])).values(status='CANCELLED', credentials_encrypted=None))
        secret, qr_id = random_token(), uid()
        expires = min(activation.expire_at, now + self.settings.qr_ttl)
        qr = QRSession(id=qr_id, secret_hash=digest(secret), order_id=activation.order_id, activation_id=activation.id,
                       status='CREATED', created_at=now, expire_at=expires)
        db.add(qr)
        db.flush()
        state = self.providers['local'].create_authorization({'qr_session_id': qr.id})
        db.add(AuthorizationTask(order_id=activation.order_id, activation_id=activation.id, qr_session_id=qr.id,
                                 provider='local', status=state['state'], created_at=now, expire_at=expires))
        proof = {'secret': secret, 'exp': expires, 'sig': sign(self.settings.signing_key, f'{qr_id}.{secret}.{expires}')}
        url = f'{self.settings.public_origin}/scan/{qr_id}#' + urlencode(proof)
        self.audit(db, 'qr.created', qr.id, 'buyer')
        return {'id': qr.id, 'url': url, 'expire_at': expires, 'status': qr.status}

    def verify_qr(self, db, qr_id, raw):
        parsed = self.providers['local'].parse_qr(raw)
        if parsed.type != 'local_authorization' or parsed.metadata['qr_id'] != qr_id:
            raise HTTPException(403, 'qr_proof_mismatch')
        initial = db.get(QRSession, qr_id)
        if not initial:
            raise HTTPException(403, 'invalid_qr_proof')
        db.scalar(select(Activation).where(Activation.id == initial.activation_id).with_for_update())
        qr = db.scalar(select(QRSession).where(QRSession.id == qr_id).with_for_update().execution_options(populate_existing=True))
        if not qr or not hmac.compare_digest(qr.secret_hash, digest(parsed.token)) or parsed.metadata['expire_at'] != qr.expire_at:
            raise HTTPException(403, 'invalid_qr_proof')
        if qr.status in ('CANCELLED', 'EXPIRED'):
            raise HTTPException(410, 'qr_unavailable')
        if self.clock() >= qr.expire_at and qr.status != 'CONFIRMED':
            qr.status = 'EXPIRED'
            db.execute(update(AuthorizationTask).where(AuthorizationTask.qr_session_id == qr.id).values(status='EXPIRED'))
            self.audit(db, 'qr.expired', qr.id)
            raise TerminalState(410, 'qr_expired')
        return qr

    def scan(self, db, qr_id, raw):
        qr = self.verify_qr(db, qr_id, raw)
        if qr.status == 'CREATED':
            qr.status = 'SCANNED'
            self.audit(db, 'qr.scanned', qr.id, 'buyer')
        order = db.get(Order, qr.order_id)
        return {'id': qr.id, 'status': qr.status, 'order_no': order.order_no, 'duration_seconds': order.duration_seconds}

    def allocate_resource(self, db, provider):
        resource = db.scalar(select(Resource).where(Resource.provider == provider, Resource.health == 'healthy',
                             Resource.status == 'available', Resource.active_leases < Resource.capacity)
                             .order_by(Resource.active_leases * 1.0 / Resource.capacity, Resource.id)
                             .with_for_update(skip_locked=not self.database.sqlite).limit(1))
        if not resource:
            raise HTTPException(409, 'resource_capacity_unavailable')
        resource.active_leases += 1
        return resource

    def new_lease(self, db, order, activation, provider, credentials=None):
        existing = db.scalar(select(Lease).where(Lease.order_id == order.id))
        if existing:
            return existing
        if order.status != 'paid' or activation.status != 'pending':
            raise HTTPException(409, 'order_not_eligible')
        resource, now = self.allocate_resource(db, provider), self.clock()
        lease = Lease(order_id=order.id, buyer_id=order.buyer_id, provider=provider, resource_id=resource.id,
                      activated_at=now, expire_at=now + order.duration_seconds, status='active', token_jti=random_token(), created_at=now)
        db.add(lease)
        db.flush()
        if credentials:
            lease.provider_credentials = self.vault.encrypt(credentials, 'lease:' + lease.id)
        order.status, activation.status = 'activated', 'authorized'
        self.audit(db, 'lease.activated', lease.id, 'buyer', provider=provider, resource_id=resource.id)
        return lease

    def confirm(self, db, qr_id, raw):
        qr = self.verify_qr(db, qr_id, raw)
        if qr.status == 'CONFIRMED':
            return self.active_lease(db, qr.lease_id)
        activation = db.scalar(select(Activation).where(Activation.id == qr.activation_id).with_for_update())
        order = db.scalar(select(Order).where(Order.id == qr.order_id).with_for_update())
        lease = self.new_lease(db, order, activation, 'local')
        qr.status, qr.confirmed_at, qr.lease_id = 'CONFIRMED', self.clock(), lease.id
        self.providers['local'].exchange_authorization({'status': qr.status})
        db.execute(update(AuthorizationTask).where(AuthorizationTask.qr_session_id == qr.id).values(status='EXCHANGED'))
        self.audit(db, 'qr.confirmed', qr.id, 'buyer')
        return lease

    def schedule(self, lease):
        try:
            self.redis.zadd('lease:expiry', {lease.id: lease.expire_at})
        except RedisError:
            # The database remains the durable queue; reconciliation restores the Redis index.
            pass

    def close_lease(self, db, lease, status='expired', actor='system'):
        if lease.status != 'active':
            return False
        now = self.clock()
        lease.status = status
        if status == 'expired':
            lease.expired_at = now
        else:
            lease.revoked_at = now
        order = db.get(Order, lease.order_id)
        if order.status not in ('refunded', 'cancelled'):
            order.status = 'expired' if status == 'expired' else 'cancelled'
        resource = db.scalar(select(Resource).where(Resource.id == lease.resource_id).with_for_update())
        resource.active_leases -= 1
        db.execute(update(AuthorizationTask).where(AuthorizationTask.order_id == lease.order_id,
            AuthorizationTask.provider == 'quark_reference',
            AuthorizationTask.status.in_(['OWNER_CONFIRMED', 'BUYER_REPORTED_LOGIN'])).values(
                status='EXPIRED' if status == 'expired' else 'CANCELLED', credentials_encrypted=None))
        sessions = db.scalars(select(Session).where(Session.lease_id == lease.id)).all()
        for session in sessions:
            if not db.get(RevokedJTI, session.token_jti):
                db.add(RevokedJTI(jti=session.token_jti, expire_at=lease.expire_at + 900))
            db.delete(session)
        if not db.get(RevokedJTI, lease.token_jti):
            db.add(RevokedJTI(jti=lease.token_jti, expire_at=lease.expire_at + 900))
        if lease.provider_credentials:
            credentials = self.vault.decrypt(lease.provider_credentials, 'lease:' + lease.id)
            db.add(ProviderRevocation(lease_id=lease.id, provider=lease.provider,
                                     credentials_encrypted=self.vault.encrypt(credentials, 'revoke:' + lease.id), next_attempt_at=now))
            lease.provider_credentials = None
        self.audit(db, 'lease.' + status, lease.id, actor, resource_released=True)
        return True

    def active_lease(self, db, lease_id):
        lease = db.scalar(select(Lease).where(Lease.id == lease_id).with_for_update())
        if not lease:
            raise HTTPException(401, 'lease_not_found')
        if lease.status != 'active':
            raise HTTPException(401, 'lease_' + lease.status)
        if self.clock() >= lease.expire_at:
            self.close_lease(db, lease)
            raise TerminalState(401, 'lease_expired')
        return lease

    def require_active_lease(self, db, access_token, scope='service:read'):
        claims = decode_access(self.settings, access_token or '', self.clock())
        lease = self.active_lease(db, claims['lease_id'])
        session = db.get(Session, claims['sid'])
        if (claims['exp'] <= self.clock() or not session or session.revoked_at or session.lease_id != lease.id
                or claims['jti'] != session.token_jti or db.get(RevokedJTI, claims['jti'])
                or claims['sub'] != lease.buyer_id or claims['order_id'] != lease.order_id):
            raise HTTPException(401, 'session_or_token_revoked')
        if scope not in claims['scope']:
            raise HTTPException(403, 'insufficient_scope')
        return lease, session

    def issue_session(self, db, activation, refresh_cookie):
        lease = db.scalar(select(Lease).where(Lease.order_id == activation.order_id).with_for_update())
        if not lease:
            return None, None, None
        self.active_lease(db, lease.id)
        if activation.status == 'claimed':
            session = db.scalar(select(Session).where(Session.refresh_hash == digest(refresh_cookie or ''), Session.lease_id == lease.id))
            if not session:
                raise HTTPException(409, 'activation_already_claimed')
            return lease, encode_access(self.settings, lease, session, self.clock()), None
        refresh = random_token()
        session = Session(lease_id=lease.id, refresh_hash=digest(refresh), token_jti=random_token(), expire_at=lease.expire_at)
        db.add(session)
        db.flush()
        activation.status = 'claimed'
        self.audit(db, 'session.created', session.id, 'buyer')
        return lease, encode_access(self.settings, lease, session, self.clock()), refresh

    def refresh(self, db, raw):
        initial = db.scalar(select(Session).where(Session.refresh_hash == digest(raw or '')))
        if not initial:
            raise HTTPException(401, 'invalid_refresh_session')
        lease = self.active_lease(db, initial.lease_id)
        session = db.scalar(select(Session).where(Session.refresh_hash == digest(raw or '')).with_for_update().execution_options(populate_existing=True))
        if not session or session.revoked_at:
            raise HTTPException(401, 'invalid_refresh_session')
        if self.clock() >= session.expire_at:
            raise HTTPException(401, 'refresh_expired')
        db.add(RevokedJTI(jti=session.token_jti, expire_at=lease.expire_at + 900))
        refresh = random_token()
        session.refresh_hash, session.token_jti = digest(refresh), random_token()
        self.audit(db, 'session.rotated', session.id, 'buyer')
        return lease, encode_access(self.settings, lease, session, self.clock()), refresh

    def expire_due(self):
        now, expired = self.clock(), 0
        with self.database.transaction() as db:
            leases = db.scalars(select(Lease).where(Lease.status == 'active', Lease.expire_at <= now)
                               .with_for_update(skip_locked=not self.database.sqlite).limit(200)).all()
            for lease in leases:
                expired += int(self.close_lease(db, lease))
            db.execute(update(QRSession).where(QRSession.expire_at <= now, QRSession.status.in_(['CREATED', 'SCANNED'])).values(status='EXPIRED'))
            db.execute(update(AuthorizationTask).where(AuthorizationTask.expire_at <= now, AuthorizationTask.status.in_(['CREATED', 'WAIT_USER'])).values(status='EXPIRED', credentials_encrypted=None))
            db.execute(delete(WebhookNonce).where(WebhookNonce.expire_at < now))
            db.execute(delete(RevokedJTI).where(RevokedJTI.expire_at < now))
        try:
            due = self.redis.zrangebyscore('lease:expiry', '-inf', now)
            if due:
                self.redis.zrem('lease:expiry', *due)
            with self.database.transaction() as db:
                active = db.scalars(select(Lease).where(Lease.status == 'active')).all()
                if active:
                    self.redis.zadd('lease:expiry', {l.id: l.expire_at for l in active})
        except RedisError:
            pass
        return expired


def lease_view(lease, now):
    return {'id': lease.id, 'order_id': lease.order_id, 'buyer_id': lease.buyer_id, 'provider': lease.provider,
            'resource_id': lease.resource_id, 'activated_at': lease.activated_at, 'expire_at': lease.expire_at,
            'status': lease.status, 'remaining_seconds': max(0, lease.expire_at - now) if lease.status == 'active' else 0}
