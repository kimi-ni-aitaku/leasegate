import json
import re
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit, parse_qs
from fastapi import HTTPException
from .security import sign
import hmac


@dataclass
class ParsedQR:
    type: str
    raw: str
    provider: str
    token: str | None
    metadata: dict

    def to_dict(self):
        return asdict(self)


class QRParser:
    """Classification is never authorization. Local proof is verified again in a locked transaction."""
    def __init__(self, settings):
        self.settings = settings

    def parse(self, raw):
        if not isinstance(raw, str) or not raw or len(raw) > 8192 or any(ord(c) < 32 for c in raw):
            raise HTTPException(422, 'invalid_qr_format')
        raw = raw.strip()
        if re.fullmatch(r'LG-[A-Za-z0-9_-]{43}', raw):
            return ParsedQR('token', raw, 'local', raw, {'kind': 'cdk'})
        if raw.startswith('{'):
            try:
                data = json.loads(raw)
            except ValueError:
                raise HTTPException(422, 'invalid_json_qr') from None
            if not isinstance(data, dict):
                raise HTTPException(422, 'invalid_json_qr')
            if data.get('issuer') == self.settings.public_origin and data.get('type') == 'activation' and set(data) == {'issuer', 'type', 'code'}:
                parsed = self.parse(data['code'])
                if parsed.metadata.get('kind') != 'cdk':
                    raise HTTPException(422, 'invalid_activation_qr')
                parsed.type, parsed.raw = 'json', raw
                return parsed
            if data.get('type') == 'oauth_device_code' and self.settings.oauth_issuer and data.get('issuer') == self.settings.oauth_issuer:
                if not isinstance(data.get('device_code'), str) or not re.fullmatch(r'[A-Za-z0-9._~-]{8,512}', data['device_code']):
                    raise HTTPException(422, 'invalid_device_code')
                return ParsedQR('oauth_device_code', raw, 'oauth', data['device_code'], {'issuer': data['issuer'], 'requires_server_task': True})
            raise HTTPException(422, 'untrusted_qr_issuer')
        try:
            url = urlsplit(raw)
            origin = urlsplit(self.settings.public_origin)
            _ = url.port
        except ValueError:
            raise HTTPException(422, 'invalid_url_qr') from None
        if url.username or url.password:
            raise HTTPException(422, 'url_credentials_forbidden')
        if url.scheme == 'leasegate' and url.netloc == 'activate' and url.path == '':
            values = self._params(url.query)
            if set(values) == {'code'}:
                parsed = self.parse(values['code'])
                parsed.type, parsed.raw = 'scheme', raw
                return parsed
            raise HTTPException(422, 'invalid_scheme_qr')
        if url.scheme not in ('https', 'http'):
            raise HTTPException(422, 'unsupported_qr_scheme')
        if (url.scheme, url.netloc) == (origin.scheme, origin.netloc):
            if url.path == '/activate' and not url.query:
                values = self._params(url.fragment)
                if set(values) == {'code'}:
                    parsed = self.parse(values['code'])
                    parsed.type, parsed.raw = 'activation', raw
                    return parsed
            if re.fullmatch(r'/scan/[0-9a-f-]{36}', url.path) and not url.query:
                values = self._params(url.fragment)
                if set(values) != {'secret', 'exp', 'sig'} or not re.fullmatch(r'[A-Za-z0-9_-]{43}', values['secret']):
                    raise HTTPException(422, 'invalid_local_qr')
                qr_id = url.path.split('/')[-1]
                try:
                    expires = int(values['exp'])
                except ValueError:
                    raise HTTPException(422, 'invalid_local_qr') from None
                expected = sign(self.settings.signing_key, f'{qr_id}.{values["secret"]}.{expires}')
                if not hmac.compare_digest(expected, values['sig']):
                    raise HTTPException(403, 'invalid_qr_signature')
                return ParsedQR('local_authorization', raw, 'local', values['secret'], {'qr_id': qr_id, 'expire_at': expires, 'signature': values['sig']})
            raise HTTPException(422, 'invalid_local_qr')
        if url.scheme == 'https' and url.netloc == 'su.quark.cn' and url.path == '/4_eMHBJ':
            values = self._params(url.query)
            if values.get('client_id') != '532' or not re.fullmatch(r'[A-Za-z0-9._~-]{8,512}', values.get('token', '')):
                raise HTTPException(422, 'invalid_quark_reference_qr')
            return ParsedQR('url', raw, 'quark_reference', None, {'supported': False})
        return ParsedQR('url', raw, 'unsupported', None, {'supported': False})

    @staticmethod
    def _params(value):
        params = parse_qs(value, keep_blank_values=True)
        if any(len(v) != 1 for v in params.values()):
            raise HTTPException(422, 'duplicate_qr_parameters')
        return {k: v[0] for k, v in params.items()}

    def detect_provider(self, raw):
        return self.parse(raw).provider
