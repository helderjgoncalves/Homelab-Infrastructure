"""Version rules. No Docker and no mail."""

from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SEMVER = re.compile(r"\d+(?:\.\d+){1,5}")
MINOR_WAIT = timedelta(days=7)
USER_AGENT = "homelab-updates"


@dataclass(frozen=True)
class Release:
    version: tuple[int, ...]
    tag: str
    published: datetime | None
    prerelease: bool = False
    draft: bool = False


def parse_version(value: str) -> tuple[int, ...] | None:
    text = value.strip()
    if text.startswith("v"):
        text = text[1:]
    if not SEMVER.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def newer(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    size = max(len(left), len(right))
    return _pad(left, size) > _pad(right, size)


def _pad(version: tuple[int, ...], size: int) -> tuple[int, ...]:
    return version + (0,) * (size - len(version))


def choose(current: str, releases: list[Release], now: datetime) -> tuple[str | None, Release | None]:
    """Return (tag to install, minor that is still inside its week).

    A x.y.0 release is eligible a week after it was published. Any patch,
    including a patch of a newer minor, is eligible immediately. Other majors
    are ignored.
    """
    current_version = parse_version(current)
    if current_version is None:
        return None, None
    best: Release | None = None
    waiting: Release | None = None
    for release in releases:
        if release.prerelease or release.draft or not release.version:
            continue
        if release.version[0] != current_version[0] or not newer(release.version, current_version):
            continue
        if _eligible(current_version, release, now):
            if best is None or newer(release.version, best.version):
                best = release
        elif _is_new_minor(current_version, release) and (waiting is None or newer(release.version, waiting.version)):
            waiting = release
    if waiting is not None and best is not None and waiting.version[1] <= best.version[1]:
        waiting = None
    return (best.tag if best else None), waiting


def _eligible(current: tuple[int, ...], release: Release, now: datetime) -> bool:
    if not _is_new_minor(current, release):
        return True
    if release.published is None:
        return False
    return now - release.published >= MINOR_WAIT


def _is_new_minor(current: tuple[int, ...], release: Release) -> bool:
    minor = release.version[1] if len(release.version) > 1 else 0
    current_minor = current[1] if len(current) > 1 else 0
    patch = release.version[2] if len(release.version) > 2 else 0
    return minor != current_minor and patch == 0


def wait_note(release: Release) -> str:
    if release.published is None:
        return f"{release.tag} is a new minor and its publish date could not be read, so it waits"
    eligible = (release.published + MINOR_WAIT).date().isoformat()
    return f"{release.tag} published {release.published.date().isoformat()}, eligible {eligible}"


def latest_stable(releases: list[Release]) -> Release | None:
    best: Release | None = None
    for release in releases:
        if release.prerelease or release.draft or not release.version:
            continue
        if best is None or newer(release.version, best.version):
            best = release
    return best


def github_releases(repo: str) -> list[Release]:
    payload = _get_json(
        f"https://api.github.com/repos/{repo}/releases?per_page=100",
        {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    releases: list[Release] = []
    for item in payload:
        version = parse_version(item.get("tag_name", ""))
        if version is None:
            continue
        published = item.get("published_at")
        releases.append(Release(
            version=version,
            tag=item["tag_name"],
            published=_parse_time(published) if published else None,
            prerelease=bool(item.get("prerelease")),
            draft=bool(item.get("draft")),
        ))
    return releases


def hub_tags(repository: str) -> list[Release]:
    releases: list[Release] = []
    for page in (1, 2, 3):
        payload = _get_json(f"https://hub.docker.com/v2/repositories/{repository}/tags?page_size=100&page={page}")
        for item in payload.get("results", []):
            version = parse_version(item.get("name", ""))
            if version is None:
                continue
            published = item.get("last_updated")
            releases.append(Release(
                version=version,
                tag=item["name"],
                published=_parse_time(published) if published else None,
            ))
        if not payload.get("next"):
            break
    return releases


def plex_public() -> Release | None:
    payload = _get_json("https://plex.tv/api/downloads/5.json")
    qnap = payload.get("nas", {}).get("QNAP", {})
    tag = str(qnap.get("version", ""))
    version = parse_version(tag.split("-", 1)[0])
    if version is None:
        return None
    published = None
    raw = qnap.get("release_date")
    if isinstance(raw, (int, float)):
        published = datetime.fromtimestamp(raw, timezone.utc)
    return Release(version, tag, published)


@dataclass(frozen=True)
class Firmware:
    """A QTS build: version, build counter and build date. A rebuild of the same version is newer."""

    version: str
    number: str
    build: str
    url: str = ""

    @property
    def order(self) -> tuple[int, ...] | None:
        version = parse_version(self.version)
        if version is None or not self.number.isdigit():
            return None
        return (*_pad(version, 3), int(self.number), int(self.build) if self.build.isdigit() else 0)

    @property
    def label(self) -> str:
        text = f"{self.version}.{self.number}"
        return f"{text} build {self.build}" if self.build else text


def qts_public(feed: str, model: str) -> Firmware | None:
    """The firmware QTS Live Update offers this model, from the feed QTS itself reads."""
    request = urllib.request.Request(feed, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return qts_firmware(response.read(), model)


def qts_firmware(document: bytes, model: str) -> Firmware | None:
    for entry in ElementTree.fromstring(document).iter("model"):
        products = [name.strip() for name in (entry.findtext("productName") or "").split(",")]
        if model != (entry.findtext("modelName") or "").strip() and model not in products:
            continue
        firmware = entry.find("firmware")
        if firmware is None:
            return None
        return Firmware(
            (firmware.findtext("version") or "").strip(),
            (firmware.findtext("number") or "").strip(),
            (firmware.findtext("buildNumber") or "").strip(),
            (firmware.findtext("updateInfoURL") or "").strip(),
        )
    return None


def normalize_image(ref: str) -> str:
    text = ref.strip()
    for prefix in ("docker.io/", "index.docker.io/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _get_json(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)
