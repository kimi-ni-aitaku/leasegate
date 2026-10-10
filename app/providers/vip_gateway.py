"""Fixed, operator-owned authorization gateway. This module does not generate native signatures."""
import httpx
from fastapi import HTTPException


class VIPGateway:
    def __init__(self, settings, transport=None):
        self.url, self.secret = settings.vip_gateway_url, settings.vip_gateway_secret
        self.transport = transport

    @property
    def configured(self):
        return bool(self.url and self.secret)

    def call(self, operation, payload, key):
        if not self.configured:
            raise HTTPException(503, 'vip_service_unavailable')
        if operation not in ('check', 'inspect', 'authorize', 'status', 'answer', 'revoke'):
            raise ValueError('invalid_gateway_operation')
        try:
            with httpx.Client(timeout=12, follow_redirects=False, trust_env=False,
                              transport=self.transport) as client:
                with client.stream('POST', self.url + '/' + operation, json=payload,
                                   headers={'Authorization': 'Bearer ' + self.secret,
                                            'Idempotency-Key': key}) as response:
                    if response.status_code != 200:
                        raise HTTPException(502, 'vip_gateway_unavailable')
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise HTTPException(502, 'vip_gateway_invalid_response')
                    import json
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise HTTPException(502, 'vip_gateway_invalid_response')
                    return data
        except (httpx.HTTPError, ValueError, TypeError):
            # Exception strings can contain request bodies, OTPs or credentials.
            raise HTTPException(502, 'vip_gateway_unavailable') from None

    def inspect(self, raw, key):
        result = self.call('inspect', {'provider': 'quark', 'raw': raw}, key)
        target = result.get('target_id')
        if (result.get('target_verified') is not True or result.get('target_kind') != 'desktop'
                or not isinstance(target, str) or not 8 <= len(target) <= 512
                or any(ord(c) < 32 for c in target)):
            raise HTTPException(409, 'target_device_unverifiable')
        return target
