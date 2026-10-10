from .base import ProviderAdapter


class LocalProvider(ProviderAdapter):
    """All authorization mutations are implemented by Service under database row locks."""
    def __init__(self, parser):
        self.parser = parser

    def parse_qr(self, raw):
        parsed = self.parser.parse(raw)
        if parsed.provider != 'local':
            raise ValueError('provider_mismatch')
        return parsed

    def create_authorization(self, context):
        return {'state': 'WAIT_USER', 'qr_session_id': context['qr_session_id']}

    def get_authorization_status(self, context):
        return context['status']

    def exchange_authorization(self, context):
        if context['status'] != 'CONFIRMED':
            raise ValueError('confirmation_required')
        return {'resource_scope': 'service:read'}

    def revoke_authorization(self, credentials):
        return {'revoked': True, 'scope': 'local_system'}

    def health_check(self):
        return {'healthy': True}
