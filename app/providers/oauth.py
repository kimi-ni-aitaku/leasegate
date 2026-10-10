from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException
from .base import ProviderAdapter


class OAuthProvider(ProviderAdapter):
    """RFC 8628. Configure only endpoints for which the operator owns an authorized client."""
    def __init__(self, settings, parser, client=None):
        self.settings, self.parser = settings, parser
        self.client = client or httpx.Client(timeout=10, follow_redirects=False)
        self.configured = bool(settings.oauth_issuer and settings.oauth_client_id and settings.oauth_device_endpoint and settings.oauth_token_endpoint)
        if self.configured:
            issuer = urlsplit(settings.oauth_issuer)
            if issuer.scheme != 'https' or not issuer.netloc or issuer.username or issuer.password:
                raise ValueError('OAuth issuer must use HTTPS without URL credentials')
            for endpoint in (settings.oauth_device_endpoint, settings.oauth_token_endpoint, settings.oauth_revocation_endpoint):
                if endpoint:
                    url = urlsplit(endpoint)
                    if url.scheme != 'https' or url.netloc != issuer.netloc or url.username or url.password or url.fragment:
                        raise ValueError('OAuth endpoints must use configured HTTPS issuer origin')

    def parse_qr(self, raw):
        return self.parser.parse(raw)

    def _post(self, endpoint, data):
        if not self.configured:
            raise HTTPException(501, 'oauth_not_configured')
        auth = (self.settings.oauth_client_id, self.settings.oauth_client_secret) if self.settings.oauth_client_secret else None
        response = self.client.post(endpoint, data={'client_id': self.settings.oauth_client_id, **data}, auth=auth)
        if response.status_code not in (200, 400):
            raise ValueError('oauth_upstream_unavailable')
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('oauth_invalid_response')
        return result

    def create_device_authorization(self):
        data = self._post(self.settings.oauth_device_endpoint, {'scope': self.settings.oauth_scope})
        if data.get('error') or not all(data.get(k) for k in ('device_code', 'user_code', 'verification_uri', 'expires_in')):
            raise ValueError('oauth_invalid_device_response')
        uri = urlsplit(data['verification_uri'])
        if uri.scheme != 'https' or uri.netloc != urlsplit(self.settings.oauth_issuer).netloc or uri.username or uri.password:
            raise ValueError('oauth_untrusted_verification_uri')
        complete = data.get('verification_uri_complete', data['verification_uri'])
        full = urlsplit(complete)
        if full.scheme != 'https' or full.netloc != uri.netloc or full.username or full.password:
            raise ValueError('oauth_untrusted_verification_uri')
        return {**data, 'expires_in': max(1, min(int(data['expires_in']), 1800)), 'interval': max(5, int(data.get('interval', 5))), 'verification_uri_complete': complete}

    def poll_device_authorization(self, device_code):
        return self._post(self.settings.oauth_token_endpoint, {'device_code': device_code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'})

    def create_authorization(self, context):
        return self.create_device_authorization()

    def get_authorization_status(self, context):
        return self.poll_device_authorization(context['device_code'])

    def exchange_authorization(self, context):
        if not context.get('access_token') or context.get('token_type', '').lower() != 'bearer':
            raise ValueError('oauth_invalid_token_response')
        return context

    def revoke_authorization(self, credentials):
        if not self.settings.oauth_revocation_endpoint:
            return {'revoked': False, 'scope': 'local_system', 'reason': 'official_revocation_not_configured'}
        response = self.client.post(self.settings.oauth_revocation_endpoint,
                                    data={'client_id': self.settings.oauth_client_id, 'token': credentials.get('refresh_token') or credentials['access_token']},
                                    auth=(self.settings.oauth_client_id, self.settings.oauth_client_secret) if self.settings.oauth_client_secret else None)
        response.raise_for_status()
        return {'revoked': True}

    def health_check(self):
        return {'healthy': self.configured, 'mode': 'configured_official_device_grant'}
