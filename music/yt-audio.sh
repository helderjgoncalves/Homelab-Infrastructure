#!/bin/bash
# Best original audio for a YouTube URL, or any URL yt-dlp supports.
# Opus is copied to .opus. AAC is kept as .m4a. No second encode.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="$here/.venv"
if [[ ! -x "$venv/bin/python" ]]; then
  python3 -m venv "$venv"
  "$venv/bin/pip" install -q mutagen
fi
exec "$venv/bin/python" "$here/yt-audio.py" "$@"
