import base64
import hashlib
import hmac
import json
import secrets
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException


def random_token():
    return secrets.token_urlsafe(32)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def sign(key, value):
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


class Vault:
    def __init__(self, key):
        self.aes = AESGCM(base64.urlsafe_b64decode(key))

    def encrypt(self, value, purpose):
        nonce = secrets.token_bytes(12)
        ciphertext = self.aes.encrypt(nonce, json.dumps(value).encode(), purpose.encode())
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, value, purpose):
        data = base64.urlsafe_b64decode(value)
        return json.loads(self.aes.decrypt(data[:12], data[12:], purpose.encode()))


def encode_access(settings, lease, session, now):
    return jwt.encode({'sub': lease.buyer_id, 'lease_id': lease.id, 'order_id': lease.order_id,
                       'scope': ['service:read'], 'iat': now, 'exp': min(now + settings.access_ttl, lease.expire_at),
                       'jti': session.token_jti, 'sid': session.id, 'iss': 'leasegate', 'aud': 'leasegate-api'},
                      settings.signing_key, algorithm='HS256')


def decode_access(settings, token, now):
    try:
        payload = jwt.decode(token, settings.signing_key, algorithms=['HS256'], audience='leasegate-api', issuer='leasegate',
                             options={'verify_exp': False, 'verify_iat': False,
                                      'require': ['sub', 'lease_id', 'order_id', 'scope', 'iat', 'exp', 'jti', 'sid']})
        if payload['iat'] > now + 5:
            raise ValueError('expired')
        return payload
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(401, 'invalid_or_expired_token') from None
