import base64
from pathlib import Path
from typing import Literal
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    environment: Literal['development', 'test', 'production'] = 'development'
    database_url: str = 'sqlite:///./leasegate.db'
    redis_url: str = 'redis://localhost:6379/0'
    public_origin: str = 'http://localhost'
    cors_origins: str = 'http://localhost'
    signing_key: str
    encryption_key: str
    webhook_secret: str
    admin_token: str
    cookie_secure: bool = False
    qr_ttl: int = 300
    cdk_ttl: int = 86400
    access_ttl: int = 900
    demo_enabled: bool = True
    image_max_bytes: int = 5 * 1024 * 1024
    delivery_url: str = ''
    delivery_secret: str = ''
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_from: str = ''
    oauth_issuer: str = ''
    oauth_device_endpoint: str = ''
    oauth_token_endpoint: str = ''
    oauth_revocation_endpoint: str = ''
    oauth_client_id: str = ''
    oauth_client_secret: str = ''
    oauth_scope: str = ''
    quark_cookie_file: str = '.private/quark-cookie.enc'
    quark_manual_confirmation_enabled: bool = False

    @model_validator(mode='after')
    def validate_security(self):
        from urllib.parse import urlsplit
        for value in (self.signing_key, self.webhook_secret, self.admin_token):
            if len(value) < 32:
                raise ValueError('Security keys must contain at least 32 characters')
        if len(base64.urlsafe_b64decode(self.encryption_key)) != 32:
            raise ValueError('ENCRYPTION_KEY must encode 32 bytes')
        origin = urlsplit(self.public_origin)
        if origin.scheme not in ('http', 'https') or not origin.netloc or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('', '/'):
            raise ValueError('PUBLIC_ORIGIN must be an origin')
        self.public_origin = self.public_origin.rstrip('/')
        origins = [v.strip().rstrip('/') for v in self.cors_origins.split(',')]
        for value in origins:
            allowed = urlsplit(value)
            if allowed.scheme not in ('http', 'https') or not allowed.netloc or allowed.username or allowed.password or allowed.path or allowed.query or allowed.fragment:
                raise ValueError('CORS_ORIGINS requires explicit valid origins')
        self.cors_origins = ','.join(origins)
        if self.environment == 'production':
            if self.demo_enabled or not self.cookie_secure or not self.public_origin.startswith('https://'):
                raise ValueError('Production requires HTTPS, secure cookies and DEMO_ENABLED=false')
            if self.database_url.startswith('sqlite'):
                raise ValueError('Production requires PostgreSQL')
            if not (self.delivery_url or self.smtp_host):
                raise ValueError('Production requires a buyer delivery channel')
        if self.delivery_url and (not self.delivery_url.startswith('https://') or len(self.delivery_secret) < 32):
            raise ValueError('Delivery callback requires HTTPS and a strong shared secret')
        return self


def load_settings():
    runtime = Path('/run/leasegate/runtime.env')
    try:
        return Settings(_env_file=runtime if runtime.exists() else '.env')
    except Exception:
        raise RuntimeError('invalid_server_configuration; sensitive configuration details suppressed') from None
