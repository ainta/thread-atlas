#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -n "${ATLAS_RUNTIME:-}" ]; then
  mkdir -p "$ATLAS_RUNTIME"
  export UV_PROJECT_ENVIRONMENT="$ATLAS_RUNTIME"
fi
if command -v uv >/dev/null 2>&1; then
  uv sync --frozen
  exec "${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python" -m atlas "$@"
fi
if python3 -c 'import aiohttp' 2>/dev/null; then exec python3 -m atlas "$@"; fi
runtime=${ATLAS_RUNTIME:-.venv}
python3 -m venv "$runtime"
"$runtime/bin/python" -m pip install 'aiohttp>=3.10,<4'
exec "$runtime/bin/python" -m atlas "$@"
