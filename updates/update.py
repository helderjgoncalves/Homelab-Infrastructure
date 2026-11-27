#!/usr/bin/env python3
"""Container updates.

The stacks directory is the parent of this file. Nothing here is pinned to a
machine path.

  updates/update.sh                update at 09:00, push each update and each new release to install by hand
                                   to ntfy, mail an error, hold the rest
  updates/update.sh --digest       Sunday noon mail, then prune unused images; no updates
  updates/update.sh --dry-run      decide only
  updates/update.sh --check        confirm the compose container can run compose
  updates/update.sh --notify-only  mail new releases now, bump nothing
"""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import push
from config import Config, ConfigError, load as load_config
from jobs import Outcome, collect_notices, run
from mail import Bump, Issue, pinned_letter, send, use_titles
from runtime import HERE, Docker, DockerError, log

STATE = HERE / "state"
NOTIFIED = STATE / "notified.tsv"
PUSHED = STATE / "pushed.tsv"
FAILURES = STATE / "failures.tsv"
BUMPS = STATE / "weekly-bumps.tsv"
DUE = STATE / "digest.due"
REJECTED = STATE / "rejected.tsv"
HOLD = STATE / "prune.hold"
PRUNE_ERROR = STATE / "prune.error"
UNCHECKED = STATE / "unchecked.tsv"
UNCHECKED_DAYS = 3
LOCK = STATE / "lock"
# Longer than the slowest possible morning run, shorter than a day.
LOCK_STALE = 4 * 3600
ENV_FILE = HERE / ".env"


def main(argv: list[str]) -> int:
    if any(arg in ("-h", "--help") for arg in argv):
        print(__doc__.strip())
        return 0
    unknown = [arg for arg in argv if arg not in ("--dry-run", "--check", "--notify-only", "--digest")]
    if unknown:
        print(f"unknown argument: {unknown[0]}", file=sys.stderr)
        return 2
    dry = "--dry-run" in argv
    digest = "--digest" in argv
    docker = Docker()
    try:
        config = load_config()
    except ConfigError as exc:
        log(str(exc))
        if dry or "--check" in argv:
            return 1
        return _config_failed(str(exc), digest)
    use_titles(config.titles)
    docker.compose_container = config.compose_container
    if "--check" in argv:
        return check(docker)
    stale = None
    if not dry:
        taken, stale = _lock()
        if not taken:
            log("another update is already running")
            if digest:
                _mark_due(_clock()[1])
                log("the weekly mail moves to the next morning")
            return 0
        signal.signal(signal.SIGTERM, _stop)
    try:
        if stale:
            log(f"cleared the lock of a run that started {stale} and did not finish")
        if digest:
            notices = _notices(docker, config)
            _push_releases(notices, dry)
            code = _send_report(notices, [], [], dry, weekly=True)
            _weekly_prune(docker, dry)
            return code
        if "--notify-only" in argv:
            notices = _notices(docker, config)
            _push_releases(notices, dry)
            return _send_notices(notices, dry)
        outcome = Outcome()
        try:
            outcome = run(docker, config, dry, _load_rejected(), outcome)
        except SystemExit:
            if not dry:
                _keep_rollback_state(outcome)
                _save_bumps(merge_bumps(_load_bumps(), outcome.bumps, _clock()[1]))
            raise
        if stale:
            outcome.issues.append(Issue("updater", f"The run that started {stale} did not finish. Its lock was cleared."))
        if not dry:
            _keep_rollback_state(outcome)
            outcome.issues.extend(_unchecked_issues(outcome.unchecked, _clock()[1]))
            outcome.issues.extend(_prune_error())
        _push(outcome.bumps, dry)
        _push_releases(outcome.notices, dry)
        return _send_report(outcome.notices, outcome.bumps, outcome.issues, dry)
    finally:
        if not dry:
            _unlock()


def _config_failed(error: str, digest: bool) -> int:
    """Nothing runs without a config. The error is mailed like a failed update, and the run fails, so the heartbeat goes down."""
    taken, _stale = _lock()
    if not taken:
        log("another update is already running, so the config error is not mailed")
        if digest:
            _mark_due(_clock()[1])
            log("the weekly mail moves to the next morning")
        return 1
    try:
        _send_report([], [], [Issue("updater", f"{error}. Nothing was updated.")], False, weekly=digest)
    finally:
        _unlock()
    return 1


