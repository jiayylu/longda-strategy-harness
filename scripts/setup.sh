#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
npm ci
npm run build
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
