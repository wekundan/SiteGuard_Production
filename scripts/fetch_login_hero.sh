#!/usr/bin/env bash
# Fetches login hero MP4 into static/media (Mixkit license: free for commercial use).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/static/media/login-hero.mp4"
mkdir -p "$(dirname "$DEST")"
URL="https://assets.mixkit.co/videos/preview/mixkit-excavators-working-on-a-construction-site-4455-hd-ready.mp4"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$DEST" "$URL"
elif command -v wget >/dev/null 2>&1; then
  wget -q -O "$DEST" "$URL"
else
  echo "Need curl or wget to fetch login hero video" >&2
  exit 1
fi
echo "Wrote $DEST ($(wc -c <"$DEST") bytes)"