#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] running alembic upgrade head"
alembic upgrade head

echo "[entrypoint] launching: $*"
exec "$@"
