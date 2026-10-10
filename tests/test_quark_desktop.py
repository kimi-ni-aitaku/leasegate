import json
from urllib.parse import parse_qs
import httpx
import pytest
from fastapi import HTTPException
from app.providers.quark_desktop import QuarkDesktopExchange, login_local_desktop


def test_exchange_uses_verified_web_session_and_fixed_official_destinations():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == 'pan.quark.cn':
            assert request.headers['cookie'] == '__pus=OWNER_COOKIE'
            return httpx.Response(200, json={'success': True, 'data': {
                'mobilekps': 'OWNER_KPS', 'nickname': 'private', 'avatarUri': '//example.com/avatar'}})
        assert str(request.url) == 'https://uop.quark.cn/cas/ajax/acquireAppToken?client_id=386&v=1.2'
        assert 'cookie' not in request.headers
        values = parse_qs(request.content.decode())
        assert values['kps_wg'] == ['OWNER_KPS'] and values['session_device'] == ['GENUINE_DEVICE']
        assert values['client_id'] == ['386']
        return httpx.Response(200, json={'status': 20000, 'data': {'app_token': 'PRIVATE_APP_TOKEN'}})
    result = QuarkDesktopExchange(httpx.MockTransport(handler)).exchange('__pus=OWNER_COOKIE', 'GENUINE_DEVICE')
    assert len(requests) == 2 and result['appToken'] == 'PRIVATE_APP_TOKEN'
    assert result['avatar'] == 'https://example.com/avatar'


def test_expired_cookie_stops_before_desktop_exchange():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'success': False, 'code': 'AUTH_ERROR:50051', 'data': None})
    with pytest.raises(HTTPException, match='quark_web_cookie_expired'):
        QuarkDesktopExchange(httpx.MockTransport(handler)).exchange('__pus=OWNER_COOKIE', 'GENUINE_DEVICE')
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['redirect', 'oversized', 'timeout', 'wrong_business_status'])
def test_failures_do_not_follow_redirects_retry_or_accept_http_success(failure):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == 'pan.quark.cn':
            return httpx.Response(200, json={'success': True, 'data': {'mobilekps': 'PRIVATE_KPS'}})
        if failure == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://attacker.example/steal'})
        if failure == 'oversized':
            return httpx.Response(200, content=b' ' * 262145)
        if failure == 'timeout':
            raise httpx.ReadTimeout('SECRET_BODY_AND_TOKEN')
        return httpx.Response(200, json={'status': 2000000, 'data': {'app_token': 'PRIVATE_APP_TOKEN'}})
    with pytest.raises(HTTPException) as error:
        QuarkDesktopExchange(httpx.MockTransport(handler)).exchange('__pus=OWNER_COOKIE', 'GENUINE_DEVICE')
    assert len(calls) == 2 and 'SECRET' not in error.value.detail
    assert all(r.url.host in ('pan.quark.cn', 'uop.quark.cn') for r in calls)


def test_local_delivery_reads_one_challenge_and_distinguishes_acceptance_from_login():
    paths = []
    class Official:
        def exchange(self, cookie, device):
            assert cookie == 'OWNER_COOKIE' and device == 'GENUINE_DEVICE'
            return {'appToken': 'PRIVATE_APP_TOKEN', 'nickname': 'private', 'avatar': ''}
    def handler(request):
        paths.append(request.url.path)
        assert request.url.host == '127.0.0.1' and request.url.port == 9125
        if request.url.path == '/desktop_info':
            return httpx.Response(200, json={'success': True, 'data': {'isLogin': False}})
        if request.url.path == '/desktop_devicetoken':
            assert paths.count('/desktop_devicetoken') == 1
            return httpx.Response(200, json={'success': True, 'data': {'data': {'sessionDevice': 'GENUINE_DEVICE'}}})
        assert request.url.path == '/desktop_postlogin'
        assert json.loads(request.content)['appToken'] == 'PRIVATE_APP_TOKEN'
        return httpx.Response(200, json={'success': True})
    result = login_local_desktop('OWNER_COOKIE', Official(), httpx.MockTransport(handler))
    assert result == {'exchange_accepted': True, 'client_accepted': True,
                      'desktop_login_verified': False, 'owner_confirmation_pending': True}
    assert 'PRIVATE' not in json.dumps(result)


def test_local_delivery_does_not_replace_existing_desktop_session():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={'success': True, 'data': {'isLogin': True}})
    with pytest.raises(HTTPException, match='existing_desktop_session_will_not_be_replaced'):
        login_local_desktop('OWNER_COOKIE', local_transport=httpx.MockTransport(handler))
    assert paths == ['/desktop_info']
