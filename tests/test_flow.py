from concurrent.futures import ThreadPoolExecutor
import io
import json
import secrets
from decimal import Decimal
import pytest
import qrcode
import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models import Order, CDK, Activation, QRSession, Lease, Session, Resource, Audit, Delivery
from app.security import sign, digest
from app.main import create_app
from conftest import activate, prepare_qr, create_order


def test_60_second_boundary_without_worker(system):
    service, client, _ = system
    _, _, result = activate(client)
    token = result['access_token']
    assert result['lease']['expire_at'] - result['lease']['activated_at'] == 60
    assert client.get('/api/service').status_code == 200
    service.clock.advance(30)
    assert client.get('/api/service').status_code == 200
    service.clock.advance(29)
    assert client.get('/api/service').status_code == 200
    service.clock.advance(1)
    response = client.get('/api/service', headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 401 and response.json()['detail'] == 'lease_expired'
    service.clock.advance(1)
    assert client.get('/api/service').status_code == 401
    assert client.post('/api/auth/refresh', json={}).status_code == 401
    with service.database.transaction() as db:
        assert db.get(Lease, result['lease']['id']).status == 'expired'
        assert db.scalar(select(Resource)).active_leases == 0
        assert db.scalar(select(func.count()).select_from(Session)) == 0
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.event == 'lease.expired')) == 1


def test_worker_duplicate_and_redis_rebuild(system):
    service, client, _ = system
    _, _, result = activate(client)
    service.redis.flushall()
    assert service.expire_due() == 0
    assert service.redis.zscore('lease:expiry', result['lease']['id']) == result['lease']['expire_at']
    service.clock.advance(60)
    assert service.expire_due() == 1
    assert service.expire_due() == 0
    assert service.redis.zcard('lease:expiry') == 0
    with service.database.transaction() as db:
        assert db.scalar(select(Resource)).active_leases == 0


def test_repeat_confirm_and_sse_final(system):
    service, client, _ = system
    order, qr, result = activate(client)
    for _ in range(3):
        response = client.post(f'/api/qr/{qr["id"]}/confirm', json={'raw': qr['url']})
        assert response.status_code == 200 and response.json()['id'] == result['lease']['id']
    response = client.get(f'/api/qr/{qr["id"]}/events')
    assert 'event: confirmed' in response.text
    with service.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(Lease)) == 1
        assert db.scalar(select(Resource)).active_leases == 1


def test_scan_other_browser_cannot_claim_lease(system):
    service, client, app = system
    _, qr = prepare_qr(client)
    with TestClient(app) as scanner:
        scanner.headers['X-CSRF-Token'] = scanner.get('/api/csrf').json()['token']
        assert scanner.post(f'/api/qr/{qr["id"]}/scanned', json={'raw': qr['url']}).status_code == 200
        assert scanner.post(f'/api/qr/{qr["id"]}/confirm', json={'raw': qr['url']}).status_code == 200
        assert scanner.post('/api/activation/result', json={}).status_code == 401
        assert scanner.get(f'/api/qr/{qr["id"]}/events').status_code == 401
    assert client.post('/api/activation/result', json={}).status_code == 200


def test_expired_qr_and_regeneration(system):
    service, client, _ = system
    _, qr = prepare_qr(client)
    newer = client.post('/api/qr/create', json={}).json()
    assert client.post(f'/api/qr/{qr["id"]}/confirm', json={'raw': qr['url']}).status_code == 410
    service.clock.advance(300)
    assert client.post(f'/api/qr/{newer["id"]}/confirm', json={'raw': newer['url']}).status_code == 410
    with service.database.transaction() as db:
        assert db.get(QRSession, newer['id']).status == 'EXPIRED'
        assert db.scalar(select(func.count()).select_from(Lease)) == 0
    assert client.post('/api/qr/create', json={}).status_code == 200


def test_expired_and_reused_cdk(system):
    service, client, _ = system
    order = create_order(client)
    service.clock.advance(service.settings.cdk_ttl)
    assert client.post('/api/activation/redeem', json={'code': order['code']}).status_code == 410
    service.clock.value = 1800000000
    order = create_order(client)
    assert client.post('/api/activation/redeem', json={'code': order['code']}).status_code == 200
    assert client.post('/api/activation/redeem', json={'code': order['code']}).status_code == 409


def test_concurrent_cdk_redemption(system):
    service, client, _ = system
    order = create_order(client)
    def redeem(_):
        try:
            with service.database.transaction() as db:
                service.redeem(db, order['code'])
            return 200
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=12) as executor:
        statuses = list(executor.map(redeem, range(12)))
    assert statuses.count(200) == 1 and statuses.count(409) == 11
    with service.database.transaction() as db:
        assert db.scalar(select(CDK)).used_count == 1
        assert db.scalar(select(func.count()).select_from(Activation)) == 1


