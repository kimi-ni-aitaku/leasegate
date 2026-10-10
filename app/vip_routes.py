from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, func
from .models import VIPAccount, VIPCard, VIPBinding, VIPLoginTask
from .vip import VIPService, OCCUPIED


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AccountInput(Input):
    alias: str = Field(min_length=1, max_length=80)
    credential_type: Literal['cookie', 'gateway_reference'] = 'cookie'
    credential: SecretStr = Field(min_length=1, max_length=16384)
    capacity: int = Field(strict=True, ge=1, le=100, default=1)

    @field_validator('alias')
    @classmethod
    def clean_alias(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('invalid_alias')
        return value.strip()


class AccountBatch(Input):
    accounts: list[AccountInput] = Field(min_length=1, max_length=50)


class CardBatch(Input):
    count: int = Field(strict=True, ge=1, le=100, default=1)
    label: str = Field(min_length=1, max_length=80, default='24 小时电脑授权')
    duration_seconds: int = Field(strict=True, ge=60, le=2592000, default=86400)
    valid_seconds: int = Field(strict=True, ge=60, le=2592000, default=604800)
    max_scans: int = Field(strict=True, ge=1, le=1000, default=7)
    max_devices: int = Field(strict=True, ge=1, le=10, default=1)


class OpenCard(Input):
    code: str = Field(pattern=r'^VG-[A-Za-z0-9_-]{43}$')


class SubmitQR(Input):
    raw: SecretStr = Field(min_length=1, max_length=8192)


class Answer(Input):
    answer: SecretStr = Field(min_length=1, max_length=256)


class CredentialUpdate(Input):
    credential: SecretStr = Field(min_length=1, max_length=16384)


def install_vip(app, service, admin, set_cookie):
    vip = VIPService(service)
    app.state.vip = vip
    router = APIRouter()

    @router.get('/api/vip/config')
    def config():
        return {'available': vip.gateway.configured, 'notice': service.settings.vip_notice,
                'image_max_bytes': service.settings.image_max_bytes}

    @router.post('/api/vip/open')
    def open_card(data: OpenCard, response: Response):
        card, secret, expires = vip.open_card(data.code)
        set_cookie(response, 'lg_vip', secret, service.settings, expires - service.clock())
        return {'card': card, 'server_time': service.clock()}

    @router.get('/api/vip/state')
    def state(request: Request):
        return vip.snapshot(request.cookies.get('lg_vip'))

    @router.post('/api/vip/logout')
    def logout(response: Response):
        response.delete_cookie('lg_vip', path='/')
        return {'status': 'ok'}

    @router.post('/api/vip/submit')
    def submit(data: SubmitQR, request: Request):
        return vip.submit(request.cookies.get('lg_vip'), data.raw.get_secret_value())

    @router.post('/api/vip/tasks/{task_id}/answer')
    def answer(task_id: str, data: Answer, request: Request):
        return vip.answer(request.cookies.get('lg_vip'), task_id, data.answer.get_secret_value())

    def require_admin(request: Request):
        admin(request)

    operations = APIRouter(prefix='/api/admin/vip', dependencies=[Depends(require_admin)])

    @operations.get('/accounts')
    def accounts(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise HTTPException(422, 'invalid_offset')
        with service.database.transaction() as db:
            rows = db.scalars(select(VIPAccount).order_by(VIPAccount.created_at.desc(), VIPAccount.id)
                              .offset(offset).limit(100)).all()
            result = []
            for account in rows:
                occupied = db.scalar(select(func.count()).select_from(VIPBinding).where(
                    VIPBinding.account_id == account.id, VIPBinding.status.in_(OCCUPIED)))
                result.append({**vip.account_view(account), 'occupied': occupied})
            return {'items': result, 'gateway_configured': vip.gateway.configured}

    @operations.post('/accounts/import')
    def import_accounts(data: AccountBatch):
        return {'items': vip.import_accounts(data.accounts)}

    @operations.post('/accounts/{account_id}/check')
    def check(account_id: str):
        return vip.check_account(account_id)

    @operations.post('/accounts/{account_id}/credentials')
    def update_credentials(account_id: str, data: CredentialUpdate):
        return vip.update_credentials(account_id, data.credential)

    @operations.post('/accounts/{account_id}/disable')
    def disable_account(account_id: str):
        with service.database.transaction() as db:
            account = db.scalar(select(VIPAccount).where(VIPAccount.id == account_id).with_for_update())
            if not account:
                raise HTTPException(404, 'vip_account_not_found')
            account.status = 'disabled'
            vip.audit(db, 'account.disabled', account.id, actor='admin')
            return vip.account_view(account)

    @operations.get('/cards')
    def cards(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise HTTPException(422, 'invalid_offset')
        with service.database.transaction() as db:
            rows = db.scalars(select(VIPCard).order_by(VIPCard.created_at.desc(), VIPCard.id)
                              .offset(offset).limit(100)).all()
            return {'items': [vip.card_view(c) for c in rows]}

    @operations.post('/cards')
    def create_cards(data: CardBatch):
        return {'items': vip.create_cards(data)}

    @operations.post('/cards/{card_id}/disable')
    def disable_card(card_id: str):
        with service.database.transaction() as db:
            card = db.scalar(select(VIPCard).where(VIPCard.id == card_id).with_for_update())
            if not card:
                raise HTTPException(404, 'vip_card_not_found')
            card.status = 'disabled'
            vip.audit(db, 'card.disabled', card.id, actor='admin')
            return vip.card_view(card)

    @operations.get('/tasks')
    def tasks(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise HTTPException(422, 'invalid_offset')
        with service.database.transaction() as db:
            rows = db.scalars(select(VIPLoginTask).order_by(VIPLoginTask.created_at.desc(), VIPLoginTask.id)
                              .offset(offset).limit(100)).all()
            return {'items': [vip.task_view(t) | {'card_id': t.card_id, 'binding_id': t.binding_id} for t in rows]}

    @operations.get('/bindings')
    def bindings(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise HTTPException(422, 'invalid_offset')
        with service.database.transaction() as db:
            rows = db.scalars(select(VIPBinding).order_by(VIPBinding.created_at.desc(), VIPBinding.id)
                              .offset(offset).limit(100)).all()
            return {'items': [{k: getattr(b, k) for k in ('id', 'card_id', 'account_id', 'status',
                'revoke_supported', 'created_at')} for b in rows]}

    app.include_router(router)
    app.include_router(operations)
    static = Path(__file__).parent / 'static'

    @app.get('/qr')
    def buyer_page():
        return FileResponse(static / 'vip.html')

    @app.get('/vip-admin')
    def admin_page():
        return FileResponse(static / 'vip_admin.html')
