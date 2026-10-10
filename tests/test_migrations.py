import os
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, inspect
from app.models import Base


def test_frozen_migration_upgrade_downgrade_matches_models(system,tmp_path,monkeypatch):
    service,_,_=system
    url=os.getenv('MIGRATION_DATABASE_URL','sqlite:///'+str(tmp_path/'migration.db'))
    if not url.startswith('sqlite') and not url.split('?')[0].endswith('_test'):
        raise RuntimeError('Use a dedicated migration database ending in _test')
    for key in ['signing_key','encryption_key','webhook_secret','admin_token']:
        monkeypatch.setenv(key.upper(),getattr(service.settings,key))
    monkeypatch.setenv('DATABASE_URL',url)
    config=Config('alembic.ini')
    command.upgrade(config,'head')
    engine=create_engine(url)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    command.downgrade(config,'base')
    assert inspect(engine).get_table_names()==['alembic_version']
    command.upgrade(config,'head')
    assert set(Base.metadata.tables)<=set(inspect(engine).get_table_names())
    engine.dispose()
