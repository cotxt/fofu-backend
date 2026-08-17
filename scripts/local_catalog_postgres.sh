#!/bin/sh
set -eu

FOFU_PG_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FOFU_PG_BIN=${FOFU_LOCAL_PG_BIN:-/usr/local/opt/postgresql@17/bin}
FOFU_PG_DATA="$FOFU_PG_ROOT/.local-postgres/17/data"
FOFU_PG_SOCKET="$FOFU_PG_ROOT/.local-postgres/17/socket"
FOFU_PG_LOG="$FOFU_PG_ROOT/.local-postgres/17/postgres.log"
FOFU_PG_PORT=55432

case "${1:-}" in
  start)
    mkdir -p "$FOFU_PG_SOCKET"
    chmod 0700 "$FOFU_PG_SOCKET"
    "$FOFU_PG_BIN/pg_ctl" \
      -D "$FOFU_PG_DATA" \
      -l "$FOFU_PG_LOG" \
      -o "-p $FOFU_PG_PORT -k $FOFU_PG_SOCKET -c listen_addresses= -c unix_socket_permissions=0700" \
      -w -t 30 start
    ;;
  stop)
    "$FOFU_PG_BIN/pg_ctl" -D "$FOFU_PG_DATA" -w -t 30 -m fast stop
    ;;
  status)
    "$FOFU_PG_BIN/pg_ctl" -D "$FOFU_PG_DATA" status
    ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