def test_concurrent_confirmation_and_capacity(system):
    service, client, _ = system
    _, qr = prepare_qr(client)
    with service.database.transaction() as db:
        db.scalar(select(Resource)).capacity = 1
    def confirm(_):
        with service.database.transaction() as db:
            return service.confirm(db, qr['id'], qr['url']).id
    with ThreadPoolExecutor(max_workers=12) as executor:
        ids = list(executor.map(confirm, range(12)))
    assert len(set(ids)) == 1
    with service.database.transaction() as db:
        assert db.scalar(select(Resource)).active_leases == 1
    _, other = prepare_qr(client)
    response = client.post(f'/api/qr/{other["id"]}/confirm', json={'raw': other['url']})
    assert response.status_code == 409


def test_admin_revoke_and_replayed_jwt(system):
    service, client, _ = system
    _, _, result = activate(client)
    token = result['access_token']
    assert client.post('/api/admin/login', json={'token': service.settings.admin_token}).status_code == 200
    lease_id = result['lease']['id']
    for _ in range(2):
        assert client.post(f'/api/admin/leases/{lease_id}/revoke', json={}).status_code == 200
    assert client.get('/api/service', headers={'Authorization': 'Bearer ' + token}).status_code == 401
    assert client.post('/api/auth/refresh', json={}).status_code == 401
    with service.database.transaction() as db:
        assert db.scalar(select(Resource)).active_leases == 0
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.event == 'lease.revoked')) == 1


def test_refresh_rotation_revokes_old_access_and_refresh(system):
    service, client, _ = system
    _, _, result = activate(client)
    old_refresh = client.cookies.get('lg_refresh')
    response = client.post('/api/auth/refresh', json={})
    assert response.status_code == 200
    assert client.cookies.get('lg_refresh') != old_refresh
    assert client.get('/api/service', headers={'Authorization': 'Bearer ' + result['access_token']}).status_code == 401
    assert client.get('/api/service', headers={'Authorization': 'Bearer ' + response.json()['access_token']}).status_code == 200
    with service.database.transaction() as db:
        with pytest.raises(HTTPException):
            service.refresh(db, old_refresh)


def test_api_restart_keeps_sessions_and_expiry(system):
    service, client, _ = system
    _, _, result = activate(client)
    cookies = dict(client.cookies)
    restarted = create_app(service.settings, service.database, service.redis, service.clock)
    with TestClient(restarted) as other:
        other.cookies.update(cookies)
        assert other.get('/api/service').status_code == 200
        service.clock.advance(61)
        assert other.get('/api/service').status_code == 401


def test_webhook_100_duplicates_and_replay(system):
    service, client, _ = system
    order_ids = set()
    for i in range(100):
        if i % 30 == 0:
            service.clock.advance(60)
        event = {'external_order_no': 'payment-001', 'buyer_id': 'buyer@example.test', 'product_id': 'test-60s', 'amount': '0.01',
                 'timestamp': service.clock(), 'nonce': secrets.token_urlsafe(18)}
        body = json.dumps(event, separators=(',', ':'))
        headers = {'Content-Type': 'application/json', 'X-Signature': sign(service.settings.webhook_secret, f'{event["timestamp"]}.{event["nonce"]}.{body}')}
        response = client.post('/api/webhooks/order-paid', content=body, headers=headers)
        assert response.status_code == 200, response.text
        order_ids.add(response.json()['order_id'])
    assert len(order_ids) == 1
    assert client.post('/api/webhooks/order-paid', content=body, headers=headers).status_code == 409
    event['nonce'] = secrets.token_urlsafe(18)
    event['buyer_id'] = 'different-buyer@example.test'
    conflict_body = json.dumps(event, separators=(',', ':'))
    conflict_headers = {'Content-Type': 'application/json', 'X-Signature': sign(service.settings.webhook_secret, f'{event["timestamp"]}.{event["nonce"]}.{conflict_body}')}
    assert client.post('/api/webhooks/order-paid', content=conflict_body, headers=conflict_headers).status_code == 409
    with service.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(CDK)) == 1
        assert db.scalar(select(func.count()).select_from(Delivery)) == 1


def test_concurrent_webhook_orders(system):
    service, _, _ = system
    data = {'external_order_no': 'concurrent', 'buyer_id': 'buyer', 'product_id': 'test-60s', 'amount': '0.01'}
    def create(_):
        from sqlalchemy import text
        with service.database.transaction() as db:
            if not service.database.sqlite:
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(digest(data['external_order_no'])[:15], 16)})
            order, payload = service.create_paid_order(db, data)
            return order.id
    with ThreadPoolExecutor(max_workers=10) as executor:
        ids = list(executor.map(create, range(100)))
    assert len(set(ids)) == 1


def test_upload_actual_qr_and_file_signatures(system):
    _, client, _ = system
    _, qr = prepare_qr(client)
    image = qrcode.make(qr['url'])
    png = io.BytesIO();image.save(png, format='PNG')
    response = client.post('/api/qr/decode-image', files={'image': ('qr.png', png.getvalue(), 'image/png')})
    assert response.status_code == 200, response.text
    assert response.json()['metadata']['qr_id'] == qr['id']
    assert client.post('/api/qr/decode-image', files={'image': ('qr.jpg', png.getvalue(), 'image/jpeg')}).status_code == 415
    assert client.post('/api/qr/decode-image', files={'image': ('x.png', b'<html>fake</html>', 'image/png')}).status_code == 422
    assert client.post('/api/qr/decode-image', files={'image': ('x.svg', b'<svg/>', 'image/svg+xml')}).status_code == 415


