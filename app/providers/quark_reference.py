import logging
import re
import time
from urllib.parse import urlsplit, parse_qs
import httpx
from fastapi import HTTPException
from .base import ProviderAdapter


class UnsupportedProvider(ProviderAdapter):
    message = '该能力需要官方 API 或获得授权的 Provider。'

    def parse_qr(self, raw):
        return {'provider': 'unsupported', 'supported': False}

    def create_authorization(self, context):
        logging.getLogger('leasegate').warning('unsupported_provider: %s', self.message)
        raise HTTPException(501, self.message)

    get_authorization_status = create_authorization
    exchange_authorization = create_authorization
    revoke_authorization = create_authorization

    def health_check(self):
        return {'healthy': False, 'reason': self.message}


class QuarkReferenceProvider(UnsupportedProvider):
    """Observed official mobile confirmation protocol; native signatures are never fabricated."""
    message = '夸克扫码确认需要 App 生成的 sign_wg、kps_wg、vcode；网页 Cookie 不能替代这些参数。'
    confirm_endpoint = 'https://uop.quark.cn/cas/ajax/loginWithKpsAndQrcodeToken'
    poll_endpoint = 'https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken'

    def __init__(self, client=None, clock=None):
        self.client = client
        self.clock = clock or time.time

    def _token(self, raw):
        try:
            url = urlsplit(raw)
            params = parse_qs(url.query, keep_blank_values=True)
        except ValueError:
            raise HTTPException(422, 'invalid_quark_reference_qr') from None
        if (url.scheme, url.netloc, url.path) != ('https', 'su.quark.cn', '/4_eMHBJ') or url.fragment:
            raise HTTPException(422, 'invalid_quark_reference_qr')
        if any(len(v) != 1 for v in params.values()) or params.get('client_id') != ['532']:
            raise HTTPException(422, 'invalid_quark_reference_qr')
        token = params.get('token', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9._~-]{8,512}', token):
            raise HTTPException(422, 'invalid_quark_reference_qr')
        return token

    def parse_qr(self, raw):
        self._token(raw)
        return {'provider': 'quark_reference', 'requires': 'native_app_signed_confirmation'}

    def _request(self, method, endpoint, **kwargs):
        try:
            if self.client is not None:
                response = self.client.request(method, endpoint, follow_redirects=False, **kwargs)
            else:
                with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
                    response = client.request(method, endpoint, **kwargs)
            if response.status_code != 200 or len(response.content) > 262144:
                raise HTTPException(502, 'quark_upstream_unavailable')
            data = response.json()
            if not isinstance(data, dict) or type(data.get('status')) is not int:
                raise HTTPException(502, 'quark_invalid_upstream_response')
            return data
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, 'quark_upstream_unavailable') from None

    def create_authorization(self, context):
        if not all(context.get(k) for k in ('raw', 'sign_wg', 'kps_wg', 'vcode')):
            raise HTTPException(409, 'quark_native_app_signature_required')
        token = self._token(context['raw'])
        for key in ('sign_wg', 'kps_wg'):
            if not isinstance(context[key], str) or not re.fullmatch(r'[A-Za-z0-9_+/=.-]{8,4096}', context[key]):
                raise HTTPException(422, 'invalid_native_app_signature')
        vcode = context['vcode']
        if not isinstance(vcode, int) or isinstance(vcode, bool) or not -30000 <= int(self.clock() * 1000) - vcode <= 120000:
            raise HTTPException(410, 'native_app_signature_expired')
        data = self._request('POST', self.confirm_endpoint,
            data={'client_id': '532', 'v': '1.2', 'request_id': str(int(self.clock() * 1000)),
                  'token': token, 'sign_wg': context['sign_wg'], 'kps_wg': context['kps_wg'], 'vcode': str(vcode)},
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://b.quark.cn'})
        # An accepted confirmation request is distinct from an established buyer session.
        return {'confirmation_accepted': data['status'] == 2000000, 'upstream_status': data['status'],
                'buyer_login_verified': False}

    def get_authorization_status(self, context):
        token = self._token(context['raw'])
        data = self._request('GET', self.poll_endpoint,
            params={'client_id': '532', 'v': '1.2', 'request_id': str(int(self.clock() * 1000)), 'token': token})
        payload = data.get('data')
        members = payload.get('members') if isinstance(payload, dict) else {}
        has_ticket = isinstance(members, dict) and bool(members.get('service_ticket'))
        state = 'AUTHORIZED' if data['status'] == 2000000 and has_ticket else {
            50004001: 'WAIT_USER', 50004002: 'EXPIRED', 50004003: 'FAILED', 50004004: 'CANCELLED'
        }.get(data['status'], 'FAILED')
        # Never expose or persist Service Tickets in this diagnostic bridge.
        return {'state': state, 'upstream_status': data['status'], 'buyer_login_verified': False}

    def health_check(self):
        return {'healthy': False, 'requires': 'native_app_signed_confirmation', 'remote_device_revoke_supported': False}
