"""A phone push to ntfy for each update that held. The weekly mail still lists them."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from mail import ICONS, Bump

NTFY_URL = "https://ntfy.sh"
# The repo is public, so ntfy and the phone can fetch the icons the mail uses.
ICON_URL = "https://raw.githubusercontent.com/helderjgoncalves/Homelab-Infrastructure/main/updates/icons"


def message(bump: Bump, topic: str) -> dict:
    body = {"topic": topic, "title": _title(bump), "message": _detail(bump)}
    if bump.url:
        body["click"] = bump.url
        body["actions"] = [{"action": "view", "label": "Release notes", "url": bump.url, "clear": True}]
    if (ICONS / f"{bump.key}.png").is_file():
        icon = f"{ICON_URL}/{bump.key}.png"
        # iOS ignores icon and shows an attached image as the thumbnail instead.
        body.update({"icon": icon, "attach": icon, "filename": f"{bump.key}.png"})
    return body


def _title(bump: Bump) -> str:
    if bump.before and bump.after and bump.before != bump.after:
        return f"{bump.title} {bump.before} → {bump.after}"
    if bump.after or bump.before:
        return f"{bump.title} {bump.after or bump.before}"
    return f"{bump.title} updated"


def _detail(bump: Bump) -> str:
    if bump.before and bump.before == bump.after:
        return "New build of the same version."
    return "Updated and running."


def send(bump: Bump, topic: str, server: str = NTFY_URL) -> None:
    """ntfy's JSON publish: the topic goes in the body, the request to the server root."""
    request = urllib.request.Request(
        server.rstrip("/"),
        data=json.dumps(message(bump, topic)).encode(),
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
