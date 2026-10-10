#!/bin/sh
set -eu
if [ "${1:-}" = "api" ]; then
  alembic upgrade head
  exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port 8000 --no-access-log
fi
exec python -m app.worker
