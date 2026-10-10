from abc import ABC, abstractmethod
from enum import StrEnum


class AuthorizationStateMachine(StrEnum):
    CREATED = 'CREATED'
    WAIT_USER = 'WAIT_USER'
    AUTHORIZED = 'AUTHORIZED'
    EXCHANGED = 'EXCHANGED'
    EXPIRED = 'EXPIRED'
    CANCELLED = 'CANCELLED'
    FAILED = 'FAILED'


class ProviderAdapter(ABC):
    @abstractmethod
    def parse_qr(self, raw): ...
    @abstractmethod
    def create_authorization(self, context): ...
    @abstractmethod
    def get_authorization_status(self, context): ...
    @abstractmethod
    def exchange_authorization(self, context): ...
    @abstractmethod
    def revoke_authorization(self, credentials): ...
    @abstractmethod
    def health_check(self): ...
