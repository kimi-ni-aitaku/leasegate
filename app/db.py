from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException


class TerminalState(HTTPException):
    """Persist only deliberate expiry/revocation transitions before returning an error."""


class Database:
    def __init__(self, url):
        self.sqlite = url.startswith('sqlite')
        self.engine = create_engine(url, pool_pre_ping=True, connect_args={'check_same_thread': False, 'timeout': 30} if self.sqlite else {})
        if self.sqlite:
            @event.listens_for(self.engine, 'connect')
            def configure(dbapi, _):
                dbapi.isolation_level = None
                dbapi.execute('PRAGMA foreign_keys=ON')
                dbapi.execute('PRAGMA journal_mode=WAL')

            @event.listens_for(self.engine, 'begin')
            def begin(connection):
                connection.exec_driver_sql('BEGIN IMMEDIATE')
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.factory() as db:
            transaction = db.begin()
            try:
                yield db
            except TerminalState:
                transaction.commit()
                raise
            except BaseException:
                transaction.rollback()
                raise
            else:
                transaction.commit()
