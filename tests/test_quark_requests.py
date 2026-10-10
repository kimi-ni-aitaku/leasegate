import io
import pytest
import zxingcpp
from PIL import Image
from sqlalchemy import select, func
from app.models import AuthorizationTask, Lease, Audit, Resource, ProviderRevocation

QR = 'https://su.quark.cn/4_eMHBJ?token=BUYERTESTTOKEN123456&client_id=532&ssb=weblogin'


@pytest.fixture(autouse=True)
def manual_mode_for_tests(system):
    system[0].settings.quark_manual_confirmation_enabled = True


def prepare(client, raw=QR):
    order = client.post('/api/demo/orders', json={'product_id':'service-24h'}).json()
    assert client.post('/api/activation/redeem', json={'code':order['code']}).json()['product_id']=='service-24h'
    response = client.post('/api/activation/quark/submit', json={'raw':raw})
    assert response.status_code==200, response.text
    return order, response.json()


def admin(client, service):
    client.headers['Authorization']='Bearer '+service.settings.admin_token


def test_manual_flow_keeps_official_ticket_with_buyer_and_confirmation_is_idempotent(system, monkeypatch):
    service, client, _ = system
    def forbidden(*args, **kwargs):
        raise AssertionError('Manual delivery must not collect official login tickets')
    monkeypatch.setattr(service.providers['quark_reference'], 'get_authorization_status', forbidden)
    order, task = prepare(client)
    assert task['buyer_login_verified'] is False
    assert client.get('/api/service').status_code==401
    assert client.post(f'/api/activation/quark/{task["id"]}/login-reported',json={}).status_code==409
    assert client.get(f'/api/admin/quark-requests/{task["id"]}/image').status_code==401
    assert client.post(f'/api/admin/quark-requests/{task["id"]}/confirmed',json={'phone_confirmed':True}).status_code==401
    admin(client, service)
    response=client.get(f'/api/admin/quark-requests/{task["id"]}/image')
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    decoded=zxingcpp.read_barcode(Image.open(io.BytesIO(response.content)))
    assert decoded and 'BUYERTESTTOKEN123456' in decoded.text and 'client_id=532' in decoded.text
    assert client.post(f'/api/admin/quark-requests/{task["id"]}/confirmed',json={'phone_confirmed':False}).status_code==422
    with service.database.transaction() as db:
        assert db.scalar(select(Lease)) is None
    for _ in range(2):
        response=client.post(f'/api/admin/quark-requests/{task["id"]}/confirmed',json={'phone_confirmed':True})
        assert response.status_code==200 and response.json()['buyer_login_verified'] is False
    result=client.post('/api/activation/result',json={}).json()
    assert result['status']=='active' and result['quark_request']['status']=='OWNER_CONFIRMED'
    assert result['quark_request']['buyer_login_verified'] is False
    assert result['lease']['expire_at']-result['lease']['activated_at']==86400
    assert client.get('/api/service', headers={'Authorization':'Bearer '+result['access_token']}).status_code==200
    assert client.get(f'/api/admin/quark-requests/{task["id"]}/image').status_code==410
    reported=client.post(f'/api/activation/quark/{task["id"]}/login-reported',json={}).json()
    assert reported['buyer_reported_login'] is True and reported['buyer_login_verified'] is False
    with service.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(Lease))==1
        assert db.get(AuthorizationTask,task['id']).credentials_encrypted is None
        assert db.scalar(select(func.sum(Resource.active_leases)))==1
    service.clock.advance(86400)
    assert client.get('/api/service').status_code==401
    service.expire_due()
    with service.database.transaction() as db:
        assert db.get(AuthorizationTask,task['id']).status=='EXPIRED'
        assert db.scalar(select(func.sum(Resource.active_leases)))==0
        assert db.scalar(select(ProviderRevocation)) is None  # Site expiry cannot revoke a Quark session.


