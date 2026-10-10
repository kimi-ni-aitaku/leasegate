import httpx
from sqlalchemy import select
from app.main import create_app
from app.models import AuthorizationTask, Lease, ProviderRevocation, Delivery
from app.providers.oauth import OAuthProvider
from app.worker import process_oauth, process_revocations, dispatch_deliveries
from fastapi.testclient import TestClient
from conftest import create_order


def test_official_device_grant_pending_slowdown_exchange_revoke(system):
    service, client, _ = system
    calls, errors = [], ['authorization_pending','slow_down']
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/device':
            return httpx.Response(200,json={'device_code':'server-only-device-code','user_code':'ABCD-EFGH','verification_uri':'https://official.example/verify',
                'verification_uri_complete':'https://official.example/verify?user_code=ABCD-EFGH','expires_in':600,'interval':5})
        if request.url.path == '/token':
            if errors:
                return httpx.Response(400,json={'error':errors.pop(0)})
            return httpx.Response(200,json={'access_token':'third-party-secret-access','refresh_token':'third-party-secret-refresh','token_type':'Bearer'})
        return httpx.Response(200,json={})
    settings=service.settings.model_copy(update={'oauth_issuer':'https://official.example','oauth_device_endpoint':'https://official.example/device',
        'oauth_token_endpoint':'https://official.example/token','oauth_revocation_endpoint':'https://official.example/revoke','oauth_client_id':'authorized-client'})
    upstream=httpx.Client(transport=httpx.MockTransport(handler))
    app=create_app(settings,service.database,service.redis,service.clock,upstream)
    worker=app.state.service
    with TestClient(app) as buyer:
        buyer.headers['X-CSRF-Token']=buyer.get('/api/csrf').json()['token']
        order=create_order(buyer);buyer.post('/api/activation/redeem',json={'code':order['code']})
        local=buyer.post('/api/qr/create',json={}).json()
        response=buyer.post('/api/providers/oauth/authorize',json={})
        assert response.status_code==200,response.text
        assert 'device_code' not in response.text and 'access_token' not in response.text
        pending=buyer.post('/api/activation/result',json={})
        assert pending.json()['authorization']['user_code']=='ABCD-EFGH'
        assert 'server-only-device-code' not in pending.text
        assert buyer.post(f'/api/qr/{local["id"]}/confirm',json={'raw':local['url']}).status_code==410
        worker.clock.advance(5);process_oauth(worker)
        worker.clock.advance(5);process_oauth(worker)
        with worker.database.transaction() as db:
            task=db.get(AuthorizationTask,response.json()['task_id'])
            assert task.poll_interval==10 and 'server-only-device-code' not in task.credentials_encrypted
        worker.clock.advance(9);process_oauth(worker)
        assert calls.count('/token')==2
        worker.clock.advance(1);process_oauth(worker)
        result=buyer.post('/api/activation/result', json={})
        assert result.status_code==200,result.text
        assert 'third-party-secret' not in result.text
        lease_id=result.json()['lease']['id']
        assert buyer.get('/api/service').status_code==200
        worker.clock.advance(60);worker.expire_due();process_revocations(worker)
        assert '/revoke' in calls
        with worker.database.transaction() as db:
            assert db.get(Lease,lease_id).provider_credentials is None
            job=db.get(ProviderRevocation,lease_id)
            assert job.status=='revoked' and job.credentials_encrypted is None


def test_delivery_encryption_retry_and_delete_after_success(system, monkeypatch):
    service, _, _ = system
    with service.database.transaction() as db:
        order,payload=service.create_paid_order(db,{'external_order_no':'deliver','buyer_id':'buyer','product_id':'test-60s','amount':'0.01'})
    with service.database.transaction() as db:
        delivery=db.scalar(select(Delivery))
        assert payload['code'] not in delivery.payload_encrypted
    def fail(*args):
        raise ValueError('secret must never be logged')
    monkeypatch.setattr('app.worker.send_delivery',fail)
    dispatch_deliveries(service)
    with service.database.transaction() as db:
        delivery=db.scalar(select(Delivery));assert delivery.attempts==1 and delivery.status=='pending'
    service.clock.advance(3)
    monkeypatch.setattr('app.worker.send_delivery',lambda *args: True)
    dispatch_deliveries(service)
    with service.database.transaction() as db:
        delivery=db.scalar(select(Delivery));assert delivery.status=='sent' and delivery.payload_encrypted is None


def test_oauth_unconfigured_and_untrusted_endpoints(system):
    service,client,_=system
    order=create_order(client);client.post('/api/activation/redeem',json={'code':order['code']})
    assert client.post('/api/providers/oauth/authorize',json={}).status_code==501
    import pytest
    bad=service.settings.model_copy(update={'oauth_issuer':'https://official.example','oauth_device_endpoint':'https://evil.example/device',
                                          'oauth_token_endpoint':'https://official.example/token','oauth_client_id':'client'})
    with pytest.raises(ValueError):
        OAuthProvider(bad,service.parser)
