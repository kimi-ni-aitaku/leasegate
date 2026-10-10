import json
import os
import httpx
import pytest
from fastapi import HTTPException
from app.providers.quark_cookie import CookieStore, parse_cookie, check_account


def test_parser_refuses_ambiguous_names_and_conflicts():
    with pytest.raises(HTTPException):
        parse_cookie('**&#x70;us=secret; \\_\\_puus=valid')
    partial = parse_cookie('**&#x70;us=secret; \\_\\_puus=valid', partial=True)
    assert partial == {'cookie': '__puus=valid', 'entry_count': 1, 'partial': True, 'skipped_entries': 1}
    assert parse_cookie('__pus=valid; __pus=valid')['entry_count'] == 1
    with pytest.raises(HTTPException):
        parse_cookie('__pus=one; __pus=two')
    with pytest.raises(HTTPException):
        parse_cookie('__pus=one\r\nHost: attacker')


def test_store_encryption_and_admin_boundary(system, tmp_path, monkeypatch):
    service, client, app = system
    path = tmp_path / 'private' / 'cookie.enc'
    service.settings.quark_cookie_file = str(path)
    # create_app captures the path; directly exercise storage plus API with an isolated app.
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(service.settings, service.database, service.redis, service.clock)) as c:
        c.headers['X-CSRF-Token'] = c.get('/api/csrf').json()['token']
        assert c.get('/api/admin/quark-cookie/status').status_code == 401
        assert c.post('/api/admin/quark-cookie/import', json={'cookie': '__pus=secret'}).status_code == 401
        c.headers['Authorization'] = 'Bearer ' + service.settings.admin_token
        response = c.post('/api/admin/quark-cookie/import', json={'cookie': '__pus=secret'})
        assert response.status_code == 200
        assert 'secret' not in response.text
        assert 'secret' not in path.read_text()
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert CookieStore(path, service.vault).load()['cookie'] == '__pus=secret'
        monkeypatch.setattr('app.main.check_account', lambda record: {'authenticated': True})
        assert c.post('/api/admin/quark-cookie/check', json={}).json()['authenticated']
        assert c.post('/api/admin/quark-cookie/clear', json={}).json()['configured'] is False
        assert not path.exists()


def test_fixed_destinations_no_redirects_and_response_redaction():
    visited = []
    def handler(request):
        visited.append(str(request.url))
        assert request.headers['cookie'] == '__pus=secret'
        if request.url.host == 'pan.quark.cn':
            return httpx.Response(302, headers={'location': 'https://attacker.example/steal'})
        return httpx.Response(200, json={'status': 200, 'data': {'member_type': 'VIP', 'total_capacity': 100,
            'cookie': 'DO_NOT_EXPOSE', 'uid': 'private-id'}})
    result = check_account(parse_cookie('__pus=secret'), httpx.MockTransport(handler))
    assert len(visited) == 2
    assert all('attacker' not in url for url in visited)
    assert result['account_check'] == 'redirect_rejected'
    assert result['membership_check'] == 'ok'
    assert result['authenticated'] and result['member_type'] == 'VIP'
    assert 'private-id' not in json.dumps(result) and 'DO_NOT_EXPOSE' not in json.dumps(result)
    assert result['qr_confirmation_supported'] is False


def test_empty_account_cannot_count_as_login():
    result = check_account(parse_cookie('__pus=secret'), httpx.MockTransport(
        lambda r: httpx.Response(200, json={'status': 200, 'data': {}})))
    assert not result['authenticated']


def test_successful_anonymous_envelope_cannot_count_as_login():
    def handler(request):
        if request.url.host == 'pan.quark.cn':
            return httpx.Response(200, json={'success': True, 'code': 'OK', 'data': None})
        return httpx.Response(401, json={'status': 401, 'code': 31001})
    result = check_account(parse_cookie('__pus=secret'), httpx.MockTransport(handler))
    assert not result['authenticated']
    assert result['account_check'] == 'not_authenticated_or_unrecognized_response'


def test_current_account_envelope_and_authentication_error_codes():
    def handler(request):
        if request.url.host=='pan.quark.cn':
            return httpx.Response(200,json={'success':True,'data':{'uid':'PRIVATE_UID'},'code':'OK'})
        return httpx.Response(401,json={'status':401,'code':31004})
    result=check_account(parse_cookie('__pus=secret'),httpx.MockTransport(handler))
    assert result['authenticated'] and result['account_check']=='ok'
    assert 'PRIVATE_UID' not in json.dumps(result)
    def invalid(request):
        return httpx.Response(200,json={'success':False,'status':200,'data':{'uid':'PRIVATE_UID'},'code':'AUTH_ERROR:50051'})
    result=check_account(parse_cookie('__pus=secret'),httpx.MockTransport(invalid))
    assert not result['authenticated'] and result['account_check_code']=='AUTH_ERROR:50051'


def test_observed_phone_login_profile_and_membership_host():
    def handler(request):
        if request.url.host == 'pan.quark.cn':
            return httpx.Response(200, json={'success': True, 'code': 'OK', 'data': {
                'nickname': 'PRIVATE_PROFILE', 'avatarUri': 'PRIVATE_AVATAR',
                'mobilekps': 'PRIVATE_CREDENTIAL', 'config': {}}})
        assert request.url.host == 'drive-member-h.quark.cn'
        return httpx.Response(200, json={'status': 200, 'code': 0, 'data': {'member_type': 'SUPER_VIP'}})
    result = check_account(parse_cookie('__pus=secret'), httpx.MockTransport(handler))
    assert result['account_check'] == 'ok' and result['membership_check'] == 'ok'
    assert result['authenticated'] and result['member_type'] == 'SUPER_VIP'
    assert 'PRIVATE_' not in json.dumps(result)
