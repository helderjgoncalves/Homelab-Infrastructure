#!/usr/bin/env python3
"""Download the best original audio stream for a YouTube (or yt-dlp) URL.

Opus stays Opus, copied into .opus. AAC stays AAC, in .m4a.
Nothing is re-encoded.
"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

YT_DLP = "yt-dlp"
FFMPEG = "ffmpeg"


def run(cmd):
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def die(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def metadata(url):
    result = run([YT_DLP, "--skip-download", "--dump-single-json", "-f", "ba", url])
    if result.returncode != 0 or not result.stdout.strip():
        die(result.stderr.strip() or f"yt-dlp could not read {url}")
    return json.loads(result.stdout)


def clean_title(title):
    title = re.sub(r"\s+", " ", title or "").strip()
    return re.sub(r'[\\/:*?"<>|]', "-", title)


def split_artist_title(title):
    if " - " in title:
        artist, track = title.split(" - ", 1)
        artist, track = artist.strip(), track.strip()
        if artist and track:
            return artist, track
    return None, title.strip()


def year_of(value):
    text = str(value or "")
    if len(text) >= 4 and text[:4].isdigit() and text.startswith("20"):
        return text[:4]
    return None


def date_of(info):
    raw = str(info.get("release_date") or "")
    if len(raw) == 8 and raw.isdigit() and raw.startswith("20"):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) >= 10 and raw[4] == "-" and raw.startswith("20"):
        return raw[:10]
    uploaded = str(info.get("upload_date") or "")
    if len(uploaded) == 8 and uploaded.isdigit():
        return f"{uploaded[:4]}-{uploaded[4:6]}-{uploaded[6:8]}"
    return year_of(info.get("release_year")) or ""


def artists_of(info, fallback):
    raw = info.get("artist") or info.get("creator") or fallback or ""
    parts = []
    for piece in re.split(r"\s*,\s*", raw):
        piece = piece.strip()
        if piece and piece not in parts:
            parts.append(piece)
    return parts


def tags_for(info):
    video_title = (info.get("title") or info.get("id") or "audio").strip()
    parsed_artist, parsed_title = split_artist_title(video_title)
    track = (info.get("track") or parsed_title or video_title).strip()
    artists = artists_of(info, parsed_artist)
    album = (info.get("album") or "").strip() or track
    when = date_of(info)
    return {
        "filename": clean_title(video_title),
        "artists": artists or ["Unknown"],
        "albumartist": (info.get("album_artist") or (artists[0] if artists else parsed_artist or "Unknown")),
        "title": track,
        "album": album,
        "date": when,
        "year": when[:4] if when else "",
    }


def download(url, folder):
    folder.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            YT_DLP,
            "--force-overwrites",
            "-f", "ba",
            "--no-mtime",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "-o", str(folder / "%(id)s.%(ext)s"),
            url,
        ]
    )
    if result.returncode != 0:
        die(f"download failed: {url}")


def remux(source, dest):
    result = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(source), "-c", "copy", str(dest)])
    if result.returncode != 0:
        die(f"could not remux {source.name}")


def cover_bytes(folder, video_id):
    jpg = folder / f"{video_id}.jpg"
    if jpg.exists():
        return jpg.read_bytes()
    webp = folder / f"{video_id}.webp"
    if not webp.exists():
        return b""
    converted = folder / f"{video_id}.jpg"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(webp), str(converted)], check=False)
    return converted.read_bytes() if converted.exists() else b""


def tag_opus(path, tags, cover):
    audio = OggOpus(path)
    audio["artist"] = [", ".join(tags["artists"])]
    audio["albumartist"] = [tags["albumartist"]]
    audio["title"] = [tags["title"]]
    audio["album"] = [tags["album"]]
    if tags["date"]:
        audio["date"] = [tags["date"]]
        audio["year"] = [tags["year"]]
    if cover:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


def tag_m4a(path, tags, cover):
    audio = MP4(path)
    audio["\xa9ART"] = [", ".join(tags["artists"])]
    audio["aART"] = [tags["albumartist"]]
    audio["\xa9nam"] = [tags["title"]]
    audio["\xa9alb"] = [tags["album"]]
    if tags["date"]:
        audio["\xa9day"] = [tags["date"]]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def cleanup(folder, video_id, keep):
    for path in folder.glob(f"{video_id}.*"):
        if path != keep:
            path.unlink(missing_ok=True)


def one(url, folder):
    info = metadata(url)
    video_id = info["id"]
    tags = tags_for(info)
    download(url, folder)
    webm = folder / f"{video_id}.webm"
    m4a = folder / f"{video_id}.m4a"
    cover = cover_bytes(folder, video_id)
    if webm.exists():
        dest = folder / f"{tags['filename']}.opus"
        remux(webm, folder / f"{video_id}.opus")
        source = folder / f"{video_id}.opus"
        tag_opus(source, tags, cover)
        source.replace(dest)
        cleanup(folder, video_id, dest)
        kind = "opus"
    elif m4a.exists():
        dest = folder / f"{tags['filename']}.m4a"
        tag_m4a(m4a, tags, cover)
        m4a.replace(dest)
        cleanup(folder, video_id, dest)
        kind = "aac"
    else:
        die(f"no audio file for {video_id}")
    print(f"{kind}\t{tags['artists'][0]} — {tags['title']}\t{dest}")


def main():
    args = [arg for arg in sys.argv[1:] if arg != "--"]
    folder = Path.cwd()
    if args and args[0] == "-o":
        if len(args) < 2:
            die(f"usage: {Path(sys.argv[0]).name} [-o DIR] URL [URL...]")
        folder = Path(args[1]).expanduser()
        args = args[2:]
    if not args:
        die(f"usage: {Path(sys.argv[0]).name} [-o DIR] URL [URL...]")
    for url in args:
        one(url, folder)


if __name__ == "__main__":
    main()
