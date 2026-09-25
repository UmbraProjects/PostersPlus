#!/bin/sh
set -e

# Fix ownership of the cache volume mount so appuser can read/write it.
# This runs as root before we drop privileges — necessary because Docker
# creates the host-side directory as root when the volume is first mounted.
# Only what is not already appuser's is touched: a recursive chown rewrote the
# inode of every cached image on each start, which took a while on a large cache.
mkdir -p /app/cache/tmdb_posters /app/cache/tmdb_logos
find /app/cache \( ! -user appuser -o ! -group appuser \) -exec chown appuser:appuser {} +

# WORKERS is the one setting Python cannot apply to itself — uvicorn spawns the
# processes before main.py runs — so honour the admin dashboard's saved value
# here, with the same precedence as every other setting: file, then env.
SETTINGS_FILE="${SETTINGS_PATH:-/app/cache/settings.json}"
if [ -f "$SETTINGS_FILE" ]; then
  FILE_WORKERS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('WORKERS',''))" "$SETTINGS_FILE" 2>/dev/null || true)
  case "$FILE_WORKERS" in
    ''|*[!0-9]*) ;;            # unset or not a number: keep the environment's value
    *) WORKERS="$FILE_WORKERS" ;;
  esac
fi

# Drop from root to appuser and exec uvicorn.
# gosu correctly transfers signals (SIGTERM etc.) to the child process,
# unlike 'su -c' which leaves an extra shell in the process tree.
exec gosu appuser uvicorn main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS:-1}"