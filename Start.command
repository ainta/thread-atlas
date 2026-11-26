#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then exec uv run --frozen python -m atlas "$@"; fi
if python3 -c 'import aiohttp' 2>/dev/null; then exec python3 -m atlas "$@"; fi
python3 -m venv .venv
.venv/bin/python -m pip install 'aiohttp>=3.10,<4'
exec .venv/bin/python -m atlas "$@"
