#!/bin/sh
# Build time: a throwaway Postgres, demo migrations, the seed posts, the local pipeline over them, then a dump.
# Spec: DESIGN.md §4.10 (option C). Usage: build_seed.sh /path/to/seed.dump
set -eu
OUT="$1"
PG=/tmp/pg-build
rm -rf "$PG" && mkdir -p "$PG"
initdb -D "$PG/data" -U thrift --auth=trust -E UTF8 >/dev/null
pg_ctl -D "$PG/data" -o "-c listen_addresses='' -c unix_socket_directories=$PG" -w start >/dev/null
createdb -h "$PG" -U thrift thriftradar
export DATABASE_URL="postgresql://thrift@/thriftradar?host=$PG"
# Seed posts never use the VLM (their attributes come from the manifest); nothing here can spend.
export VLM_PROVIDER=off SESSION_SECRET=build-only

python -m app.migrate --demo
if [ -f seed/manifest.json ]; then
  python -m scripts.seed_demo
  python -m app.worker --drain
else
  echo "build_seed: no seed/manifest.json; the demo starts with no sample listings"
fi
pg_dump -h "$PG" -U thrift -Fc thriftradar > "$OUT"
pg_ctl -D "$PG/data" -m fast -w stop >/dev/null
rm -rf "$PG"
echo "build_seed: wrote $OUT"