def test_24h_lease_short_access_token(system):
    service, client, _ = system
    order = client.post('/api/demo/orders', json={'product_id': 'service-24h'}).json()
    client.post('/api/activation/redeem', json={'code': order['code']})
    qr = client.post('/api/qr/create', json={}).json()
    client.post(f'/api/qr/{qr["id"]}/confirm', json={'raw': qr['url']})
    result = client.post('/api/activation/result', json={}).json()
    claims = jwt.decode(result['access_token'], options={'verify_signature': False})
    assert result['lease']['expire_at'] - result['lease']['activated_at'] == 86400
    assert claims['exp'] - claims['iat'] == 900
    service.clock.advance(900)
    assert client.get('/api/service').status_code == 401
    assert client.post('/api/auth/refresh', json={}).status_code == 200
    assert client.get('/api/service').status_code == 200


def test_scoped_token_and_jti_validation(system):
    service, client, _ = system
    _, _, result = activate(client)
    claims = jwt.decode(result['access_token'], options={'verify_signature':False})
    claims['scope'] = ['admin:write']
    token = jwt.encode(claims, service.settings.signing_key, algorithm='HS256')
    assert client.get('/api/service', headers={'Authorization': 'Bearer ' + token}).status_code == 403
    claims['scope']=['service:read'];claims['jti']='forged'
    token=jwt.encode(claims,service.settings.signing_key,algorithm='HS256')
    assert client.get('/api/service',headers={'Authorization':'Bearer '+token}).status_code==401


def test_secrets_not_saved_plaintext_or_admin_exported(system):
    service, client, _ = system
    order, qr, _ = activate(client)
    with service.database.transaction() as db:
        assert db.scalar(select(CDK)).code_hash == digest(order['code'])
        for audit in db.scalars(select(Audit)):
            assert order['code'] not in audit.detail and qr['url'] not in audit.detail
        assert db.scalar(select(Delivery)).payload_encrypted is None
    client.post('/api/admin/login', json={'token':service.settings.admin_token})
    for collection in ['orders','cdks','leases','resources','authorization-tasks','qr-sessions','audit']:
        response=client.get('/api/admin/'+collection)
        assert response.status_code==200 and 'secret_hash' not in response.text and 'code_hash' not in response.text


def test_csrf_cors_admin_and_rate_limits(system):
    _, client, _ = system
    assert client.post('/api/demo/orders',json={},headers={'X-CSRF-Token':''}).status_code==403
    assert client.post('/api/demo/orders',json={},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.get('/api/admin/orders').status_code==401
    response=client.get('/api/csrf')
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
    assert "script-src 'self'" in response.headers['content-security-policy']
    statuses=[client.post('/api/qr/parse',json={'raw':'garbage'}).status_code for _ in range(45)]
    assert 429 in statuses


@pytest.mark.parametrize('raw',[ 'javascript:alert(1)', '{"issuer":"https://evil.test","type":"activation","code":"x"}',
                               'https://localhost@evil.test/scan/x', 'https://su.quark.cn/4_eMHBJ?token=abcd&client_id=532'])
def test_invalid_qr_formats(system,raw):
    _,client,_=system
    assert client.post('/api/qr/parse',json={'raw':raw}).status_code in (403,422)


def test_domain_spoof_and_unsupported_provider(system):
    _,client,_=system
    raw='https://su.quark.cn.evil.test/4_eMHBJ?token=abcdefgh&client_id=532'
    assert client.post('/api/qr/parse',json={'raw':raw}).json()['provider']=='unsupported'
    order=create_order(client);client.post('/api/activation/redeem',json={'code':order['code']})
    assert client.post('/api/providers/quark_reference/authorize',json={}).status_code==409


def test_tampered_qr_signature(system):
    _,client,_=system
    _,qr=prepare_qr(client)
    raw=qr['url'][:-1]+('0' if qr['url'][-1]!='0' else '1')
    assert client.post(f'/api/qr/{qr["id"]}/confirm',json={'raw':raw}).status_code==403


def test_webhook_bad_signature_and_stale_timestamp(system):
    service,client,_=system
    event={'external_order_no':'bad','buyer_id':'b','product_id':'test-60s','amount':'0.01','timestamp':service.clock(),'nonce':secrets.token_urlsafe(18)}
    assert client.post('/api/webhooks/order-paid',json=event).status_code==403
    event['timestamp']-=301
    assert client.post('/api/webhooks/order-paid',json=event).status_code==403


def test_upload_size_limit(system):
    _,client,_=system
    assert client.post('/api/qr/decode-image',files={'image':('big.png',b'x'*(5*1024*1024+1),'image/png')}).status_code==413
