import uuid
from decimal import Decimal
from sqlalchemy import String, Text, Integer, Numeric, ForeignKey, CheckConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_no: Mapped[str] = mapped_column(String(80), unique=True)
    external_order_no: Mapped[str] = mapped_column(String(128), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(200))
    product_id: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    status: Mapped[str] = mapped_column(String(24), default='pending')
    paid_at: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint('duration_seconds > 0'),)


class CDK(Base):
    __tablename__ = 'cdks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'), unique=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[int] = mapped_column(Integer)
    activated_at: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default='issued')


class Activation(Base):
    __tablename__ = 'activations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'), unique=True)
    owner_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[int] = mapped_column(Integer)
    expire_at: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default='pending')


class QRSession(Base):
    __tablename__ = 'qr_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    secret_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'))
    activation_id: Mapped[str] = mapped_column(ForeignKey('activations.id'))
    status: Mapped[str] = mapped_column(String(24), default='CREATED')
    created_at: Mapped[int] = mapped_column(Integer)
    expire_at: Mapped[int] = mapped_column(Integer)
    confirmed_at: Mapped[int | None] = mapped_column(Integer)
    lease_id: Mapped[str | None] = mapped_column(ForeignKey('leases.id'))


class AuthorizationTask(Base):
    __tablename__ = 'authorization_tasks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'))
    activation_id: Mapped[str] = mapped_column(ForeignKey('activations.id'))
    qr_session_id: Mapped[str | None] = mapped_column(ForeignKey('qr_sessions.id'), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(Integer)
    expire_at: Mapped[int] = mapped_column(Integer)
    next_poll_at: Mapped[int] = mapped_column(Integer, default=0)
    poll_interval: Mapped[int] = mapped_column(Integer, default=5)


class Resource(Base):
    __tablename__ = 'resources'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    type: Mapped[str] = mapped_column(String(32), default='service')
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default='available')
    capacity: Mapped[int] = mapped_column(Integer)
    active_leases: Mapped[int] = mapped_column(Integer, default=0)
    health: Mapped[str] = mapped_column(String(24), default='healthy')
    metadata_json: Mapped[str] = mapped_column(Text, default='{}')
    __table_args__ = (CheckConstraint('capacity > 0'), CheckConstraint('active_leases >= 0 AND active_leases <= capacity'))


class Lease(Base):
    __tablename__ = 'leases'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'), unique=True)
    buyer_id: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(ForeignKey('resources.id'))
    activated_at: Mapped[int] = mapped_column(Integer)
    expire_at: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(24), default='active')
    token_jti: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[int] = mapped_column(Integer)
    expired_at: Mapped[int | None] = mapped_column(Integer)
    revoked_at: Mapped[int | None] = mapped_column(Integer)
    provider_credentials: Mapped[str | None] = mapped_column(Text)


class Session(Base):
    __tablename__ = 'sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    lease_id: Mapped[str] = mapped_column(ForeignKey('leases.id'), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_jti: Mapped[str] = mapped_column(String(64), unique=True)
    expire_at: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[int | None] = mapped_column(Integer)


class RevokedJTI(Base):
    __tablename__ = 'revoked_jtis'
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expire_at: Mapped[int] = mapped_column(Integer)


class WebhookNonce(Base):
    __tablename__ = 'webhook_nonces'
    nonce_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expire_at: Mapped[int] = mapped_column(Integer)


class Delivery(Base):
    __tablename__ = 'deliveries'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'), unique=True)
    payload_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[int | None] = mapped_column(Integer)


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default='{}')
    created_at: Mapped[int] = mapped_column(Integer)


class ProviderRevocation(Base):
    __tablename__ = 'provider_revocations'
    lease_id: Mapped[str] = mapped_column(ForeignKey('leases.id'), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[int] = mapped_column(Integer)


Index('lease_due', Lease.status, Lease.expire_at)
