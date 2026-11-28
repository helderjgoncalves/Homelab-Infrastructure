"""Releases the run would leave to a hand: whether this setup can take them by a tag change alone, asked once, and what a move needs.

The question is built here, from the release notes, the compose service and the files config.toml names; Workers AI only answers
it (verdict.py). Code can only say "manual", never "auto": when it already does, Workers AI is not asked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import composefile
import notes
from composefile import Edit
from config import App
from releases import Release, newer
from runtime import HERE, STACKS, Container, Docker, DockerError, Stack, last_line, log
from verdict import HELD_AI, HELD_BREAKING, MODEL, Question, Unavailable, Verdict, VerdictError, ask, messages

ROLLBACK = HERE / "state" / "rollback"
# alpine/git v2.54.1: git for the commit, and a shell to clear files before they are put back.
TOOLS = "alpine/git@sha256:29cff25811f790b9f72fc266bf8009a5a911714c0e03c31c47339c50909d9aef"
AUTHOR = ("updates", "updates@nas.invalid")
MAX_NOTES = 100_000
MAX_FILE = 20_000
MAX_PAGES = 3
# A linked page this long once trimmed is cut: a discussion page carries every comment.
MAX_PAGE = 60_000
CUT = f"[cut at {MAX_PAGE} characters]"
CUT_PAGE = "A page the notes link was too long to show whole, so a step in it may be missed."
# A context file's values that act as passwords, such as an automation's webhook_id, are hidden before it is sent.
SECRET_VALUE = re.compile(r"^([ \t]*(?:- )?[\w-]*(?:webhook_id|token|password|passwd|secret|api_?key|authorization|bearer|private_?key)[\w-]*[ \t]*:[ \t]*)\S.*$", re.I | re.M)
BLOCK = re.compile(r"[|>][-+0-9]*[ \t]*(?:#.*)?$")
# Mornings a release with no answer waits, unpushed, before it goes out as one to install by hand.
RETRY_DAYS = 3


def fingerprint(text: str) -> str:
    return sha256(text.encode()).hexdigest()[:16]


class Judge:
    """Asks Workers AI about a release once, and again only when what it would be shown has changed.

    update.py keeps the answers in state/verdicts.tsv, each with the fingerprint of what was shown (`prints`; "" for one that
    always holds), and in state/asking.tsv the day a release first could not be asked (`failures`). `asking` is False for the
    Sunday mail and --notify-only: they show the answers already given and ask nothing. `unavailable` says why Workers AI
    can't be asked, when it can't: what it would be asked goes out by hand with that line.
    """

    def __init__(self, docker: Docker, env: dict[str, str], known: dict[tuple[str, str], Verdict], asking: bool = True,
                 prints: dict[tuple[str, str], str] | None = None, failures: dict[tuple[str, str], date] | None = None,
                 today: date | None = None, unavailable: str = "", journal: Path | None = None):
        self.docker = docker
        self.env = env
        self.known = dict(known)
        self.asking = asking
        self.prints = dict(prints or {})
        self.failures = dict(failures or {})
        self.today = today or date.today()
        self.unavailable = unavailable
        self.journal = journal
        self.fresh: list[tuple[str, str, Verdict, str]] = []

    def verdict(self, app: App, info: Container, stack: Stack, current: tuple[int, ...], target: Release, releases: list[Release], images: dict[str, str], version: str) -> Verdict | None:
        """The verdict on moving `app` to `target`, the one given before if it still holds. None when it could not be asked.

        Workers AI is asked only when code can't already say "manual". What it is asked, what it answers and what is decided go
        in the journal.
        """
        key = (app.key, version)
        known = self.known.get(key)
        if known is not None and (not self.asking or not self.prints.get(key)):
            return known
        if not self.asking:
            return None
        question, vetoes, missing = None, [], ""
        try:
            question, vetoes = self.question(app, info, stack, current, target, releases, images)
        except notes.NotesError as exc:
            missing = str(exc)
        except Exception as exc:
            # A page cut off mid-read, an unknown charset: tried again the next morning, then sent by hand.
            log(f"{app.key}: the question about {version} could not be put together: {exc}")
            # A "manual" given before still holds; an "auto" is not taken on a question that couldn't be checked again.
            return known if known is not None and not known.auto else self._failed(key)
        model = self.env.get("CLOUDFLARE_AI_MODEL") or MODEL
        shown = fingerprint(missing) if question is None else fingerprint(json.dumps([messages(question), vetoes, model]))
        if known is not None and self.prints.get(key) == shown:
            return known
        if known is not None:
            log(f"{app.key} {version}: what Workers AI is shown has changed since it answered, so it is asked again")
        entry = {
            "app": app.key, "running": ".".join(str(part) for part in current), "version": version, "model": model,
            "messages": messages(question) if question else None, "vetoes": vetoes,
        }
        if question is None or vetoes:
            breaking = question is not None and any(veto != CUT_PAGE for veto in vetoes)
            found = Verdict("manual", (missing,) if question is None else tuple(vetoes[:5]), held=HELD_BREAKING if breaking else "")
            entry["asked"] = False
        elif self.unavailable:
            return None
        else:
            entry["asked"] = True
            try:
                answer = ask(question, self.env)
            except Unavailable as exc:
                self.unavailable = f"Workers AI was not asked: {exc}."
                log(f"{app.key}: Workers AI {exc}, so nothing more is asked this run")
                self._journal({**entry, "error": str(exc)})
                return None
            except VerdictError as exc:
                log(f"{app.key}: {exc}")
                self._journal({**entry, "error": str(exc), "answer": exc.said})
                return self._failed(key)
            entry.update(answer=answer.said, said=_plain(answer))
            found = combine(replace(answer, held=HELD_AI), vetoes)
        self._keep(key, found, shown)
        self._journal({**entry, "decision": _plain(found)})
        log(f"{app.key} {version}: {found.decision}. {' '.join(found.reasons)}")
        return found

    def waiting(self, key: str, version: str) -> bool:
        """Whether a release with no answer yet waits for one, rather than going out as one to install by hand.

        It waits RETRY_DAYS from the first morning Workers AI could not be asked. The Sunday mail never asks, so a release it
        has no answer for waits for the next morning.
        """
        first = self.failures.get((key, version))
        if first is not None:
            return (self.today - first).days < RETRY_DAYS
        return not self.asking and (key, version) not in self.known

    def overrule(self, key: str, version: str, reason: str) -> None:
        """A release that went in by itself and was rolled back stays by hand, whatever was said before or is shown later."""
        self._keep((key, version), Verdict("manual", (reason,)), "")

    def _keep(self, key: tuple[str, str], found: Verdict, shown: str) -> None:
        self.known[key] = found
        self.prints[key] = shown
        self.failures.pop(key, None)
        self.fresh.append((*key, found, shown))

    def _failed(self, key: tuple[str, str]) -> Verdict | None:
        self.failures.setdefault(key, self.today)
        return None

    def _journal(self, entry: dict) -> None:
        """One line per question in <journal>/<year>-<month>.jsonl, to see later what Workers AI was shown and said."""
        if self.journal is None:
            return
        try:
            # It holds the files shown, such as the automations.
            self.journal.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.journal / f"{self.today:%Y-%m}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a") as handle:
                handle.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}, ensure_ascii=False) + "\n")
            # A year of questions, and this month.
            oldest = f"{self.today.year - 1}-{self.today.month:02d}"
            for path in self.journal.glob("*.jsonl"):
                if path.stem < oldest:
                    path.unlink()
        except OSError as exc:
            log(f"the Workers AI journal could not be written: {exc}")

    def question(self, app: App, info: Container, stack: Stack, current: tuple[int, ...], target: Release, releases: list[Release], images: dict[str, str]) -> tuple[Question, list[str]]:
        text = self._notes(app, releases, current, target)
        compose = compose_text(stack, set(images) | {info.name}, self.docker, app)
        terms = inventory(self.docker, app, info)
        listing = ""
        if app.inventory_file:
            listing = f"In use by this setup, every {app.inventory_key} in {app.inventory_file}: {', '.join(terms) or '(none)'}"
        files = tuple((path, self._file(info, path)) for path in app.context_files)
        vetoes = []
        used = notes.named(text, terms, app.inventory_match)
        if used:
            vetoes.append(f"They name {', '.join(used)}, which this setup uses.")
        configured = notes.named(text, notes.compose_terms(compose), "{}")
        if configured:
            vetoes.append(f"They name {', '.join(configured)}, set in its compose file.")
        for sentence in notes.undone(text)[:1]:
            vetoes.append(f"The notes say a change can't be undone: “{sentence}”.")
        if CUT in text:
            vetoes.append(CUT_PAGE)
        moved = [f"{name} {ref} → {images[name]}" if images[name] != ref else f"{name} {ref}, the same tag, already pulled" for name, ref in _running(self.docker, images).items()]
        version = ".".join(str(part) for part in target.version)
        running = ".".join(str(part) for part in current)
        return Question(app.title, running, version, "; ".join(moved), compose, listing, files, text, app.rollback_files), vetoes

    def _notes(self, app: App, releases: list[Release], current: tuple[int, ...], target: Release) -> str:
        parts, pages = [], []
        for release in notes.between(releases, current, target.version):
            text = notes.relevant(release.notes) or "(no notes)"
            # A beta links the page of whatever is in beta now, not the page of this release.
            if app.linked_notes and not release.prerelease:
                for url in notes.links(release.notes):
                    if url in pages:
                        continue
                    if len(pages) == MAX_PAGES:
                        text += f"\n\n[{url} is linked too, and was not read]"
                        continue
                    pages.append(url)
                    try:
                        page = notes.relevant(notes.fetch_page(url)) or "(nothing about breaking changes)"
                        if len(page) > MAX_PAGE:
                            page = f"{page[:MAX_PAGE]}\n{CUT}"
                    except urllib.error.HTTPError as exc:
                        # A server error may pass by the next morning; a missing page will not.
                        if exc.code >= 500:
                            raise
                        page = f"(could not be read: HTTP {exc.code})"
                    text += f"\n\nFrom {url}, linked from these notes:\n{page}"
            parts.append(f"## {release.tag}\n{text}")
        text = "\n\n".join(parts)
        if len(text) > MAX_NOTES:
            raise notes.NotesError(f"The release notes are too long to check ({len(text)} characters once trimmed).")
        return text

    def _file(self, info: Container, path: str) -> str:
        try:
            text = self.docker.read_file(info.name, path).decode(errors="replace")
        except DockerError as exc:
            return f"(could not be read: {exc})"
        text = hide_secrets(text)
        return text if len(text) <= MAX_FILE else f"{text[:MAX_FILE]}\n[cut at {MAX_FILE} characters]"


def hide_secrets(text: str) -> str:
    """SECRET_VALUE's values hidden, with the lines of a block scalar (`password: |`) under them."""
    kept, under = [], None
    for line in text.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(" \t"))
        if under is not None and (not line.strip() or indent > under):
            continue
        under = None
        match = SECRET_VALUE.match(line)
        if match:
            if BLOCK.match(line[match.end(1):].rstrip("\r\n")):
                under = indent + (2 if line.lstrip(" \t").startswith("- ") else 0)
            line = SECRET_VALUE.sub(r"\1(hidden)", line)
        kept.append(line)
    return "".join(kept)


