# Container updates

`update.sh` runs once a morning. It looks at what is actually running, bumps a short list of stateless containers, and sends mail only when something new happened. It does not run all day, and it does not update every stack.

Installed in the NAS crontab. Updates run at 09:00, NAS time, after the QNAP jobs at 03:00 and 04:00. The weekly mail and the image prune run at noon on Sunday. Output goes to `updates/state/cron.log`, so `updates/state/` must exist before cron runs. Past 2 MB the log is cut to its last 5000 lines.

```
0 9 * * * /share/CACHEDEV2_DATA/SSDShared/updates/update.sh >> /share/CACHEDEV2_DATA/SSDShared/updates/state/cron.log 2>&1
0 12 * * 0 /share/CACHEDEV2_DATA/SSDShared/updates/update.sh --digest >> /share/CACHEDEV2_DATA/SSDShared/updates/state/cron.log 2>&1
```

## What it does

| Running container | Action |
| --- | --- |
| Cloudflared, Traefik, Uptime Kuma, Beszel and its agent, Dockge | Pull the floating tag and recreate only if the image changed. Traefik must run `traefik:v3`; anything else is reported and left alone. |
| Immich server and machine learning | A patch is taken that morning. A new minor's `.0` waits a week; a patch of that minor during the week is taken that morning. Server and machine learning move together. The exact tag is pulled and retagged as the tag the containers run (`:v3`), then Compose starts it with `--pull never`. |
| AdGuard, Home Assistant, Zigbee2MQTT, Mosquitto, OpenCloud, Plex | Never bumped. A newer release is included once, in the Sunday mail. |
| Immich Postgres and Valkey, Collabora, Yjs | Part of Immich or OpenCloud. Not listed on their own. |
| Homer, devbox, ntfy, and anything else | Left alone. |

Plex is the QNAP package, not a container. The script reads the version from Plex on `127.0.0.1:32400` and compares it with the public QNAP build. It never installs the package.

A container that is not running is skipped. Beszel is skipped unless the hub and the agent are both up. Immich is skipped unless the server and machine learning are both up.

Dockge is recreated last, from a one-shot container, because the script uses Dockge to run Compose.

An error in one container is reported and the run goes on with the next. If the run is stopped midway, what already moved is kept for the Sunday mail.

A release check that fails (GitHub, Docker Hub or the Plex feed) is only logged, unless it fails three mornings in a row; then it is mailed once.

## When an update fails

Healthy means its healthcheck passes within 6 minutes. A container without one must stay up for three checks 15 seconds apart. A container that restarts while it is checked is not healthy.

If a recreate fails or the container does not become healthy, the previous image is tagged back and the container is recreated. That failure is mailed that morning. The image that failed is written to `state/rejected.tsv` and is not tried again; the next new image is. Delete its line to try it again.

If restoring the previous image also fails, `state/prune.hold` is written and the weekly prune is skipped, so the previous image stays on disk. Each skipped prune is mailed the next morning as a reminder. Delete the file once the container is fixed.

Immich is never rolled back, because the new server may already have migrated its database. A failed or unhealthy Immich update is mailed and left as it is. An unhealthy one still runs the new version, so it is also an **Updated** row in the Sunday mail. A version whose recreate failed is written to `state/rejected.tsv` and not tried again.

## Images

On Sunday at noon, after the mail, images that no container is using are removed, including tagged ones. An image still used by a stopped container stays. `--dry-run`, `--notify-only` and the morning run never prune. A failed prune is mailed the next morning.

## Mail

An error is mailed that morning. The same failure is not mailed again the next morning; a different failure is. Bumps and newer pinned releases are stored and go out at noon on Sunday, including that Sunday morning's bumps. A week with nothing new sends nothing. The same release is not sent again.

If the Sunday mail does not go out, or the noon run finds the morning run still holding the lock, `state/digest.due` makes the next morning send it. If the NAS misses Sunday, a stored bump goes out on the first weekday morning once it is a week old.

The mail is one row per app, for the main container only: name on the left, version on the right. An automatic bump is an **Updated** row, from the version at the start of the week to the version it is running now. The version comes from the image label. Uptime Kuma and Dockge have none, so it is read from their `/app/package.json`; Cloudflared reports it with `cloudflared --version`. Immich, while a new minor waits out its week, is a **Newer releases** row.

Resend settings live in `updates/.env` (mode 600, gitignored). Copy `.env.example`. `RESEND_FROM` must use a domain verified in Resend.

`updates/state/` remembers what was already mailed. It is gitignored. A lock older than four hours belongs to a run that died; the next run clears it and mails that it did.

## Heartbeat

After every run except `--dry-run` and `--check`, `update.sh` tells an Uptime Kuma push monitor how it went: up when the run finished and any mail went out, down when it did not, such as when Resend is unreachable. Kuma alerts through ntfy on a down, and when a day passes with no heartbeat at all: cron stopped, or the run died before it could mail.

In Kuma, add a Push monitor with a heartbeat interval of 90000 seconds (25 hours) and the ntfy notification. Put the token from its push URL, the part after `/api/push/`, in `updates/.env` as `KUMA_PUSH_TOKEN`. The script calls Kuma on `127.0.0.1:3001`, not the URL Kuma shows. Without a token, no heartbeat is sent.

## Layout

The stacks directory is the parent of this folder. Nothing in the Python is pinned to a machine path.

| File | Role |
| --- | --- |
| `update.sh` | Starts the run. Under cron (root) it runs `python:3.13-alpine` as the owner of this script, with the Docker socket and the stacks directory mounted. Then sends the heartbeat. |
| `update.py` | Lock, mail timing, weekly state, prune. `--dry-run`, `--check`, `--notify-only`, `--digest`. |
| `jobs.py` | Which container is bumped, mailed, or left alone. |
| `releases.py` | Version rules, GitHub, Docker Hub, and the Plex download feed. |
| `mail.py` | The card mail. |
| `runtime.py` | Docker over the socket, API 1.41 or the daemon's minimum if that is newer. Compose runs inside the Dockge container. |
| `test_update.py` | The week rule, rollback, Immich, the weekly state, the lock, the prune, release-check failures, and the mail. No Docker. |

```
updates/update.sh --dry-run      decide only
updates/update.sh --check        confirm Dockge can run Compose
updates/update.sh --digest       Sunday mail and prune, no updates
updates/update.sh --notify-only  mail new releases now, bump nothing
```

`python3 -m unittest test_update.py` runs the tests on a machine that has Python.
