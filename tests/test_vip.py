"""Business invariants with an explicit test gateway; no real Quark login is claimed."""
import json
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models import VIPAccount, VIPCard, VIPBinding, VIPLoginTask, Audit
from app.providers.vip_gateway import VIPGateway


class TestGateway:
    __test__ = False
    configured = True

    def __init__(self):
        self.target = 'verified-computer-one'
        self.state = 'authorized'
        self.calls = []
        self.login_verified = False
        self.revoke_supported = False
        self.revoked = False
        self.timeout_once = False
        self.unavailable_account = None
        self.inspect_verified = True

    def inspect(self, raw, key):
        if not self.inspect_verified:
            raise HTTPException(409, 'target_device_unverifiable')
        return self.target

    def call(self, operation, payload, key):
        self.calls.append((operation, payload, key))
        if operation == 'check':
            return {'authenticated': True, 'membership_valid': True,
                    'device_limit': 2, 'target_identity_supported': True,
                    'account_subject': payload['credential']}
        if operation == 'revoke':
            return {'revoked': self.revoked}
        if operation == 'authorize' and self.timeout_once:
            self.timeout_once = False
            raise HTTPException(502, 'vip_gateway_unavailable')
        if operation == 'authorize' and payload['account_id'] == self.unavailable_account:
            return {'state': 'account_unavailable'}
        if self.state == 'challenge':
            return {'state': 'challenge', 'kind': 'sms'}
        if self.state != 'authorized':
            return {'state': self.state}
        return {'state': 'authorized', 'target_id': self.target, 'session_ref': 'test-session-reference',
                'login_verified': self.login_verified, 'revoke_supported': self.revoke_supported}


@pytest.fixture
def vip_system(system):
    service, client, app = system
    gateway = TestGateway()
    app.state.vip.gateway = gateway
    client.headers['Authorization'] = 'Bearer ' + service.settings.admin_token
    return service, client, app.state.vip, gateway, app


def account(client, alias='owner-1', credential='test-reference-1', capacity=1):
    response = client.post('/api/admin/vip/accounts/import', json={'accounts': [
        {'alias': alias, 'credential_type': 'gateway_reference', 'credential': credential, 'capacity': capacity}]})
    assert response.status_code == 200, response.text
    record = response.json()['items'][0]
    checked = client.post(f'/api/admin/vip/accounts/{record["id"]}/check', json={})
    assert checked.status_code == 200, checked.text
    assert checked.json()['status'] == 'ready'
    return record


def card(client, **fields):
    response = client.post('/api/admin/vip/cards', json=fields)
    assert response.status_code == 200, response.text
    record = response.json()['items'][0]
    assert client.post('/api/vip/open', json={'code': record['code']}).status_code == 200
    return record


def submit(client, token='QR_TOKEN_NUMBER_ONE'):
    return client.post('/api/vip/submit', json={'raw':
        'https://su.quark.cn/4_eMHBJ?client_id=532&token=' + token})


def task_status(client, task_id):
    return next(t for t in client.get('/api/vip/state').json()['tasks'] if t['id'] == task_id)


def test_missing_gateway_cannot_issue_fake_success_or_charge_card(system):
    service, client, app = system
    client.headers['Authorization'] = 'Bearer ' + service.settings.admin_token
    record = card(client)
    response = submit(client)
    assert response.status_code == 503 and response.json()['detail'] == 'vip_service_unavailable'
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 7
    with service.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(VIPLoginTask)) == 0
        assert db.get(VIPCard, record['id']).activated_at is None


def test_valid_web_cookie_without_gateway_never_becomes_authorization_inventory(system, monkeypatch):
    service, client, app = system
    client.headers['Authorization'] = 'Bearer ' + service.settings.admin_token
    record = client.post('/api/admin/vip/accounts/import', json={'accounts': [
        {'alias': 'owner', 'credential': '__pus=PRIVATE_COOKIE'}]}).json()['items'][0]
    monkeypatch.setattr('app.vip.check_web_cookie', lambda record: {'authenticated': True,
        'account_check': 'ok', 'membership_check': 'ok', 'member_type': 'SUPER_VIP'})
    checked = client.post(f'/api/admin/vip/accounts/{record["id"]}/check', json={})
    assert checked.json()['status'] == 'web_only'
    card(client)
    assert submit(client).status_code == 503
    assert 'PRIVATE_COOKIE' not in checked.text
    with service.database.transaction() as db:
        assert db.get(VIPAccount, record['id']).checked_at == service.clock()
        assert db.scalar(select(func.count()).select_from(VIPBinding)) == 0


