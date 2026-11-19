#!/usr/bin/env bash
# Wrapper: use Python fetcher (browser UA + Pexels fallbacks). Mixkit 403s on many build hosts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/scripts/fetch_login_hero.py"