"""Observed official Web -> Desktop exchange, independent of QR confirmation."""
import json
import secrets
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException
from .quark_cookie import parse_cookie


def bounded_json(client, method, url, **kwargs):
    try:
        with client.stream(method, url, **kwargs) as response:
            if response.status_code != 200:
                raise HTTPException(502, 'quark_desktop_upstream_unavailable')
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 262144:
                    raise HTTPException(502, 'quark_desktop_invalid_response')
            data = json.loads(body)
            if not isinstance(data, dict):
                raise HTTPException(502, 'quark_desktop_invalid_response')
            return data
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, 'quark_desktop_upstream_unavailable') from None


class QuarkDesktopExchange:
    def __init__(self, transport=None):
        self.transport = transport

    def exchange(self, cookie, session_device):
        cookie = parse_cookie(cookie)['cookie']
        if (not isinstance(session_device, str) or not 8 <= len(session_device) <= 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in session_device)):
            raise HTTPException(422, 'invalid_desktop_device_credential')
        with httpx.Client(timeout=12, trust_env=False, follow_redirects=False,
                          transport=self.transport) as client:
            account = bounded_json(client, 'GET', 'https://pan.quark.cn/account/info',
                params={'fr': 'pc', 'platform': 'pc'},
                headers={'Cookie': cookie, 'Origin': 'https://pan.quark.cn',
                         'Referer': 'https://pan.quark.cn/', 'User-Agent': 'Mozilla/5.0'})
            profile = account.get('data')
            kps = profile.get('mobilekps') if isinstance(profile, dict) else None
            if account.get('success') is not True or not isinstance(kps, str) or not kps:
                raise HTTPException(409, 'quark_web_cookie_expired')
            # No Cookie header at this endpoint, no guessed device values, no automatic retries.
            result = bounded_json(client, 'POST',
                'https://uop.quark.cn/cas/ajax/acquireAppToken?client_id=386&v=1.2',
                data={'kps_wg': kps, 'session_device': session_device, 'v': '1.2', 'client_id': '386',
                      'request_id': secrets.token_hex(16)},
                headers={'Origin': 'https://pan.quark.cn', 'Referer': 'https://pan.quark.cn/',
                         'User-Agent': 'Mozilla/5.0'})
            data = result.get('data')
            token = data.get('app_token') if isinstance(data, dict) else None
            if type(result.get('status')) is not int or result['status'] != 20000 or not isinstance(token, str) or not token:
                raise HTTPException(409, 'quark_desktop_exchange_rejected')
            if len(token) > 16384:
                raise HTTPException(502, 'quark_desktop_invalid_response')
            nickname, avatar = profile.get('nickname') or '', profile.get('avatarUri') or ''
            if not isinstance(nickname, str) or len(nickname) > 200:
                nickname = ''
            if not isinstance(avatar, str) or len(avatar) > 2048:
                avatar = ''
            if avatar.startswith('//'):
                avatar = 'https:' + avatar
            try:
                parts = urlsplit(avatar)
                if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password:
                    avatar = ''
            except ValueError:
                avatar = ''
            # Secret return value is for the originating desktop, never an API diagnostic response or log.
            return {'appToken': token, 'nickname': nickname, 'avatar': avatar}


def login_local_desktop(cookie, official=None, local_transport=None):
    """Administrative diagnostic. Leaves any official confirmation prompt to the owner."""
    official = official or QuarkDesktopExchange()
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False, transport=local_transport) as client:
        for port in (9125, 9126, 9127):
            base = f'http://127.0.0.1:{port}'
            try:
                info = bounded_json(client, 'GET', base + '/desktop_info')
            except HTTPException:
                continue
            state = info.get('data')
            if info.get('success') is not True or not isinstance(state, dict):
                continue
            if state.get('isLogin') is True:
                raise HTTPException(409, 'existing_desktop_session_will_not_be_replaced')
            if state.get('isLogin') is not False:
                continue
            device = bounded_json(client, 'GET', base + '/desktop_devicetoken')
            data = device.get('data')
            data = data.get('data') if isinstance(data, dict) else None
            session = data.get('sessionDevice') if isinstance(data, dict) else None
            if device.get('success') is not True or not isinstance(session, str):
                raise HTTPException(409, 'genuine_desktop_device_required')
            payload = official.exchange(cookie, session)
            # Reading desktop_devicetoken again would refresh the challenge and invalidate this exchange.
            posted = bounded_json(client, 'POST', base + '/desktop_postlogin', json=payload)
            if posted.get('success') is not True:
                raise HTTPException(409, 'desktop_client_rejected_exchange')
            checked = bounded_json(client, 'GET', base + '/desktop_info')
            verified = checked.get('success') is True and isinstance(checked.get('data'), dict) and checked['data'].get('isLogin') is True
            return {'exchange_accepted': True, 'client_accepted': True,
                    'desktop_login_verified': verified, 'owner_confirmation_pending': not verified}
    raise HTTPException(503, 'quark_desktop_client_unavailable')