def test_credential_rotation_is_encrypted_resets_health_and_preserves_disable(system, monkeypatch):
    service, client, app = system
    client.headers['Authorization'] = 'Bearer ' + service.settings.admin_token
    record = client.post('/api/admin/vip/accounts/import', json={'accounts': [
        {'alias': 'owner', 'credential': '__pus=OLD_PRIVATE_COOKIE'}]}).json()['items'][0]
    path = f'/api/admin/vip/accounts/{record["id"]}'
    client.post(path + '/disable', json={})
    rotated = client.post(path + '/credentials', json={'credential': '__pus=NEW_PRIVATE_COOKIE'})
    assert rotated.status_code == 200 and rotated.json()['status'] == 'disabled'
    assert 'NEW_PRIVATE_COOKIE' not in rotated.text
    monkeypatch.setattr('app.vip.check_web_cookie', lambda record: {'authenticated': True,
        'account_check': 'ok', 'membership_check': 'ok', 'member_type': 'SUPER_VIP'})
    assert client.post(path + '/check', json={}).json()['status'] == 'disabled'
    with service.database.transaction() as db:
        owner = db.get(VIPAccount, record['id'])
        assert 'NEW_PRIVATE_COOKIE' not in owner.credentials_encrypted
        assert app.state.vip.credentials(owner)['credential'] == '__pus=NEW_PRIVATE_COOKIE'
        assert not any('PRIVATE_COOKIE' in a.detail for a in db.scalars(select(Audit)))


def test_credential_rotation_refuses_duplicate_and_occupied_account(vip_system):
    service, client, vip, _, _ = vip_system
    owner = account(client)
    account(client, 'owner-2', 'OTHER_REFERENCE')
    path = f'/api/admin/vip/accounts/{owner["id"]}/credentials'
    assert client.post(path, json={'credential': 'OTHER_REFERENCE'}).status_code == 409
    card(client)
    submit(client)
    vip.tick()
    response = client.post(path, json={'credential': 'REPLACEMENT_REFERENCE'})
    assert response.status_code == 409 and response.json()['detail'] == 'vip_account_has_active_bindings'


def test_card_clock_starts_on_authorization_idempotent_retries_do_not_charge_twice(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    record = card(client, max_scans=2, duration_seconds=60)
    assert client.get('/api/vip/state').json()['card']['expire_at'] is None
    service.clock.advance(15)
    response = submit(client)
    task_id = response.json()['id']
    assert response.status_code == 200
    assert submit(client).json()['id'] == task_id
    vip.tick()
    state = client.get('/api/vip/state').json()
    assert state['card']['remaining_scans'] == 1
    assert state['card']['expire_at'] == service.clock() + 60
    assert task_status(client, task_id)['status'] == 'AUTHORIZED'
    assert task_status(client, task_id)['buyer_login_verified'] is False
    assert submit(client).json()['id'] == task_id
    vip.tick()
    assert len([c for c in gateway.calls if c[0] == 'authorize']) == 1
    second = submit(client, 'QR_TOKEN_NUMBER_TWO').json()['id']
    vip.tick()
    assert task_status(client, second)['status'] == 'AUTHORIZED'
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 0
    assert submit(client, 'QR_TOKEN_NUMBER_THREE').json()['detail'] == 'vip_scan_limit'
    with service.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(VIPBinding)) == 1
        assert db.get(VIPCard, record['id']).used_scans == 2


def test_server_verified_target_rejects_second_computer_before_authorization(vip_system):
    _, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    submit(client)
    vip.tick()
    gateway.target = 'different-verified-computer'
    task_id = submit(client, 'QR_TOKEN_OTHER_COMPUTER').json()['id']
    vip.tick()
    result = task_status(client, task_id)
    assert result['status'] == 'FAILED' and result['error_code'] == 'vip_device_limit'
    assert len([c for c in gateway.calls if c[0] == 'authorize']) == 1
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 6


