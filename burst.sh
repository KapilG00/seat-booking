#!/usr/bin/env sh
# One-command on-sale stampede: ./burst.sh <BASE_URL> [extra burst.py flags]
# Admin token comes from $ADMIN_TOKEN (defaults to the local dev token).
set -eu
URL="${1:-http://localhost:8000}"
if [ "$#" -gt 0 ]; then shift; fi
exec uv run "$(dirname "$0")/scripts/burst.py" "$URL" "$@"
