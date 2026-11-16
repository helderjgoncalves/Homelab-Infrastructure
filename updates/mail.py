"""Mail in the same shape as the trips form: one card, inline styles, icons attached inline."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, Helvetica, Arial, sans-serif"
ICONS = Path(__file__).resolve().parent / "icons"
DARK = """
.m-bg { background: #0e131c !important; }
.m-card { background: #161d2c !important; border-color: #2a3242 !important; }
.m-text { color: #e8edf7 !important; }
.m-muted { color: #8e98ab !important; }
.m-accent { color: #6fa3da !important; }
.m-rule { border-top-color: #2a3242 !important; }
.m-bad { color: #ffb4a2 !important; }
"""

# The updater's own rows. The apps' titles come from config.toml through use_titles.
TITLES = {
    "images": "Images",
    "docker": "Docker",
    "updater": "Updater",
}


def use_titles(titles: dict[str, str]) -> None:
    TITLES.update(titles)


@dataclass(frozen=True)
class Notice:
    key: str
    running: str
    available: str
    url: str

    @property
    def title(self) -> str:
        return TITLES.get(self.key, self.key)


@dataclass(frozen=True)
class Issue:
    name: str
    detail: str

    @property
    def title(self) -> str:
        return TITLES.get(self.name, self.name)


@dataclass(frozen=True)
class Bump:
    key: str
    before: str
    after: str
    url: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.key, self.key)


@dataclass(frozen=True)
class Letter:
    subject: str
    text: str
    html: str


def pinned_letter(notices: list[Notice], bumps: list[Bump] | None = None, issues: list[Issue] | None = None) -> Letter:
    done = list(bumps or [])
    failed = list(issues or [])
    several = sum(bool(part) for part in (done, notices, failed)) > 1
    if failed and not done and not notices:
        subject, title, intro = ("Container update failed", "Update failed", "These did not succeed.")
        eyebrow, eyebrow_class, eyebrow_color = "Update failed", "m-bad", "#9a3412"
    elif done and notices:
        subject, title, intro = (
            "Homelab update",
            "Update",
            "Some containers were updated. The pinned ones were left as they are.",
        )
        eyebrow, eyebrow_class, eyebrow_color = "Homelab", "m-accent", "#3a6ea5"
    elif done:
        subject, title, intro = ("Containers were updated", "Updated", "These were updated.")
        eyebrow, eyebrow_class, eyebrow_color = "Homelab", "m-accent", "#3a6ea5"
    else:
        subject, title, intro = (
            "Pinned containers have a newer release",
            "Newer releases",
            "Nothing was changed. These stay on the release they are running.",
        )
        eyebrow, eyebrow_class, eyebrow_color = "Homelab", "m-accent", "#3a6ea5"
    if failed and (done or notices):
        subject, title = "Homelab update", "Update"
    lines = [intro, ""]
    blocks: list[str] = []
    if done:
        lines.append("Updated")
        blocks.append(_label("Updated") if several else "")
        blocks.append(_table([(bump.key, bump.title, _bump_value(bump)) for bump in done]))
        for bump in done:
            lines.append(f"{bump.title}  {_bump_detail(bump)}")
            if bump.url:
                lines.append(f"  {bump.url}")
        lines.append("")
    if notices:
        lines.append("Newer releases")
        blocks.append(_label("Newer releases") if several else "")
        blocks.append(_table([
            (notice.key, notice.title, f"{_escape(notice.running)} {_arrow()} {_link(notice.url, notice.available)}")
            for notice in notices
        ]))
        for notice in notices:
            lines.append(f"{notice.title}  {notice.running} → {notice.available}")
            lines.append(f"  {notice.url}")
        lines.append("")
    if failed:
        lines.append("Failed")
        blocks.append(_label("Failed") if several else "")
        blocks.append(_failure_table(failed))
        for issue in failed:
            lines += [issue.title, f"  {issue.detail}", ""]
    text = "\n".join(lines).rstrip() + "\n"
    html = _page(subject, eyebrow=eyebrow, eyebrow_class=eyebrow_class, eyebrow_color=eyebrow_color, title=title, intro=intro, body="".join(blocks))
    return Letter(subject, text, html)


def failure_letter(issues: list[Issue]) -> Letter:
    return pinned_letter([], None, issues)


def send(letter: Letter, env: dict[str, str]) -> None:
    body = {
        "from": env["RESEND_FROM"],
        "to": [env["RESEND_TO"]],
        "subject": letter.subject,
        "text": letter.text,
        "html": letter.html,
    }
    icons = _inline_icons(letter.html)
    if icons:
        body["attachments"] = icons
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {env['RESEND_API_KEY']}",
            "Content-Type": "application/json",
            "User-Agent": "homelab-updates",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status // 100 != 2:
                raise RuntimeError(f"resend returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        reason = exc.read().decode(errors="replace").strip()[:300]
        raise RuntimeError(f"resend returned HTTP {exc.code}: {reason}") from None


def _page(subject: str, eyebrow: str, eyebrow_class: str, eyebrow_color: str, title: str, intro: str, body: str) -> str:
    dark = DARK.replace("\n", "\n    ")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_escape(subject)}</title>
<style>
  :root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
  @media (prefers-color-scheme: dark) {{
    {dark}
  }}
</style>
</head>
<body class="m-bg" style="margin:0;padding:0;background:#f7f9fc;font-family:{FONT};-webkit-font-smoothing:antialiased">
  <table class="m-bg" role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f9fc">
    <tr><td align="center" style="padding:32px 16px">
      <table class="m-card" role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid #e3e8ef;border-radius:16px;overflow:hidden">
        <tr><td style="height:4px;line-height:4px;font-size:0;background:#ff9a4d;background-image:linear-gradient(90deg,#ffd27a,#ff9a4d,#ff6f91)">&nbsp;</td></tr>
        <tr><td style="padding:24px 28px 0">
          <div class="{eyebrow_class}" style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:{eyebrow_color}">{_escape(eyebrow)}</div>
          <h1 class="m-text" style="margin:6px 0 0;font-size:20px;line-height:1.3;font-weight:700;color:#1c2330">{_escape(title)}</h1>
          <p class="m-muted" style="margin:8px 0 0;font-size:14px;line-height:1.5;color:#54627a">{_escape(intro)}</p>
        </td></tr>
        <tr><td style="padding:18px 28px 20px">{body}</td></tr>
      </table>
      <p class="m-muted" style="margin:16px 0 0;max-width:560px;font-size:12px;line-height:1.5;color:#7a879c">Sent by the nightly update on the NAS.</p>
    </td></tr>
  </table>
</body>
</html>"""


def _label(text: str) -> str:
    return (
        '<div class="m-muted" style="padding:12px 0 2px;font-size:11px;font-weight:700;'
        f'letter-spacing:0.08em;text-transform:uppercase;color:#7a879c">{_escape(text)}</div>'
    )


def _table(rows: list[tuple[str, str, str]]) -> str:
    body = []
    last = len(rows) - 1
    for index, (key, name, value) in enumerate(rows):
        border = "" if index == last else "border-bottom:1px solid #e3e8ef;"
        body.append(
            "<tr>"
            + _icon_cell(key, border)
            + f'<td class="m-text m-rule" style="padding:11px 16px 11px 0;{border}font-size:14px;line-height:1.4;font-weight:700;color:#1c2330">{_escape(name)}</td>'
            + f'<td class="m-text m-rule" align="right" style="padding:11px 0;{border}font-size:14px;line-height:1.4;color:#1c2330;white-space:nowrap">{value}</td>'
            + "</tr>"
        )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(body)}</table>'


def _failure_table(issues: list[Issue]) -> str:
    rows = []
    last = len(issues) - 1
    for index, issue in enumerate(issues):
        border = "" if index == last else "border-bottom:1px solid #e3e8ef;"
        rows.append(
            "<tr>"
            + _icon_cell(issue.name, border)
            + f'<td class="m-text m-rule" style="padding:11px 0;{border}">'
            + f'<div style="font-size:14px;line-height:1.4;font-weight:700;color:#1c2330">{_escape(issue.title)}</div>'
            + f'<div class="m-bad" style="margin-top:3px;font-size:13px;line-height:1.4;color:#9a3412">{_escape(issue.detail)}</div>'
            + "</td></tr>"
        )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'


def _icon_cell(key: str, border: str) -> str:
    if not (ICONS / f"{key}.png").is_file():
        return ""
    return (
        f'<td width="34" valign="middle" style="width:34px;padding:8px 10px 8px 0;{border}">'
        f'<img src="cid:{key}" width="24" height="24" alt="" '
        'style="display:block;width:24px;height:24px;border:0;border-radius:6px;"></td>'
    )


def _inline_icons(html: str) -> list[dict]:
    icons = []
    seen: set[str] = set()
    for key in re.findall(r'src="cid:([^"]+)"', html):
        if key in seen:
            continue
        seen.add(key)
        path = ICONS / f"{key}.png"
        if not path.is_file():
            continue
        icons.append({
            "filename": path.name,
            "content": base64.b64encode(path.read_bytes()).decode(),
            "content_type": "image/png",
            "content_id": key,
        })
    return icons


def _bump_value(bump: Bump) -> str:
    before, after = bump.before.strip(), bump.after.strip()
    floating = {"latest", "v3", "0", "1", "2"}
    shown = after if after and after not in floating else ""
    if before and shown and before != shown:
        return f"{_escape(before)} {_arrow()} {_linked(bump.url, shown)}"
    if shown:
        return _linked(bump.url, shown)
    if bump.url:
        return _linked(bump.url, "Notes")
    return _escape("Updated")


def _linked(url: str, label: str) -> str:
    if not url:
        return _escape(label)
    return _link(url, label)


def _bump_detail(bump: Bump) -> str:
    before, after = bump.before.strip(), bump.after.strip()
    floating = {"latest", "v3", "0", "1", "2"}
    if before and after and before != after and after not in floating:
        return f"{before} → {after}"
    if after and after not in floating:
        return after
    return "Updated"


def _arrow() -> str:
    return '<span class="m-muted" style="color:#7a879c">→</span>'


def _link(href: str, label: str) -> str:
    return f'<a class="m-accent" href="{_escape(href)}" style="color:#3a6ea5;text-decoration:none">{_escape(label)}</a>'


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
