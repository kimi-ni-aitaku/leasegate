import io
import json
from urllib.parse import urlencode
import pytest
from pydantic import ValidationError
import qrcode
from sqlalchemy import select
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.models import ProviderRevocation, Lease
from app.worker import process_revocations, send_delivery
from app.security import sign
from conftest import create_order, prepare_qr


def test_show_admin_direct_script_works_without_pythonpath(tmp_path):
    import base64
    import os
    import subprocess
    import sys
    from pathlib import Path
    # Isolated fake configuration: never read or print the operator's real credentials.
    expected = 'test-admin-token-' + 'a' * 40
    env = {'PATH': os.environ.get('PATH', ''), 'ENVIRONMENT': 'test',
           'ADMIN_TOKEN': expected, 'SIGNING_KEY': 's' * 48, 'WEBHOOK_SECRET': 'w' * 48,
           'ENCRYPTION_KEY': base64.urlsafe_b64encode(b'e' * 32).decode()}
    script = Path(__file__).resolve().parents[1] / 'scripts/show_admin.py'
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert result.stdout.strip() == expected
    assert result.stderr == ''


def test_token_json_scheme_and_activation_qr_formats(system):
    service,client,_=system
    order=create_order(client);code=order['code']
    for raw,expected in [(code,'token'),(json.dumps({'issuer':service.settings.public_origin,'type':'activation','code':code}),'json'),
                         ('leasegate://activate?'+urlencode({'code':code}),'scheme'),(order['activation_url'],'activation')]:
        result=client.post('/api/qr/parse',json={'raw':raw})
        assert result.status_code==200 and result.json()['type']==expected and result.json()['token']==code
    assert client.post('/api/qr/parse',json={'raw':'leasegate://activate?'+urlencode({'code':code})+'&code=other'}).status_code==422


@pytest.mark.parametrize('format,mime',[('JPEG','image/jpeg'),('WEBP','image/webp')])
def test_jpeg_webp_fallback_decoding(system,format,mime):
    _,client,_=system
    _,qr=prepare_qr(client)
    image=qrcode.make(qr['url']).convert('RGB');buffer=io.BytesIO();image.save(buffer,format=format,quality=100)
    response=client.post('/api/qr/decode-image',files={'image':('qr.image',buffer.getvalue(),mime)})
    assert response.status_code==200,response.text
    assert response.json()['raw']==qr['url']


def test_production_configuration_enforces_security(system):
    service,_,_=system
    base=service.settings.model_dump()
    for changes in [{'environment':'production'},{'cors_origins':'*'},{'public_origin':'http://user:password@localhost'}, {'environment':'unrecognized'}]:
        with pytest.raises(ValidationError):Settings(_env_file=None,**{**base,**changes})


def test_production_rate_limit_redis_failure_closed(system):
    service,_,_=system
    settings=service.settings.model_copy(update={'environment':'production'})
    app=create_app(settings,service.database,service.redis,service.clock)
    # fakeredis without Lua raises RedisError for EVAL, simulating unavailable atomic limiter.
    with TestClient(app) as client:
        client.headers['X-CSRF-Token']=client.get('/api/csrf').json()['token']
        assert client.post('/api/qr/parse',json={'raw':'x'}).status_code==503
        assert client.get('/api/service').status_code==401


def test_smtp_starttls_and_recipient_validation(system,monkeypatch):
    service,_,_=system
    service.settings=service.settings.model_copy(update={'smtp_host':'smtp.example','smtp_from':'orders@example.test','smtp_user':'u','smtp_password':'p'})
    calls=[]
    class SMTP:
        def __init__(self,*args,**kwargs):calls.append('connect')
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def starttls(self):calls.append('starttls')
        def login(self,*args):calls.append('login')
        def send_message(self,message):calls.append('send');assert message['To']=='buyer@example.test'
    monkeypatch.setattr('app.worker.smtplib.SMTP',SMTP)
    payload={'buyer_id':'buyer@example.test','code':'secret','activation_url':'https://example.test/activate'}
    assert send_delivery(service,payload,'delivery-id') is True
    assert calls==['connect','starttls','login','send']
    with pytest.raises(ValueError):send_delivery(service,{**payload,'buyer_id':'buyer\r\nBcc:other@example.test'},'id')


def test_remote_revoke_retry_cannot_reactivate_local_lease(system):
    service,client,_=system
    from conftest import activate
    _,_,result=activate(client);lease_id=result['lease']['id']
    with service.database.transaction() as db:
        lease=db.get(Lease,lease_id);lease.provider='oauth';lease.provider_credentials=service.vault.encrypt({'access_token':'secret'},'lease:'+lease_id)
    class Adapter:
        attempts=0
        def revoke_authorization(self,credentials):
            self.attempts+=1
            if self.attempts==1:raise ValueError('upstream timeout')
            return {'revoked':True}
    service.providers['oauth']=Adapter();service.clock.advance(60);service.expire_due();process_revocations(service)
    assert client.get('/api/service').status_code==401
    with service.database.transaction() as db:
        assert db.get(ProviderRevocation,lease_id).status=='pending'
        assert db.get(Lease,lease_id).provider_credentials is None
    service.clock.advance(3);process_revocations(service);process_revocations(service)
    with service.database.transaction() as db:
        assert db.get(ProviderRevocation,lease_id).status=='revoked'
    assert service.providers['oauth'].attempts==2