def test_replacement_cancels_old_request_even_in_same_second(system):
    service, client, _ = system
    _, first=prepare(client)
    response=client.post('/api/activation/quark/submit',json={'raw':QR.replace('BUYERTEST','SECONDTEST')})
    assert response.status_code==200
    second=response.json()
    result=client.post('/api/activation/result',json={}).json()
    assert result['quark_request']['id']==second['id']
    with service.database.transaction() as db:
        assert db.get(AuthorizationTask,first['id']).status=='CANCELLED'
        assert db.get(AuthorizationTask,first['id']).credentials_encrypted is None
        encrypted=db.get(AuthorizationTask,second['id']).credentials_encrypted
        assert encrypted and 'SECONDTEST' not in encrypted
        assert 'BUYERTEST' not in str([a.detail for a in db.scalars(select(Audit)).all()])
    admin(client,service)
    assert client.post(f'/api/admin/quark-requests/{first["id"]}/confirmed',json={'phone_confirmed':True}).status_code==409


def test_qr_identity_cannot_cross_orders_or_replay_optional_parameters(system):
    service,client,_=system
    _,task=prepare(client)
    assert client.post('/api/activation/quark/submit',json={'raw':QR+'&uc_param_str='}).json()['id']==task['id']
    second=client.post('/api/demo/orders',json={'product_id':'service-24h'}).json()
    client.post('/api/activation/redeem',json={'code':second['code']})
    assert client.post('/api/activation/quark/submit',json={'raw':QR+'&uc_param_str='}).status_code==409
    for endpoint in ['cancel','login-reported']:
        assert client.post(f'/api/activation/quark/{task["id"]}/{endpoint}',json={}).status_code==404


def test_pending_expiry_is_persisted_and_wipes_qr(system):
    service,client,_=system
    _,task=prepare(client)
    service.clock.advance(service.settings.qr_ttl)
    admin(client,service)
    assert client.post(f'/api/admin/quark-requests/{task["id"]}/confirmed',json={'phone_confirmed':True}).status_code==410
    with service.database.transaction() as db:
        assert db.get(AuthorizationTask,task['id']).status=='EXPIRED'
        assert db.get(AuthorizationTask,task['id']).credentials_encrypted is None
        assert db.scalar(select(Lease)) is None
    assert client.post('/api/activation/result',json={}).json()['quark_request']['status']=='EXPIRED'


@pytest.mark.parametrize('raw',[QR.replace('su.quark.cn','example.test'),QR+'&client_id=532',QR.replace('client_id=532','client_id=999')])
def test_untrusted_qr_cannot_create_manual_request(system,raw):
    service,client,_=system
    order=client.post('/api/demo/orders',json={'product_id':'service-24h'}).json()
    client.post('/api/activation/redeem',json={'code':order['code']})
    assert client.post('/api/activation/quark/submit',json={'raw':raw}).status_code==422
    with service.database.transaction() as db:
        assert db.scalar(select(AuthorizationTask)) is None


def test_requires_order_and_pending_flow_cannot_be_overwritten_by_local_qr(system):
    service,client,_=system
    assert client.post('/api/activation/quark/submit',json={'raw':QR}).status_code==401
    _,task=prepare(client)
    assert client.post('/api/qr/create',json={}).status_code==409
    assert client.post('/api/activation/quark/'+task['id']+'/cancel',json={}).json()['status']=='CANCELLED'
    assert client.post('/api/qr/create',json={}).status_code==200


def test_opening_another_tab_does_not_invalidate_buyer_csrf(system):
    _,client,_=system
    first=client.headers['X-CSRF-Token']
    assert client.get('/api/csrf').json()['token']==first
    assert client.post('/api/demo/orders',json={'product_id':'service-24h'}).status_code==200
    client.cookies.set('lg_csrf','tampered.signature',domain='testserver.local',path='/')
    renewed=client.get('/api/csrf').json()['token']
    assert renewed!=first and renewed!='tampered.signature'


def test_automatic_mode_does_not_silently_request_human_confirmation(system):
    service,client,_=system
    service.settings.quark_manual_confirmation_enabled=False
    order=client.post('/api/demo/orders',json={'product_id':'service-24h'}).json()
    client.post('/api/activation/redeem',json={'code':order['code']})
    response=client.post('/api/activation/quark/submit',json={'raw':QR})
    assert response.status_code==409 and response.json()['detail']=='quark_native_app_signature_required'
    assert client.get('/api/config').json()['quark_manual_confirmation_enabled'] is False
    with service.database.transaction() as db:
        assert db.scalar(select(AuthorizationTask)) is None and db.scalar(select(Lease)) is None
