#!/bin/sh
# Every boot: a fresh Postgres in /tmp restored from the seed dump, then the app. Spec: DESIGN.md §4.10 (option C).
# Unix socket only (no TCP), so trust auth is reachable only from inside this container.
set -eu
PG=/tmp/pg
rm -rf "$PG" && mkdir -p "$PG"
initdb -D "$PG/data" -U thrift --auth=trust -E UTF8 >/dev/null
pg_ctl -D "$PG/data" -l "$PG/postgres.log" \
  -o "-c listen_addresses='' -c unix_socket_directories=$PG -c shared_buffers=128MB" -w start >/dev/null
createdb -h "$PG" -U thrift thriftradar
pg_restore -h "$PG" -U thrift -d thriftradar --no-owner /app/seed.dump
export DATABASE_URL="postgresql://thrift@/thriftradar?host=$PG"
cd /app/backend
exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-7860}" --workers 1 --timeout-keep-alive 5