def check(docker: Docker) -> int:
    try:
        code, output = docker.exec(docker.compose_container, ["/usr/bin/docker", "compose", "version"])
    except DockerError as exc:
        log(f"compose check failed: {exc}")
        return 1
    log(output.strip() or f"compose exited {code}")
    return 0 if code == 0 else 1


@dataclass(frozen=True)
class Kept:
    key: str
    before: str
    after: str
    url: str
    since: date

    def as_bump(self) -> Bump:
        return Bump(self.key, self.before, self.after, self.url)


def digest_due(today: date, kept: list[Kept], retry: bool = False, wday: int | None = None) -> bool:
    """Whether a morning run sends the weekly mail. On Sunday the noon run sends it."""
    if retry:
        return True
    if not kept or wday == 7:
        return False
    return (today - min(item.since for item in kept)).days >= 7


def merge_bumps(kept: list[Kept], bumps: list[Bump], today: date) -> list[Kept]:
    by_key = {item.key: item for item in kept}
    for bump in bumps:
        previous = by_key.get(bump.key)
        if previous is None:
            by_key[bump.key] = Kept(bump.key, bump.before, bump.after, bump.url, today)
            continue
        by_key[bump.key] = Kept(bump.key, previous.before, bump.after, bump.url or previous.url, previous.since)
    return list(by_key.values())


def _send_report(notices, bumps, issues, dry: bool, weekly: bool = False) -> int:
    wday, today = _clock()
    for issue in issues:
        log(f"{issue.name}: {issue.detail}")
    # The failure mail goes first, so a state file that cannot be written does not swallow it.
    fresh = _fresh(issues)
    failed_send = _deliver(pinned_letter([], None, fresh), dry) if fresh else 0
    kept = merge_bumps(_load_bumps(), bumps, today)
    if not dry:
        _save_bumps(kept)
    if not dry and (not fresh or failed_send == 0):
        _remember_failures(issues)
    pending = _pending(notices)
    if not (weekly or digest_due(today, kept, retry=DUE.is_file(), wday=wday)):
        if kept or pending:
            log(f"weekly mail waits ({len(kept)} updates, {len(pending)} newer releases)")
        elif not fresh:
            log("nothing new to email")
        return failed_send
    if not kept and not pending:
        log("nothing new for the weekly mail")
        if not dry:
            DUE.unlink(missing_ok=True)
        return failed_send
    if not dry:
        _mark_due(today)
    digest_failed = _deliver(pinned_letter(pending, [item.as_bump() for item in kept], None), dry)
    if digest_failed == 0 and not dry:
        _save_bumps([])
        _remember_notices(notices)
        DUE.unlink(missing_ok=True)
    return failed_send or digest_failed


def _send_notices(notices, dry: bool) -> int:
    pending = _pending(notices)
    if not pending:
        log("nothing new to email")
        return 0
    failed = _deliver(pinned_letter(pending, None, None), dry)
    if failed == 0 and not dry:
        _remember_notices(notices)
    return failed


def _notices(docker: Docker, config: Config) -> list:
    notices: list = []
    try:
        running = docker.names()
    except DockerError as exc:
        log(f"could not list containers: {exc}")
        running = set()
    collect_notices(docker, config, running, notices)
    return notices


def _weekly_prune(docker: Docker, dry: bool) -> None:
    if dry:
        log("would prune unused images")
        return
    if HOLD.is_file():
        log(f"unused images kept: {HOLD.name} is present after a failed restore")
        today = _clock()[1].isoformat()
        _write(PRUNE_ERROR, f"Unused images were not pruned on {today}, because updates/state/prune.hold is present after a failed restore. Delete it once the container is fixed.\n")
        return
    try:
        removed, space = docker.prune_unused()
    except DockerError as exc:
        log(f"prune failed: {exc}")
        _write(PRUNE_ERROR, f"Prune of unused images failed. {exc}\n")
        return
    PRUNE_ERROR.unlink(missing_ok=True)
    log(f"unused images: deleted {removed} images and layers, freed {_mb(space)}")


