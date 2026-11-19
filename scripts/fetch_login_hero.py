#!/usr/bin/env python3
"""Download login hero MP4 for same-origin playback (CDNs often 403 datacenter IPs)."""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEST = os.path.join(ROOT, "static", "media", "login-hero.mp4")
MIN_BYTES = 80_000

# Browser-like headers — required for many stock-video CDNs on CI/Render.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.5",
    "Referer": "https://www.pexels.com/",
}

# Pexels first (construction / heavy equipment); Mixkit often 403 on build hosts.
# Excavator / dump-truck pages only. File names are guesses: if these 404 at build time,
# commit a verified MP4 to static/media/login-hero.mp4 instead (the script never deletes it).
SOURCES = (
    "https://videos.pexels.com/video-files/36298168/36298168-hd_1920_1080_30fps.mp4",
    "https://videos.pexels.com/video-files/35649973/35649973-hd_1920_1080_50fps.mp4",
    "https://videos.pexels.com/video-files/35650014/35650014-hd_1920_1080_50fps.mp4",
    "https://videos.pexels.com/video-files/30331567/30331567-hd_1920_1080_30fps.mp4",
)


def fetch(url: str) -> int:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    if len(data) < MIN_BYTES:
        raise ValueError(f"response too small ({len(data)} bytes)")
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    with open(DEST, "wb") as f:
        f.write(data)
    return len(data)


def main() -> int:
    # Keep a committed/verified file; only download when missing.
    if os.path.isfile(DEST) and os.path.getsize(DEST) >= MIN_BYTES:
        print(f"Using existing {DEST} ({os.path.getsize(DEST)} bytes)")
        return 0
    last: Exception | None = None
    for url in SOURCES:
        try:
            n = fetch(url)
            print(f"Fetched {url} -> {DEST} ({n} bytes)")
            return 0
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            last = e
            print(f"skip {url}: {e}", file=sys.stderr)
    print(
        "WARN: could not fetch login-hero.mp4 on build host (CDN may block datacenter IPs). "
        "Deploy will continue; browsers use Pexels construction fallbacks.",
        file=sys.stderr,
    )
    if last:
        print(f"Last error: {last}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())