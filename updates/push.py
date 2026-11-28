"""Phone pushes to ntfy: each update that held, and each release to install by hand. The weekly mail still lists both."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from mail import FOUND, ICONS, Bump, Notice, todo

NTFY_URL = "https://ntfy.sh"
# The repo is public, so ntfy and the phone can fetch the icons the mail uses.
ICON_URL = "https://raw.githubusercontent.com/helderjgoncalves/Homelab-Infrastructure/main/updates/icons"
MAJOR = re.compile(r"v?(\d+)\.")


def message(item: Bump | Notice, topic: str) -> dict:
    if isinstance(item, Notice):
        body = {"topic": topic, "title": f"{item.title} {item.available} is out", "message": _release_detail(item)}
    else:
        body = {"topic": topic, "title": _title(item), "message": _detail(item)}
    if item.url:
        label = "Release notes" if "/releases" in item.url else "Open"
        body["click"] = item.url
        body["actions"] = [{"action": "view", "label": label, "url": item.url, "clear": True}]
    if (ICONS / f"{item.key}.png").is_file():
        icon = f"{ICON_URL}/{item.key}.png"
        # iOS ignores icon and shows an attached image as the thumbnail instead.
        body.update({"icon": icon, "attach": icon, "filename": f"{item.key}.png"})
    return body


def _title(bump: Bump) -> str:
    if bump.before and bump.after and bump.before != bump.after:
        return f"{bump.title} {bump.before} → {bump.after}"
    if bump.after or bump.before:
        return f"{bump.title} {bump.after or bump.before}"
    return f"{bump.title} updated"


def _detail(bump: Bump) -> str:
    if bump.reason:
        return "Went in by itself, checked by AI, and is running."
    if bump.before and bump.before == bump.after:
        return "New build of the same version."
    return "Updated and running."


def _release_detail(notice: Notice) -> str:
    majors = [MAJOR.match(version) for version in (notice.running, notice.available)]
    major = "A new major, install" if all(majors) and majors[0].group(1) != majors[1].group(1) else "Install"
    # The first reason only: the mail has them all.
    reasons = " ".join(part for part in (FOUND.get(notice.held, ""), *notice.reasons[:1]) if part)
    if notice.verdict == "shadow":
        return f"You run {notice.running}. Shadow mode: it would have gone in by itself. Install it by hand. {reasons}".rstrip()
    if notice.verdict == "manual":
        return f"You run {notice.running}. {major} it by hand. {reasons}{todo(notice.actions)}".rstrip()
    return f"You run {notice.running}. {major} it by hand."


def send(item: Bump | Notice, topic: str, server: str = NTFY_URL) -> None:
    """ntfy's JSON publish: the topic goes in the body, the request to the server root."""
    request = urllib.request.Request(
        server.rstrip("/"),
        data=json.dumps(message(item, topic)).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "homelab-updates"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status // 100 != 2:
                raise RuntimeError(f"ntfy returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        reason = exc.read().decode(errors="replace").strip()[:300]
        raise RuntimeError(f"ntfy returned HTTP {exc.code}: {reason}") from None