def _prune_error() -> list[Issue]:
    if not PRUNE_ERROR.is_file():
        return []
    text = PRUNE_ERROR.read_text().strip()
    return [Issue("images", text)] if text else []


def _unchecked_issues(unchecked: list[tuple[str, str]], today: date) -> list[Issue]:
    """A release check that failed UNCHECKED_DAYS mornings in a row is a failure."""
    previous: dict[str, date] = {}
    if UNCHECKED.is_file():
        for line in UNCHECKED.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                try:
                    previous[parts[0]] = date.fromisoformat(parts[1])
                except ValueError:
                    pass
    since = {key: previous.get(key, today) for key, _error in unchecked}
    if since:
        _write(UNCHECKED, "".join(f"{key}\t{day.isoformat()}\n" for key, day in since.items()))
    else:
        UNCHECKED.unlink(missing_ok=True)
    return [
        Issue(key, f"Checking for new releases has failed every morning since {day.isoformat()}. See updates/state/cron.log.")
        for key, day in since.items()
        if (today - day).days >= UNCHECKED_DAYS - 1
    ]


def _keep_rollback_state(outcome: Outcome) -> None:
    if outcome.rejected:
        lines = REJECTED.read_text().splitlines() if REJECTED.is_file() else []
        lines += [f"{key}\t{value}" for key, value in outcome.rejected]
        _write(REJECTED, "".join(f"{line}\n" for line in dict.fromkeys(lines)))
    if outcome.hold:
        existing = HOLD.read_text() if HOLD.is_file() else ""
        today = _clock()[1].isoformat()
        _write(HOLD, existing + "".join(f"{today}\t{line}\n" for line in outcome.hold))


def _load_rejected() -> set[tuple[str, str]]:
    if not REJECTED.is_file():
        return set()
    pairs = (line.split("\t") for line in REJECTED.read_text().splitlines())
    return {(parts[0], parts[1]) for parts in pairs if len(parts) == 2}


def _mb(space: int) -> str:
    return f"{space / 1048576:.1f} MB"


def _clock() -> tuple[int, date]:
    raw_date = os.environ.get("UPDATE_TODAY", "")
    raw_day = os.environ.get("UPDATE_WDAY", "")
    today = date.fromisoformat(raw_date) if raw_date else date.today()
    wday = int(raw_day) if raw_day.isdigit() else today.isoweekday()
    return wday, today


def _load_bumps() -> list[Kept]:
    if not BUMPS.is_file():
        return []
    kept: list[Kept] = []
    for line in BUMPS.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        key, before, after, url, raw = parts
        try:
            since = date.fromisoformat(raw)
        except ValueError:
            continue
        kept.append(Kept(key, before, after, url, since))
    return kept


def _save_bumps(kept: list[Kept]) -> None:
    if not kept:
        BUMPS.unlink(missing_ok=True)
        return
    _write(BUMPS, "".join(f"{item.key}\t{item.before}\t{item.after}\t{item.url}\t{item.since.isoformat()}\n" for item in kept))


def _fresh(issues: list[Issue]) -> list[Issue]:
    seen = set(FAILURES.read_text().splitlines()) if FAILURES.is_file() else set()
    return [issue for issue in issues if _failure_line(issue) not in seen]


def _failure_line(issue: Issue) -> str:
    return f"{issue.name}\t{' '.join(issue.detail.split())}"


def _notified() -> dict[str, list[str]]:
    if not NOTIFIED.is_file():
        return {}
    rows = (line.split("\t") for line in NOTIFIED.read_text().splitlines())
    return {parts[0]: parts for parts in rows if len(parts) == 4}


def _pending(notices) -> list:
    seen = {(parts[0], parts[2]) for parts in _notified().values()}
    return [notice for notice in notices if (notice.key, notice.available) not in seen]


def _remember_failures(issues: list[Issue]) -> None:
    if issues:
        _write(FAILURES, "".join(f"{_failure_line(issue)}\n" for issue in issues))
    else:
        FAILURES.unlink(missing_ok=True)


def _remember_notices(notices) -> None:
    """Add to what was sent. A check that failed this time must not forget its release."""
    rows = {key: "\t".join(parts) for key, parts in _notified().items()}
    for notice in notices:
        rows[notice.key] = _line(notice)
    _write(NOTIFIED, "".join(f"{line}\n" for line in rows.values()))


