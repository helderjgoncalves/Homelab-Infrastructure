"""A phone push to ntfy for each update that held. The weekly mail still lists them."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from mail import Bump

NTFY_URL = "https://ntfy.sh"


def message(bump: Bump, topic: str) -> dict:
    body = {
        "topic": topic,
        "title": f"{bump.title} updated",
        "message": f"{bump.before} → {bump.after}" if bump.before and bump.after else bump.after or bump.before or "updated",
        "tags": ["white_check_mark"],
        # Low: no sound or vibration, so an Uptime Kuma alert on the same topic still stands out.
        "priority": 2,
    }
    if bump.url:
        body["click"] = bump.url
    return body


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
