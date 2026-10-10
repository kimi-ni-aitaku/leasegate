import asyncio
import hmac
import io
import json
import logging
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlsplit
import cv2
import numpy as np
import qrcode
import redis
import zxingcpp
from PIL import Image, UnidentifiedImageError
from fastapi import FastAPI, Request, Response, HTTPException, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool
from .config import load_settings
from .db import Database
from .models import Order, CDK, Lease, Resource, Activation, QRSession, AuthorizationTask, Audit, Delivery, WebhookNonce, uid
from .security import random_token, sign, digest
from .service import Service, lease_view, PRODUCTS
from .providers.quark_cookie import CookieStore, parse_cookie, check_account
from . import quark_requests

STATIC = Path(__file__).parent / 'static'
logger = logging.getLogger('leasegate')


class RawQR(BaseModel):
    raw: str = Field(min_length=1, max_length=8192)


class CodeInput(BaseModel):
    code: str = Field(pattern=r'^LG-[A-Za-z0-9_-]{43}$')


class DemoOrder(BaseModel):
    product_id: str = 'test-60s'


class PaymentEvent(BaseModel):
    external_order_no: str = Field(min_length=1, max_length=128)
    buyer_id: str = Field(min_length=1, max_length=200)
    product_id: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    timestamp: int
    nonce: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$')


class AdminLogin(BaseModel):
    token: str = Field(min_length=32, max_length=200)


class QuarkCookieInput(BaseModel):
    cookie: SecretStr = Field(min_length=1, max_length=16384)


class QuarkBuyerQR(BaseModel):
    raw: SecretStr = Field(min_length=1, max_length=8192)


class MemberConfirmation(BaseModel):
    phone_confirmed: bool = Field(strict=True)


class QuarkNativeConfirmation(BaseModel):
    raw: str = Field(min_length=1, max_length=8192)
    sign_wg: SecretStr = Field(min_length=8, max_length=4096)
    kps_wg: SecretStr = Field(min_length=8, max_length=4096)
    vcode: int = Field(strict=True, gt=0)


class ResourceInput(BaseModel):
    provider: str = Field(pattern=r'^(local|oauth)$')
    capacity: int = Field(gt=0, le=10000)


def cookie(response, name, value, settings, max_age):
    response.set_cookie(name, value, max_age=max_age, httponly=True, secure=settings.cookie_secure, samesite='strict', path='/')


