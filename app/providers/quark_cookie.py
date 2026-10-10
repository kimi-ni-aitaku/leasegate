"""Explicitly authorized, read-only account checks. Never confirms login QR codes."""
import html
import os
import re
import tempfile
from pathlib import Path
import httpx
from fastapi import HTTPException

PURPOSE = 'quark-owner-cookie-v1'


def parse_cookie(raw, partial=False):
    raw = html.unescape(raw).replace('\\_', '_').strip()
    if not raw or len(raw) > 16384 or any(ord(c) < 32 or ord(c) > 126 for c in raw):
        raise HTTPException(422, 'invalid_cookie_format')
    cookies, skipped = {}, 0
    for entry in raw.split(';'):
        if not entry.strip():
            continue
        name, sep, value = entry.strip().partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not value or re.search(r'[\s"\\,;*<>]', value):
            if partial:
                skipped += 1
                continue
            raise HTTPException(422, 'cookie_contains_markup_or_invalid_entries')
        if name in cookies and cookies[name] != value:
            raise HTTPException(422, 'conflicting_duplicate_cookie')
        cookies[name] = value
    if not cookies:
        raise HTTPException(422, 'no_valid_cookie_entries')
    return {'cookie': '; '.join(f'{k}={v}' for k, v in cookies.items()),
            'entry_count': len(cookies), 'partial': bool(skipped), 'skipped_entries': skipped}


class CookieStore:
    def __init__(self, path, vault):
        self.path, self.vault = Path(path), vault

    def save(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(self.vault.encrypt(record, PURPOSE))
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def load(self):
        try:
            return self.vault.decrypt(self.path.read_text(), PURPOSE)
        except FileNotFoundError:
            raise HTTPException(404, 'quark_cookie_not_configured') from None
        except Exception:
            raise HTTPException(503, 'quark_cookie_storage_unavailable') from None

    def status(self):
        if not self.path.exists():
            return {'configured': False, 'qr_confirmation_supported': False}
        record = self.load()
        return {'configured': True, 'entry_count': record['entry_count'], 'partial': record['partial'],
                'skipped_entries': record['skipped_entries'], 'qr_confirmation_supported': False}

    def clear(self):
        self.path.unlink(missing_ok=True)


def check_account(record, transport=None):
    result = {'authenticated': False, 'account_check': 'unverified', 'membership_check': 'unverified',
              'partial_cookie': record['partial'], 'qr_confirmation_supported': False}
    # Fixed destinations, no redirects, no ambient proxies; credentials never reach QR URLs.
    try:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False, transport=transport,
                          headers={'Cookie': record['cookie'], 'Origin': 'https://pan.quark.cn',
                                   'Referer': 'https://pan.quark.cn/', 'User-Agent': 'Mozilla/5.0'}) as client:
            for field, url, params in (
                ('account_check', 'https://pan.quark.cn/account/info', {'fr': 'pc', 'platform': 'pc'}),
                ('membership_check', 'https://drive-member-h.quark.cn/1/clouddrive/member',
                 {'pr': 'ucpro', 'fr': 'pc', 'uc_param_str': '', 'fetch_subscribe': 'false', '_ch': 'home', 'fetch_identity': 'false'}),
            ):
                with client.stream('GET', url, params=params) as response:
                    if 300 <= response.status_code < 400:
                        result[field] = 'redirect_rejected'
                        continue
                    if response.status_code != 200:
                        result[field] = f'http_{response.status_code}'
                        continue
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 262144:
                            raise ValueError('oversized_response')
                    import json
                    data = json.loads(body)
                    payload = data.get('data')
                    successful_envelope = data.get('success') is True if 'success' in data else data.get('status') == 200
                    success = successful_envelope and isinstance(payload, dict) and bool(payload)
                    code = data.get('code')
                    if isinstance(code, int) or isinstance(code, str) and re.fullmatch(r'[A-Z0-9_:.-]{1,60}', code):
                        result[field + '_code'] = code
                    if field == 'account_check':
                        success = success and bool(payload.get('user_info', {}).get('uid') or payload.get('uid')
                                                   or isinstance(payload.get('mobilekps'), str) and payload['mobilekps'])
                    else:
                        success = success and ('member_type' in payload or 'total_capacity' in payload)
                    result[field] = 'ok' if success else 'not_authenticated_or_unrecognized_response'
                    if success:
                        result['authenticated'] = True
                    if field == 'membership_check' and success:
                        # Preserve the type code; do not infer paid membership from undocumented codes.
                        member_type = payload.get('member_type')
                        if isinstance(member_type, (str, int)) and len(str(member_type)) <= 40:
                            result['member_type'] = member_type
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        result['network_or_response_error'] = True
    return result
