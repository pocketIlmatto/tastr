#!/bin/sh
# Starts a hosted Tastr (docs/deploy.md). With LITESTREAM_BUCKET set, a new volume first gets the library
# back from R2, and the server then runs under Litestream, which copies every change to R2 within seconds.
set -e
DB="$TASTR_HOME/tastr.db"
SERVE="tastr serve --host 0.0.0.0 --port 8080"
if [ -n "$LITESTREAM_BUCKET" ]; then
  litestream restore -if-db-not-exists -if-replica-exists "$DB"
  exec litestream replicate -exec "$SERVE"
fi
echo "LITESTREAM_BUCKET is not set, so the library is not backed up." >&2
exec $SERVE