def create_app(settings=None, database=None, redis_client=None, clock=None, oauth_client=None):
    settings = settings or load_settings()
    database = database or Database(settings.database_url)
    redis_client = redis_client or redis.Redis.from_url(settings.redis_url, socket_connect_timeout=.3, socket_timeout=.3)
    service = Service(settings, database, redis_client, clock, oauth_client)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(service.seed)
        yield

    app = FastAPI(title='LeaseGate', version='1.0.0', lifespan=lifespan)
    app.state.service = service
    app.add_middleware(CORSMiddleware, allow_origins=[v.strip() for v in settings.cors_origins.split(',')],
                       allow_credentials=True, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'X-CSRF-Token', 'Authorization'])
    fallback, mutex = defaultdict(list), threading.Lock()

    def rate_limit(request):
        ip = request.client.host if request.client else 'unknown'
        bucket = 'mutate' if request.method == 'POST' else 'read'
        global_limit = 120
        path = request.url.path
        limit = 8 if path == '/api/admin/login' else 20 if path == '/api/activation/redeem' else 120 if path == '/api/webhooks/order-paid' or bucket == 'read' else 40
        key = 'rate:' + digest(ip + ':' + bucket + ':' + str(service.clock() // 60))
        endpoint_key = 'rate:' + digest(ip + ':' + path + ':' + str(service.clock() // 60))
        try:
            counts = redis_client.eval("local a=redis.call('INCR',KEYS[1]); if a==1 then redis.call('EXPIRE',KEYS[1],70) end; local b=redis.call('INCR',KEYS[2]); if b==1 then redis.call('EXPIRE',KEYS[2],70) end; return {a,b}", 2, key, endpoint_key)
            return counts[0] <= global_limit and counts[1] <= limit
        except RedisError:
            if settings.environment == 'production' and request.method == 'POST':
                raise HTTPException(503, 'rate_limit_backend_unavailable')
            with mutex:
                now = service.clock()
                for item in [key, endpoint_key]:
                    fallback[item] = [t for t in fallback[item] if t > now - 60]
                    fallback[item].append(now)
                if len(fallback) > 10000:
                    fallback.clear()
                return len(fallback[key]) <= global_limit and len(fallback[endpoint_key]) <= limit

    @app.middleware('http')
    async def guard(request, call_next):
        try:
            if request.url.path.startswith('/api/') and request.url.path != '/api/health':
                if not await run_in_threadpool(rate_limit, request):
                    raise HTTPException(429, 'rate_limited')
            if request.method == 'POST':
                limit = settings.image_max_bytes + 65536 if request.url.path == '/api/qr/decode-image' else 32768
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise HTTPException(413, 'request_too_large')
                request._body = bytes(body)
                if request.url.path != '/api/webhooks/order-paid':
                    origin = request.headers.get('origin')
                    if origin and origin not in [settings.public_origin, *settings.cors_origins.split(',')]:
                        raise HTTPException(403, 'untrusted_origin')
                    supplied, stored = request.headers.get('x-csrf-token', ''), request.cookies.get('lg_csrf', '')
                    try:
                        value, signature = stored.rsplit('.', 1)
                    except ValueError:
                        raise HTTPException(403, 'csrf_required') from None
                    if not supplied or not hmac.compare_digest(supplied, stored) or not hmac.compare_digest(sign(settings.signing_key, 'csrf:' + value), signature):
                        raise HTTPException(403, 'invalid_csrf')
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
                                 'X-Frame-Options': 'DENY', 'Permissions-Policy': 'camera=(self)',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if settings.cookie_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': 'invalid_request', 'fields': ['.'.join(str(v) for v in e['loc']) for e in exc.errors()]}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def conflict_error(request, exc):
        return JSONResponse({'detail': 'concurrent_conflict_retry'}, status_code=409)

    @app.exception_handler(OperationalError)
    async def db_error(request, exc):
        logger.error('database_unavailable; sensitive details suppressed')
        return JSONResponse({'detail': 'database_unavailable_retry'}, status_code=503)

    @app.get('/api/health')
    def health():
        with database.transaction() as db:
            db.execute(text('SELECT 1'))
        try:
            redis_ok = bool(redis_client.ping())
        except RedisError:
            redis_ok = False
        return {'status': 'ok' if redis_ok else 'degraded', 'database': 'ok', 'redis': redis_ok}

    @app.get('/api/config')
    def config():
        return {'demo_enabled': settings.demo_enabled, 'image_max_bytes': settings.image_max_bytes,
                'oauth_enabled': service.providers['oauth'].configured, 'public_origin': settings.public_origin,
                'quark_manual_confirmation_enabled': settings.quark_manual_confirmation_enabled}

    @app.get('/api/csrf')
    def csrf(request: Request, response: Response):
        token = request.cookies.get('lg_csrf', '')
        value, _, signature = token.partition('.')
        if not value or not signature or not hmac.compare_digest(sign(settings.signing_key, 'csrf:' + value), signature):
            value = random_token()
            token = value + '.' + sign(settings.signing_key, 'csrf:' + value)
        cookie(response, 'lg_csrf', token, settings, 3600)
        return {'token': token}

    @app.post('/api/demo/orders')
    def demo(data: DemoOrder):
        if not settings.demo_enabled:
            raise HTTPException(404, 'demo_disabled')
        product = PRODUCTS.get(data.product_id)
        if not product:
            raise HTTPException(422, 'invalid_product')
        with database.transaction() as db:
            order, payload = service.create_paid_order(db, {'external_order_no': 'demo-' + random_token(), 'buyer_id': 'demo-buyer-' + random_token()[:8],
                                                           'product_id': data.product_id, 'amount': product['amount']})
            db.flush()
            delivery = db.scalar(select(Delivery).where(Delivery.order_id == order.id))
            delivery.status, delivery.payload_encrypted, delivery.sent_at = 'sent', None, service.clock()
            service.audit(db, 'delivery.demo_inbox', delivery.id)
            return {**payload, 'order_no': order.order_no, 'duration_seconds': order.duration_seconds}

    @app.post('/api/webhooks/order-paid')
    async def webhook(data: PaymentEvent, request: Request):
        if abs(service.clock() - data.timestamp) > 300:
            raise HTTPException(403, 'webhook_timestamp_invalid')
        body = (await request.body()).decode()
        expected = sign(settings.webhook_secret, f'{data.timestamp}.{data.nonce}.{body}')
        if not hmac.compare_digest(request.headers.get('x-signature', ''), expected):
            raise HTTPException(403, 'invalid_webhook_signature')

        def persist():
            with database.transaction() as db:
                if not database.sqlite:
                    lock_key = int(digest(data.external_order_no)[:15], 16)
                    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
                if db.get(WebhookNonce, digest(data.nonce)):
                    raise HTTPException(409, 'webhook_replay')
                db.add(WebhookNonce(nonce_hash=digest(data.nonce), expire_at=service.clock() + 600))
                order, payload = service.create_paid_order(db, data.model_dump())
                return {'order_id': order.id, 'status': order.status, 'duplicate': payload is None}
        return await run_in_threadpool(persist)

    @app.post('/api/activation/redeem')
    def redeem(data: CodeInput, response: Response):
        with database.transaction() as db:
            activation, owner = service.redeem(db, data.code)
            product_id = db.get(Order, activation.order_id).product_id
        cookie(response, 'lg_activation', owner, settings, 86400)
        return {'activation_id': activation.id, 'status': activation.status, 'product_id': product_id}

    @app.post('/api/activation/quark/submit')
    def quark_submit(data: QuarkBuyerQR, request: Request):
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            if not settings.quark_manual_confirmation_enabled:
                raise HTTPException(409, 'quark_native_app_signature_required')
            return quark_requests.submit(service, db, activation, data.raw.get_secret_value())

    @app.post('/api/activation/quark/{task_id}/cancel')
    def quark_cancel(task_id: str, request: Request):
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            return quark_requests.cancel(service, db, activation, task_id)

    @app.post('/api/activation/quark/{task_id}/login-reported')
    def quark_reported(task_id: str, request: Request):
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            return quark_requests.report_login(service, db, activation, task_id)

    @app.post('/api/qr/create')
    def create_qr(request: Request):
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            if db.scalar(select(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id,
                    AuthorizationTask.provider == 'quark_reference', AuthorizationTask.status == 'WAIT_USER',
                    AuthorizationTask.expire_at > service.clock())):
                raise HTTPException(409, 'quark_request_already_pending')
            return service.create_qr(db, activation)

    @app.post('/api/qr/parse')
    def parse(data: RawQR):
        return service.parser.parse(data.raw).to_dict()

    @app.post('/api/qr/decode-image')
    async def decode_image(image: UploadFile = File(...)):
        try:
            data = await image.read(settings.image_max_bytes + 1)
            if len(data) > settings.image_max_bytes:
                raise HTTPException(413, 'image_too_large')
            def decode():
                if image.content_type not in ('image/png', 'image/jpeg', 'image/webp'):
                    raise HTTPException(415, 'unsupported_image_type')
                try:
                    with Image.open(io.BytesIO(data)) as img:
                        expected = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(img.format)
                        if expected != image.content_type:
                            raise HTTPException(415, 'image_signature_mismatch')
                        if img.width * img.height > 16000000:
                            raise HTTPException(413, 'image_dimensions_too_large')
                        img.verify()
                    with Image.open(io.BytesIO(data)) as img:
                        pixels = np.array(img.convert('RGB'))
                    raw, _, _ = cv2.QRCodeDetector().detectAndDecode(pixels)
                    if not raw:
                        decoded = zxingcpp.read_barcode(pixels, formats=zxingcpp.BarcodeFormat.QRCode)
                        raw = decoded.text if decoded else ''
                except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError, cv2.error):
                    raise HTTPException(422, 'invalid_image') from None
                if not raw:
                    raise HTTPException(422, 'qr_not_found')
                return service.parser.parse(raw).to_dict()
            return await run_in_threadpool(decode)
        finally:
            await image.close()  # closes and removes multipart spooled temporary file

    @app.post('/api/qr/{qr_id}/scanned')
    def scanned(qr_id: str, data: RawQR):
        with database.transaction() as db:
            return service.scan(db, qr_id, data.raw)

    @app.post('/api/qr/{qr_id}/confirm')
    def confirm(qr_id: str, data: RawQR):
        with database.transaction() as db:
            lease = service.confirm(db, qr_id, data.raw)
            result = lease_view(lease, service.clock())
            result.pop('buyer_id')
        service.schedule(lease)
        return result

    def qr_snapshot(qr_id, owner):
        with database.transaction() as db:
            activation = service.owned_activation(db, owner, allow_expired=True)
            qr = db.get(QRSession, qr_id)
            if not qr or qr.activation_id != activation.id:
                raise HTTPException(404, 'qr_not_found')
            if qr.status in ('CREATED', 'SCANNED') and service.clock() >= qr.expire_at:
                qr.status = 'EXPIRED'
            return {'id': qr.id, 'status': qr.status, 'lease_id': qr.lease_id, 'expire_at': qr.expire_at}

    @app.get('/api/qr/{qr_id}/events')
    async def events(qr_id: str, request: Request):
        owner = request.cookies.get('lg_activation')
        first = await run_in_threadpool(qr_snapshot, qr_id, owner)
        async def stream():
            previous = None
            for i in range(1800):
                if await request.is_disconnected():
                    return
                state = first if i == 0 else await run_in_threadpool(qr_snapshot, qr_id, owner)
                if state['status'] != previous:
                    name = {'CREATED': 'created', 'SCANNED': 'scanned', 'CONFIRMED': 'confirmed', 'EXPIRED': 'expired', 'CANCELLED': 'cancelled'}[state['status']]
                    yield f'event: {name}\ndata: {json.dumps(state)}\n\n'
                    previous = state['status']
                elif i % 15 == 0:
                    yield ': heartbeat\n\n'
                if previous in ('CONFIRMED', 'EXPIRED', 'CANCELLED'):
                    return
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/qr/{qr_id}/image')
    def qr_image(qr_id: str, request: Request):
        # The raw proof stays in the client, and PNG generation is authenticated and body-free.
        raw = request.headers.get('x-qr-payload', '')
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            qr = service.verify_qr(db, qr_id, raw)
            if qr.activation_id != activation.id:
                raise HTTPException(403, 'qr_not_owned')
        image = qrcode.make(raw)
        output = io.BytesIO()
        image.save(output, format='PNG')
        return Response(output.getvalue(), media_type='image/png')

    @app.post('/api/activation/result')
    def result(request: Request, response: Response):
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            quark_request = quark_requests.snapshot(service, db, activation)
            product_id = db.get(Order, activation.order_id).product_id
            lease, token, refresh = service.issue_session(db, activation, request.cookies.get('lg_refresh'))
            if not lease:
                task = db.scalar(select(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id, AuthorizationTask.status != 'CANCELLED').order_by(AuthorizationTask.created_at.desc()))
                public = None
                if task and task.provider == 'oauth' and task.status == 'WAIT_USER' and task.credentials_encrypted:
                    context = service.vault.decrypt(task.credentials_encrypted, 'task:' + task.id)
                    public = {key: context[key] for key in ('user_code', 'verification_uri', 'verification_uri_complete', 'interval')}
                return {'status': 'pending', 'authorization_status': task.status if task else 'CREATED', 'authorization': public,
                        'quark_request': quark_request, 'product_id': product_id}
            payload = {'status': 'active', 'lease': lease_view(lease, service.clock()), 'access_token': token,
                       'server_time': service.clock(), 'quark_request': quark_request, 'product_id': product_id}
        if refresh:
            cookie(response, 'lg_refresh', refresh, settings, lease.expire_at - service.clock())
        cookie(response, 'lg_access', token, settings, min(settings.access_ttl, lease.expire_at - service.clock()))
        return payload

    @app.post('/api/auth/refresh')
    def refresh(request: Request, response: Response):
        with database.transaction() as db:
            lease, token, raw = service.refresh(db, request.cookies.get('lg_refresh'))
        cookie(response, 'lg_refresh', raw, settings, lease.expire_at - service.clock())
        cookie(response, 'lg_access', token, settings, min(settings.access_ttl, lease.expire_at - service.clock()))
        return {'access_token': token, 'lease': lease_view(lease, service.clock()), 'server_time': service.clock()}

    @app.get('/api/service')
    def protected(request: Request):
        token = request.headers.get('authorization', '').removeprefix('Bearer ') or request.cookies.get('lg_access')
        with database.transaction() as db:
            lease, session = service.require_active_lease(db, token)
            return {'message': '服务可正常访问', 'lease': lease_view(lease, service.clock()), 'server_time': service.clock()}

    @app.post('/api/providers/{provider}/authorize')
    def authorize(provider: str, request: Request):
        adapter = service.providers.get(provider, service.providers['unsupported'])
        with database.transaction() as db:
            activation = service.owned_activation(db, request.cookies.get('lg_activation'))
            if provider != 'oauth':
                return adapter.create_authorization({})
            if db.scalar(select(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id,
                    AuthorizationTask.provider == 'quark_reference', AuthorizationTask.status == 'WAIT_USER',
                    AuthorizationTask.expire_at > service.clock())):
                raise HTTPException(409, 'quark_request_already_pending')
            if activation.status != 'pending' or db.scalar(select(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id, AuthorizationTask.provider == 'oauth',
                           AuthorizationTask.status.in_(['WAIT_USER', 'AUTHORIZED']))):
                raise HTTPException(409, 'authorization_already_pending')
            data = adapter.create_device_authorization()
            db.execute(update(QRSession).where(QRSession.activation_id == activation.id, QRSession.status.in_(['CREATED', 'SCANNED'])).values(status='CANCELLED'))
            db.execute(update(AuthorizationTask).where(AuthorizationTask.activation_id == activation.id, AuthorizationTask.provider == 'local',
                       AuthorizationTask.status == 'WAIT_USER').values(status='CANCELLED'))
            task_id, now = uid(), service.clock()
            task = AuthorizationTask(id=task_id, order_id=activation.order_id, activation_id=activation.id, provider='oauth',
                                     status='WAIT_USER', credentials_encrypted=service.vault.encrypt({key: data[key] for key in ('device_code', 'user_code', 'verification_uri', 'verification_uri_complete', 'interval')}, 'task:' + task_id),
                                     created_at=now, expire_at=min(activation.expire_at, now + data['expires_in']),
                                     poll_interval=data['interval'], next_poll_at=now + data['interval'])
            db.add(task)
            service.audit(db, 'provider.device_created', task_id, 'buyer', provider='oauth')
            return {'task_id': task_id, 'user_code': data['user_code'], 'verification_uri': data['verification_uri'],
                    'verification_uri_complete': data['verification_uri_complete'], 'expires_in': data['expires_in'], 'interval': data['interval']}

    def admin(request):
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if supplied and hmac.compare_digest(supplied, settings.admin_token):
            return
        stored = request.cookies.get('lg_admin', '')
        try:
            expires, nonce, signature = stored.split('.')
            if int(expires) > service.clock() and hmac.compare_digest(signature, sign(settings.signing_key, f'admin:{expires}.{nonce}')):
                return
        except (ValueError, TypeError):
            pass
        raise HTTPException(401, 'admin_authentication_required')

    @app.post('/api/admin/login')
    def login(data: AdminLogin, response: Response):
        if not hmac.compare_digest(data.token, settings.admin_token):
            raise HTTPException(401, 'invalid_admin_token')
        expires, nonce = service.clock() + 3600, random_token()
        value = f'{expires}.{nonce}.' + sign(settings.signing_key, f'admin:{expires}.{nonce}')
        cookie(response, 'lg_admin', value, settings, 3600)
        return {'status': 'ok'}

    @app.post('/api/admin/logout')
    def logout(response: Response):
        response.delete_cookie('lg_admin', path='/')
        return {'status': 'ok'}

    @app.get('/api/admin/{collection}')
    def admin_list(collection: str, request: Request, offset: int = 0):
        admin(request)
        if offset < 0 or offset > 1000000:
            raise HTTPException(422, 'invalid_offset')
        service.expire_due()
        mapping = {'orders': Order, 'cdks': CDK, 'leases': Lease, 'resources': Resource, 'quark-requests': AuthorizationTask,
                   'authorization-tasks': AuthorizationTask, 'audit': Audit, 'qr-sessions': QRSession, 'deliveries': Delivery}
        model = mapping.get(collection)
        if not model:
            raise HTTPException(404, 'unknown_collection')
        excluded = {'code_hash', 'secret_hash', 'owner_hash', 'token_jti', 'provider_credentials', 'credentials_encrypted', 'payload_encrypted'}
        with database.transaction() as db:
            order_field = getattr(model, 'created_at', model.id)
            query = select(model)
            if collection == 'quark-requests':
                query = query.where(AuthorizationTask.provider == 'quark_reference')
            rows = db.scalars(query.order_by(order_field.desc()).offset(offset).limit(100)).all()
            items = [{col.name: getattr(row, col.name) for col in model.__table__.columns if col.name not in excluded} for row in rows]
            for item in items:
                if 'expire_at' in item:
                    item['remaining_seconds'] = max(0, item['expire_at'] - service.clock())
            return {'items': items, 'offset': offset, 'next_offset': offset + len(items) if len(items) == 100 else None, 'server_time': service.clock()}

    @app.get('/api/admin/quark-requests/{task_id}/image')
    def quark_request_image(task_id: str, request: Request):
        admin(request)
        with database.transaction() as db:
            raw = quark_requests.qr_payload(service, db, task_id)
        output = io.BytesIO()
        qrcode.make(raw).save(output, format='PNG')
        return Response(output.getvalue(), media_type='image/png')

    @app.post('/api/admin/quark-requests/{task_id}/confirmed')
    def quark_member_confirmed(task_id: str, data: MemberConfirmation, request: Request):
        admin(request)
        if not settings.quark_manual_confirmation_enabled:
            raise HTTPException(409, 'quark_manual_confirmation_disabled')
        if data.phone_confirmed is not True:
            raise HTTPException(422, 'quark_phone_confirmation_required')
        with database.transaction() as db:
            lease = quark_requests.member_confirmed(service, db, task_id)
            result = {'status': 'OWNER_CONFIRMED', 'buyer_login_verified': False,
                      'remote_session_revocation_supported': False, 'lease_expire_at': lease.expire_at}
        service.schedule(lease)
        return result

    quark_store = CookieStore(settings.quark_cookie_file, service.vault)

    @app.get('/api/admin/quark-cookie/status')
    def quark_status(request: Request):
        admin(request)
        return quark_store.status()

    @app.post('/api/admin/quark-cookie/import')
    def quark_import(data: QuarkCookieInput, request: Request):
        admin(request)
        quark_store.save(parse_cookie(data.cookie.get_secret_value()))
        return quark_store.status()

    @app.post('/api/admin/quark-cookie/check')
    def quark_check(request: Request):
        admin(request)
        return check_account(quark_store.load())

    @app.post('/api/admin/quark-cookie/clear')
    def quark_clear(request: Request):
        admin(request)
        quark_store.clear()
        return quark_store.status()

    @app.post('/api/admin/quark-cookie/confirm-qr')
    def quark_native_confirm(data: QuarkNativeConfirmation, request: Request):
        admin(request)
        provider = service.providers['quark_reference']
        result = provider.create_authorization({'raw': data.raw, 'sign_wg': data.sign_wg.get_secret_value(),
            'kps_wg': data.kps_wg.get_secret_value(), 'vcode': data.vcode})
        # No Lease is issued by a test endpoint. Official buyer login remains separately verified.
        with database.transaction() as db:
            service.audit(db, 'quark.confirmation.submitted', uid(), 'admin', accepted=result['confirmation_accepted'],
                          upstream_status=result['upstream_status'])
        return {**result, 'poll': provider.get_authorization_status({'raw': data.raw})}

    @app.post('/api/admin/leases/{lease_id}/revoke')
    def revoke(lease_id: str, request: Request):
        admin(request)
        with database.transaction() as db:
            lease = db.scalar(select(Lease).where(Lease.id == lease_id).with_for_update())
            if not lease:
                raise HTTPException(404, 'lease_not_found')
            service.close_lease(db, lease, 'revoked', 'admin')
            return lease_view(lease, service.clock())

    @app.post('/api/admin/resources')
    def resource_create(data: ResourceInput, request: Request):
        admin(request)
        with database.transaction() as db:
            resource = Resource(provider=data.provider, capacity=data.capacity, active_leases=0, health='healthy', status='available')
            db.add(resource)
            db.flush()
            service.audit(db, 'resource.created', resource.id, 'admin', capacity=data.capacity)
            return {'id': resource.id}

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/')
    @app.get('/activate')
    @app.get('/scan/{qr_id}')
    @app.get('/admin/{page}')
    def page():
        return FileResponse(STATIC / 'index.html')

    return app


def app_factory():
    return create_app()