def test_unknown_target_and_wrong_qr_never_reach_authorize(vip_system):
    _, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    assert client.post('/api/vip/submit', json={'raw': 'https://attacker.invalid/?token=whatever'}).status_code == 422
    gateway.inspect_verified = False
    task_id = submit(client).json()['id']
    vip.tick()
    assert task_status(client, task_id)['error_code'] == 'target_device_unverifiable'
    assert not any(c[0] == 'authorize' for c in gateway.calls)


def test_expiry_keeps_external_capacity_until_revocation_is_confirmed(vip_system):
    service, client, vip, gateway, _ = vip_system
    account_record = account(client)
    record = card(client, duration_seconds=60)
    gateway.revoke_supported = True
    submit(client)
    vip.tick()
    service.clock.advance(60)
    vip.tick()
    assert submit(client, 'AFTER_EXPIRY_TOKEN').status_code == 410
    with service.database.transaction() as db:
        binding = db.scalar(select(VIPBinding))
        assert binding.status == 'remote_unconfirmed'
    gateway.target = 'second-card-computer'
    card(client)
    second = submit(client, 'SECOND_CARD_QR_TOKEN').json()['id']
    vip.tick()
    assert task_status(client, second)['error_code'] == 'vip_inventory_empty'
    gateway.revoked = True
    service.clock.advance(60)
    vip.tick()
    with service.database.transaction() as db:
        binding = db.scalar(select(VIPBinding))
        assert binding.status == 'revoked' and binding.session_encrypted is None
        assert db.get(VIPCard, record['id']).status == 'expired'
    assert client.get('/api/admin/vip/accounts').json()['items'][0]['occupied'] == 0
    third = submit(client, 'THIRD_CARD_QR_TOKEN').json()['id']
    vip.tick()
    assert task_status(client, third)['status'] == 'AUTHORIZED'