def inventory(docker: Docker, app: App, info: Container) -> list[str]:
    """Every inventory_key value in inventory_file: what this setup uses."""
    if not app.inventory_file:
        return []
    text = docker.read_file(info.name, app.inventory_file).decode(errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # One JSON object per line, as Zigbee2MQTT keeps its devices.
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    return sorted(set(_values(data, app.inventory_key)))


def _plain(found: Verdict) -> dict:
    return {"decision": found.decision, "reasons": list(found.reasons), "actions": list(found.actions)}


def combine(answer: Verdict, vetoes: list[str]) -> Verdict:
    """Code can only turn the answer into "manual": a veto, an action, or anything but "auto"."""
    if answer.auto and not vetoes:
        return answer
    return Verdict("manual", (*vetoes, *answer.reasons)[:5], answer.actions, held=HELD_BREAKING if vetoes else answer.held)


def line_since(releases: list[Release], current: tuple[int, ...], version: tuple[int, ...], size: int) -> datetime | None:
    """When the line of `version` (its first `size` parts) had its first stable release after `current`."""
    dates = [
        release.published for release in releases
        if release.published and release.version and not release.prerelease and not release.draft
        and release.version[:size] == version[:size] and newer(release.version, current)
    ]
    return min(dates) if dates else None


def compose_text(stack: Stack, names: set[str], docker: Docker, app: App) -> str:
    """The app's services as written in its compose files. Override files hold host paths, so they stay out."""
    services = []
    for name in app.containers:
        if name in names:
            try:
                services.append(docker.inspect(name).service)
            except DockerError:
                pass
    blocks = []
    for path in stack.files:
        if "override" in Path(path).name:
            continue
        text = Path(path).read_bytes().decode(errors="replace")
        for service in services:
            found = composefile.block(text, service)
            if found:
                # Comments describe the routine by hand, and a model reads them as steps to take.
                lines = [_uncommented(line) for line in found.splitlines() if not line.lstrip().startswith("#")]
                blocks.append(f"# {Path(path).name}\n" + "\n".join(lines))
    return "\n".join(blocks) or "(not found)"


def _uncommented(line: str) -> str:
    """A YAML line without its trailing comment: a `#` after a space, outside quotes."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and index and line[index - 1] in " \t":
            return line[:index].rstrip()
    return line


@dataclass(frozen=True)
class Saved:
    container: str
    # (path in the container, its tar under state/rollback/, or None when the path did not exist)
    files: tuple[tuple[str, Path | None], ...]


def save(docker: Docker, app: App, container: str) -> Saved:
    """Copy rollback_files out of the stopped container to state/rollback/<app>/. The copy stays there until the next move."""
    folder = ROLLBACK / app.key
    shutil.rmtree(folder, ignore_errors=True)
    # The copies hold keys and tokens (acme.json, a Zigbee network key), and state/ is on a share.
    for path in (ROLLBACK, folder):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    files = []
    for index, path in enumerate(app.rollback_files):
        target = folder / f"{index}.tar"
        files.append((path, target if docker.archive_to(container, path, target) else None))
    listing = folder / "paths.tsv"
    listing.touch(mode=0o600)
    listing.write_text("".join(f"{index}.tar\t{path}\n" for index, (path, _tar) in enumerate(files)))
    return Saved(container, tuple(files))


def put_back(docker: Docker, saved: Saved) -> None:
    """Each saved path exactly as it was: cleared first, so files the new version added go too, and a path that did not exist stays away."""
    paths = [path for path, _tar in saved.files]
    code, output = docker.run_once(TOOLS, ["-rf", "--", *paths], entrypoint=["rm"], user="0:0", volumes_from=[saved.container])
    if code != 0:
        raise DockerError(last_line(output) or f"could not clear {', '.join(paths)} ({code})")
    for path, tar in saved.files:
        if tar is not None:
            docker.put_archive_file(saved.container, path, tar)


def uncommitted(docker: Docker, paths: list[str]) -> list[str]:
    """The paths whose file is not exactly as committed: changed, staged, untracked or ignored. The commit would take those edits too."""
    relative = _relative(paths)
    # No optional locks: a dry run and the Sunday mail ask this too, and must not rewrite the index.
    code, output = _git(docker, ["--no-optional-locks", "status", "--porcelain", "--ignored", "--untracked-files=all", "--", *relative])
    if code != 0:
        raise DockerError(last_line(output) or f"git status exited {code}")
    return [line[3:] for line in output.splitlines() if len(line) > 3]


def commit(docker: Docker, edits: list[Edit], subject: str, body: str) -> str | None:
    """Commit the edited files in the stacks checkout, as the owner of this script. The error, or None. Nothing is pushed."""
    try:
        paths = _relative([edit.path for edit in edits])
    except ValueError as exc:
        return str(exc)
    message = f"{subject}\n\n{body}" if body else subject
    command = [
        "-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}", "-c", "commit.gpgsign=false",
        # The one-shot has git only, so hooks and signing that need other tools would fail.
        "commit", "--quiet", "--no-verify", "-m", message, "--", *paths,
    ]
    try:
        code, output = _git(docker, command)
    except DockerError as exc:
        return str(exc)
    return None if code == 0 else (last_line(output) or f"git exited {code}")


def _relative(paths: list[str]) -> list[str]:
    relative = [os.path.relpath(path, STACKS) for path in paths]
    if any(path.startswith("..") for path in relative):
        raise ValueError(f"{', '.join(relative)} is outside the stacks checkout")
    return relative


def _git(docker: Docker, args: list[str]) -> tuple[int, str]:
    """git in the stacks checkout, from the pinned one-shot, as the owner of this script."""
    return docker.run_once(
        TOOLS, ["-c", f"safe.directory={STACKS}", *args],
        user=f"{os.getuid()}:{os.getgid()}", workdir=str(STACKS), env=["HOME=/tmp"], binds=[f"{STACKS}:{STACKS}"],
    )


def _running(docker: Docker, images: dict[str, str]) -> dict[str, str]:
    refs = {}
    for name in images:
        try:
            refs[name] = docker.inspect(name).image.split("@", 1)[0]
        except DockerError:
            refs[name] = "?"
    return refs


def _values(data, key: str):
    """The first `key` down each branch: the domain of a config entry, not the domains its own data holds."""
    if isinstance(data, dict):
        if isinstance(data.get(key), str):
            yield data[key]
            return
        for value in data.values():
            yield from _values(value, key)
    elif isinstance(data, list):
        for item in data:
            yield from _values(item, key)