def _mark_due(today: date) -> None:
    _write(DUE, today.isoformat() + "\n")


def _write(path: Path, text: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _deliver(letter, dry: bool) -> int:
    if dry:
        log(f"would email: {letter.subject}")
        STATE.mkdir(parents=True, exist_ok=True)
        name = "failure" if letter.subject == "Container update failed" else "update"
        path = STATE / f"preview-{name}.html"
        path.write_text(letter.html)
        log(f"preview {path}")
        return 0
    env = _env()
    if env is None:
        return 1
    try:
        send(letter, env)
    except Exception as exc:
        log(f"email failed: {exc}")
        return 1
    log(f"emailed {letter.subject} to {env['RESEND_TO']}")
    return 0


def _push(bumps, dry: bool) -> None:
    """One ntfy push per update. A push that fails is logged only: the weekly mail still has the update."""
    if not bumps:
        return
    if dry:
        for bump in bumps:
            log(f"would push: {bump.title} {bump.before} → {bump.after}")
        return
    for bump in _send_pushes(bumps):
        log(f"pushed {bump.title} {bump.before} → {bump.after}")


def _push_releases(notices, dry: bool) -> None:
    """One push per release to install by hand, the first time it is seen. One that fails is tried again the next morning."""
    mailed = {(parts[0], parts[2]) for parts in _notified().values()}
    pushed = {release for release in _pushed() if release not in mailed}
    fresh = [notice for notice in _pending(notices) if notice.by_hand and (notice.key, notice.available) not in pushed]
    if dry:
        for notice in fresh:
            log(f"would push: {notice.title} {notice.available} is out")
        return
    sent = _send_pushes(fresh)
    for notice in sent:
        log(f"pushed {notice.title} {notice.available} is out")
    rows = pushed | {(notice.key, notice.available) for notice in sent}
    if rows != _pushed():
        _write(PUSHED, "".join(f"{key}\t{available}\n" for key, available in sorted(rows)))


def _pushed() -> set[tuple[str, str]]:
    if not PUSHED.is_file():
        return set()
    rows = (line.split("\t") for line in PUSHED.read_text().splitlines())
    return {(parts[0], parts[1]) for parts in rows if len(parts) == 2}


def _send_pushes(items: list) -> list:
    """The items whose push went out. A push that fails is logged only: the weekly mail still has it."""
    if not items:
        return []
    env = _read_env() or {}
    topic = env.get("NTFY_TOPIC", "")
    if not topic:
        log("NTFY_TOPIC is not set, no push sent")
        return []
    sent = []
    for item in items:
        try:
            push.send(item, topic, env.get("NTFY_URL") or push.NTFY_URL)
        except Exception as exc:
            log(f"push for {item.key} failed: {exc}")
        else:
            sent.append(item)
    return sent


def _read_env() -> dict[str, str] | None:
    if not ENV_FILE.is_file():
        return None
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'\"").strip()
    return env


def _env() -> dict[str, str] | None:
    env = _read_env()
    if env is None:
        log(f"{ENV_FILE.name} is missing, email not sent")
        return None
    if not all(env.get(key) for key in ("RESEND_API_KEY", "RESEND_FROM", "RESEND_TO")):
        log("Resend settings are incomplete, email not sent")
        return None
    return env


def _line(notice) -> str:
    return f"{notice.key}\t{notice.running}\t{notice.available}\t{notice.url}"


def _lock() -> tuple[bool, str | None]:
    """Take the lock. A lock older than LOCK_STALE belongs to a run that died, and is taken over."""
    STATE.mkdir(parents=True, exist_ok=True)
    stale = None
    for _attempt in range(2):
        try:
            LOCK.mkdir()
            return True, stale
        except FileExistsError:
            pass
        try:
            started = LOCK.stat().st_mtime
        except FileNotFoundError:
            continue
        if time.time() - started < LOCK_STALE:
            return False, None
        stale = datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _unlock()
    return False, None


def _unlock() -> None:
    try:
        LOCK.rmdir()
    except OSError:
        pass


def _stop(signum, _frame) -> None:
    # Python is PID 1 in the container; without a handler SIGTERM is ignored and the lock is left behind.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except DockerError as exc:
        log(str(exc))
        raise SystemExit(1)