def test_no_revoke_capability_does_not_free_live_device_capacity(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    card(client, duration_seconds=60)
    submit(client)
    vip.tick()
    service.clock.advance(60)
    vip.tick()
    with service.database.transaction() as db:
        assert db.scalar(select(VIPBinding)).status == 'remote_unconfirmed'
    assert not any(c[0] == 'revoke' for c in gateway.calls)
    assert client.get('/api/admin/vip/accounts').json()['items'][0]['occupied'] == 1


def test_ambiguous_timeout_is_reconciled_without_duplicate_authorize(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    gateway.timeout_once = True
    task_id = submit(client).json()['id']
    vip.tick()
    assert task_status(client, task_id)['status'] == 'UNCERTAIN'
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 7
    assert submit(client, 'OTHER_REQUEST_TOKEN').json()['detail'] == 'vip_login_pending'
    service.clock.advance(10)
    vip.tick()
    assert task_status(client, task_id)['status'] == 'AUTHORIZED'
    operations = [c[0] for c in gateway.calls if c[0] in ('authorize', 'status')]
    assert operations == ['authorize', 'status']
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 6


def test_sms_challenge_answer_is_encrypted_and_then_erased(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    gateway.state = 'challenge'
    task_id = submit(client).json()['id']
    vip.tick()
    assert task_status(client, task_id)['challenge_kind'] == 'sms'
    response = client.post(f'/api/vip/tasks/{task_id}/answer', json={'answer': '123456'})
    assert response.status_code == 200
    with service.database.transaction() as db:
        task = db.get(VIPLoginTask, task_id)
        assert '123456' not in task.payload_encrypted
        assert vip.vault.decrypt(task.payload_encrypted, 'vip-task:' + task.id)['answer'] == '123456'
    gateway.state = 'authorized'
    gateway.login_verified = True
    vip.tick()
    assert task_status(client, task_id)['buyer_login_verified'] is True
    with service.database.transaction() as db:
        assert db.get(VIPLoginTask, task_id).payload_encrypted is None
        logs = '\n'.join(a.detail for a in db.scalars(select(Audit)).all())
        assert '123456' not in logs
    assert any(c[0] == 'answer' and c[1]['answer'] == '123456' for c in gateway.calls)


def test_definitively_invalid_account_switches_to_next_without_charging_failure(vip_system):
    service, client, vip, gateway, _ = vip_system
    first = account(client, 'a', 'reference-a')
    second = account(client, 'b', 'reference-b')
    gateway.unavailable_account = min(first['id'], second['id'])
    card(client)
    task_id = submit(client).json()['id']
    vip.tick()
    assert task_status(client, task_id)['status'] == 'QUEUED'
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 7
    service.clock.advance(5)
    vip.tick()
    assert task_status(client, task_id)['status'] == 'AUTHORIZED'
    calls = [c for c in gateway.calls if c[0] == 'authorize']
    assert len(calls) == 2 and calls[0][2] != calls[1][2]
    with service.database.transaction() as db:
        assert db.get(VIPAccount, gateway.unavailable_account).status == 'unavailable'
        assert db.scalar(select(func.count()).select_from(VIPBinding).where(VIPBinding.status == 'active')) == 1


def test_batch_is_atomic_duplicates_cannot_increase_capacity_and_secrets_never_return(vip_system):
    service, client, vip, gateway, _ = vip_system
    secret = 'test-private-cookie=private-credential-value'
    payload = {'accounts': [{'alias': 'x', 'credential': secret}, {'alias': 'y', 'credential': secret}]}
    assert client.post('/api/admin/vip/accounts/import', json=payload).status_code == 409
    assert client.get('/api/admin/vip/accounts').json()['items'] == []
    record = client.post('/api/admin/vip/accounts/import', json={'accounts': [payload['accounts'][0]]}).json()['items'][0]
    assert secret not in json.dumps(record)
    assert secret not in client.get('/api/admin/vip/accounts').text
    with service.database.transaction() as db:
        stored = db.get(VIPAccount, record['id'])
        assert secret not in stored.credentials_encrypted
        assert vip.credentials(stored)['credential'] == secret
    assert client.post('/api/admin/vip/accounts/import', json={'accounts': [payload['accounts'][1]]}).status_code == 409


def test_card_sessions_are_isolated_qr_cannot_cross_cards_and_admin_is_required(vip_system):
    service, client, vip, gateway, app = vip_system
    account(client)
    card(client)
    first = submit(client).json()['id']
    with TestClient(app) as other:
        other.headers['X-CSRF-Token'] = other.get('/api/csrf').json()['token']
        assert other.get('/api/admin/vip/accounts').status_code == 401
        assert other.get('/api/vip/state').status_code == 401
        assert other.post(f'/api/vip/tasks/{first}/answer', json={'answer': '123456'}).status_code == 401
        second_card = card(client)
        assert other.post('/api/vip/open', json={'code': second_card['code']}).status_code == 200
        assert submit(other).json()['detail'] == 'vip_qr_already_assigned'
        assert other.post(f'/api/vip/tasks/{first}/answer', json={'answer': '123456'}).status_code == 404


def test_concurrent_workers_claim_once_and_concurrent_cards_cannot_oversell(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    first = submit(client).json()['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(vip.process_task, [first, first]))
    assert len([c for c in gateway.calls if c[0] == 'authorize']) == 1
    card(client)
    second = submit(client, 'SECOND_CARD_CONCURRENT').json()['id']
    card(client)
    third = submit(client, 'THIRD_CARD_CONCURRENT').json()['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(vip.process_task, [second, third]))
    with service.database.transaction() as db:
        assert db.get(VIPLoginTask, second).error_code == 'vip_inventory_empty'
        assert db.get(VIPLoginTask, third).error_code == 'vip_inventory_empty'
        assert db.scalar(select(func.count()).select_from(VIPBinding).where(VIPBinding.status.in_(('active', 'reserved')))) == 1


def test_disable_blocks_new_authorization_and_ready_check_cannot_undo_disable(vip_system):
    _, client, vip, gateway, _ = vip_system
    record = account(client)
    assert client.post(f'/api/admin/vip/accounts/{record["id"]}/disable', json={}).status_code == 200
    assert client.post(f'/api/admin/vip/accounts/{record["id"]}/check', json={}).json()['status'] == 'disabled'
    activation = card(client)
    assert client.post(f'/api/admin/vip/cards/{activation["id"]}/disable', json={}).status_code == 200
    assert submit(client).json()['detail'] == 'vip_card_disabled'
    assert not any(c[0] == 'authorize' for c in gateway.calls)


def test_mismatched_authorization_target_never_reports_success_and_keeps_capacity(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    task_id = submit(client).json()['id']
    original = gateway.call
    def changed(operation, payload, key):
        result = original(operation, payload, key)
        if operation == 'authorize':
            result['target_id'] = 'unexpected-third-computer'
        return result
    gateway.call = changed
    vip.tick()
    assert task_status(client, task_id)['status'] == 'UNCERTAIN'
    assert client.get('/api/vip/state').json()['card']['remaining_scans'] == 7
    with service.database.transaction() as db:
        assert db.scalar(select(VIPBinding)).status == 'reserved'


def test_gateway_fixed_https_no_redirect_and_verified_device_required(vip_system):
    service, _, _, _, _ = vip_system
    service.settings.vip_gateway_url = 'https://owner-gateway.invalid/quark'
    service.settings.vip_gateway_secret = 's' * 48
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/inspect'):
            return httpx.Response(200, json={'target_kind':'desktop', 'target_id':'verified-computer', 'target_verified':False})
        return httpx.Response(302, headers={'Location':'https://attacker.invalid'})
    gateway = VIPGateway(service.settings, httpx.MockTransport(handler))
    with pytest.raises(HTTPException) as err:
        gateway.inspect('test-qr', 'key-1')
    assert err.value.detail == 'target_device_unverifiable'
    with pytest.raises(HTTPException):
        gateway.call('check', {'credential': 'test-private-credential'}, 'key-2')
    assert len(requests) == 2
    assert all(str(r.url).startswith('https://owner-gateway.invalid/quark/') for r in requests)
    assert requests[-1].headers['Idempotency-Key'] == 'key-2'
    assert requests[-1].headers['Authorization'] == 'Bearer ' + 's' * 48


def test_reordered_cookies_and_second_credential_for_same_subject_cannot_duplicate_stock(vip_system):
    _, client, vip, gateway, _ = vip_system
    payload = {'accounts': [{'alias': 'first-cookie', 'credential': 'a=one; b=two'}]}
    assert client.post('/api/admin/vip/accounts/import', json=payload).status_code == 200
    payload['accounts'][0] = {'alias': 'reordered', 'credential': 'b=two; a=one'}
    assert client.post('/api/admin/vip/accounts/import', json=payload).status_code == 409
    original = gateway.call
    def same_owner(operation, value, key):
        result = original(operation, value, key)
        if operation == 'check':
            result['account_subject'] = 'same-verified-owner'
        return result
    gateway.call = same_owner
    account(client, 'first-reference', 'fresh-reference-1')
    second = client.post('/api/admin/vip/accounts/import', json={'accounts': [
        {'alias': 'other-reference', 'credential_type': 'gateway_reference', 'credential': 'fresh-reference-2'}]}).json()['items'][0]
    assert client.post(f'/api/admin/vip/accounts/{second["id"]}/check', json={}).json()['status'] == 'unavailable'


def test_late_confirmation_is_cleaned_up_without_starting_card_or_charging(vip_system):
    service, client, vip, gateway, _ = vip_system
    account(client)
    record = card(client)
    gateway.timeout_once = True
    task_id = submit(client).json()['id']
    vip.tick()
    service.clock.advance(service.settings.qr_ttl + 1)
    vip.tick()
    result = task_status(client, task_id)
    assert result['status'] == 'FAILED' and result['error_code'] == 'vip_qr_expired'
    with service.database.transaction() as db:
        assert db.get(VIPCard, record['id']).activated_at is None
        assert db.get(VIPCard, record['id']).used_scans == 0
        binding = db.scalar(select(VIPBinding))
        assert binding.status == 'revoke_pending' and binding.session_encrypted is not None
    vip.tick()
    with service.database.transaction() as db:
        assert db.scalar(select(VIPBinding)).status == 'remote_unconfirmed'


def test_latest_status_remains_correct_for_multiple_requests_in_the_same_second(vip_system, monkeypatch):
    _, client, vip, gateway, _ = vip_system
    account(client)
    card(client)
    ids = iter(['ffffffff-ffff-ffff-ffff-ffffffffffff',
                '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000'])
    monkeypatch.setattr('app.vip.uid', lambda: next(ids))
    first = submit(client).json()['id']
    vip.tick()
    gateway.state = 'rejected'
    latest = submit(client, 'SECOND_SAME_SECOND_QR').json()['id']
    vip.tick()
    tasks = client.get('/api/vip/state').json()['tasks']
    assert tasks[0]['id'] == latest and tasks[0]['status'] == 'FAILED'
    assert tasks[1]['id'] == first and tasks[1]['status'] == 'AUTHORIZED'
