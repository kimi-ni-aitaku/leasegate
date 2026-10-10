from urllib.parse import parse_qs
import httpx
import pytest
from fastapi import HTTPException
from app.providers.quark_reference import QuarkReferenceProvider

QR = 'https://su.quark.cn/4_eMHBJ?token=TESTTOKEN123456789&client_id=532&ssb=weblogin'


def test_cookie_cannot_replace_app_signature():
    provider = QuarkReferenceProvider()
    with pytest.raises(HTTPException) as exc:
        provider.create_authorization({'raw': QR, 'cookie': '__pus=valid'})
    assert exc.value.status_code == 409
    assert exc.value.detail == 'quark_native_app_signature_required'


def test_observed_form_protocol_and_ticket_redaction():
    calls = []
    now = 1800000000
    def handler(request):
        calls.append(request)
        if request.method == 'POST':
            assert str(request.url) == QuarkReferenceProvider.confirm_endpoint
            assert request.headers['content-type'] == 'application/x-www-form-urlencoded'
            form = parse_qs(request.content.decode())
            assert form == {'client_id':['532'], 'v':['1.2'], 'request_id':[str(now*1000)],
                            'token':['TESTTOKEN123456789'], 'sign_wg':['MOCKSIGN123'],
                            'kps_wg':['MOCKKPS123'], 'vcode':[str(now*1000)]}
            return httpx.Response(200, json={'status':2000000})
        return httpx.Response(200, json={'status':2000000, 'data':{'members':{'service_ticket':'NEVER_EXPOSE'}}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider=QuarkReferenceProvider(client, lambda:now)
        result=provider.create_authorization({'raw':QR,'sign_wg':'MOCKSIGN123','kps_wg':'MOCKKPS123','vcode':now*1000})
        assert result['confirmation_accepted'] is True and result['buyer_login_verified'] is False
        status=provider.get_authorization_status({'raw':QR})
        assert status['state']=='AUTHORIZED' and 'NEVER_EXPOSE' not in str(status)
        assert len(calls)==2


@pytest.mark.parametrize('raw', [QR.replace('su.quark.cn','attacker.example'), QR+'&client_id=532', QR.replace('client_id=532','client_id=999')])
def test_confirmation_never_sends_credentials_to_arbitrary_qr(raw):
    provider=QuarkReferenceProvider(clock=lambda:1800000000)
    with pytest.raises(HTTPException) as exc:
        provider.create_authorization({'raw':raw,'sign_wg':'MOCKSIGN123','kps_wg':'MOCKKPS123','vcode':1800000000000})
    assert exc.value.status_code==422


def test_expired_native_signature_never_reaches_network():
    provider=QuarkReferenceProvider(clock=lambda:1800000000)
    with pytest.raises(HTTPException) as exc:
        provider.create_authorization({'raw':QR,'sign_wg':'MOCKSIGN123','kps_wg':'MOCKKPS123','vcode':1799999000000})
    assert exc.value.status_code==410


def test_confirmation_requires_admin_and_issues_no_lease(system):
    service, client, app = system
    from app.models import Lease, Audit
    from sqlalchemy import select
    now=service.clock()
    body={'raw':QR,'sign_wg':'MOCKSIGN123','kps_wg':'MOCKKPS123','vcode':now*1000}
    assert client.post('/api/admin/quark-cookie/confirm-qr',json=body).status_code==401
    def handler(request):
        return httpx.Response(200,json={'status':2000000 if request.method=='POST' else 50004001})
    with httpx.Client(transport=httpx.MockTransport(handler)) as upstream:
        service.providers['quark_reference']=QuarkReferenceProvider(upstream,service.clock)
        client.headers['Authorization']='Bearer '+service.settings.admin_token
        response=client.post('/api/admin/quark-cookie/confirm-qr',json=body)
    assert response.status_code==200 and response.json()['poll']['state']=='WAIT_USER'
    assert response.json()['buyer_login_verified'] is False
    assert 'MOCKSIGN' not in response.text and 'MOCKKPS' not in response.text
    with service.database.transaction() as db:
        assert db.scalar(select(Lease)) is None
        audit=db.scalar(select(Audit).where(Audit.event=='quark.confirmation.submitted'))
        assert audit and 'MOCKSIGN' not in audit.detail and 'TESTTOKEN' not in audit.detail
