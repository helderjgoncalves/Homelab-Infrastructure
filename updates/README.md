# Container updates

`update.sh` runs once a morning. It looks at what is actually running, bumps the apps `config.toml` lists, and sends mail only when something new happened. It does not run all day, and it does not update every stack.

Installed in the NAS crontab. Updates run at 09:00, NAS time, after the QNAP jobs at 03:00 and 04:00. The weekly mail and the image prune run at noon on Sunday. Output goes to `updates/state/cron.log`, so `updates/state/` must exist before cron runs. Past 2 MB the log is cut to its last 5000 lines.

```
0 9 * * * /share/CACHEDEV2_DATA/SSDShared/updates/update.sh >> /share/CACHEDEV2_DATA/SSDShared/updates/state/cron.log 2>&1
0 12 * * 0 /share/CACHEDEV2_DATA/SSDShared/updates/update.sh --digest >> /share/CACHEDEV2_DATA/SSDShared/updates/state/cron.log 2>&1
```

## What it does

`config.toml` lists the apps, one `[apps.<key>]` table each, and each app has a policy. Its header explains every field. A container in no app is left alone.

| Policy | Action | Today |
| --- | --- | --- |
| `floating` | Pull the tag the containers run and recreate only if the image changed. With `image`, any other tag is reported and left alone. | Cloudflared, Traefik (`traefik:v3`), Uptime Kuma, Beszel and its agent, Dockge |
| `semver` | Follows the GitHub releases. A patch is taken that morning. A new minor's `.0` waits a week; a patch of that minor during the week is taken that morning. The exact tag is pulled and retagged as the tag the containers run (`:v3`), then Compose starts it with `--pull never`. Never rolled back. `minor_wait_days = 0` takes a new minor that morning too. Another major is never taken. | Immich server and machine learning (`:v3`); AdGuard Home (`:v1`, a local tag, minors that morning) |
| `report` | Never bumped. A newer release is included once, in the Sunday mail. | Home Assistant, Zigbee2MQTT, Mosquitto, OpenCloud, Plex, QTS |

`members` are the other containers of an app, such as Immich's Postgres and Valkey or OpenCloud's Collabora and Yjs. They are only named in the log.

Plex is the QNAP package, not a container (`releases = "plex"`). The script reads the version from Plex on `127.0.0.1:32400` and compares it with the public QNAP build. It never installs the package.

QTS is the NAS firmware (`releases = "qts"`). The script reads the running version, build counter and build date with `getcfg`, and the model and Live Update feed from `/etc/config/uLinux.conf`, the same feed QTS itself checks. A newer version, or a rebuild of the same one, goes in the Sunday mail with its release notes. A model missing from the feed counts as a failed check. It never installs firmware.

A floating or semver app is skipped unless all its containers are running, and they move together. A report app only needs its first container running.

Compose runs inside `compose_container` (Dockge), with the project, service and every file Compose put on the containers' labels, so an override file such as Immich's is never dropped. A container that Compose did not start is reported and left alone. The compose container's own app is recreated last, from a one-shot container.

If `config.toml` cannot be read or has a mistake, nothing runs. The error is mailed like a failed update, and the run exits 1, so the heartbeat goes down.

An error in one container is reported and the run goes on with the next. If the run is stopped midway, what already moved is kept for the Sunday mail.

A release check that fails (GitHub, Docker Hub, the Plex feed or QTS Live Update) is only logged, unless it fails three mornings in a row; then it is mailed once.

## When an update fails

Every failure is written to `state/cron.log` with its error, as well as mailed. A pull the registry answers with "too many requests" is tried three times, 30 seconds apart, before it counts as failed. Any other pull error fails at once. A failed pull changes no container, and nothing remembers it: the next morning tries the same release again.

Healthy means its healthcheck passes within 6 minutes. A container without one must stay up for three checks 15 seconds apart. A container that restarts while it is checked is not healthy.

If a recreate fails or the container does not become healthy, the previous image is tagged back and the container is recreated. That failure is mailed that morning. The image that failed is written to `state/rejected.tsv` and is not tried again; the next new image is. Delete its line to try it again.

If restoring the previous image also fails, `state/prune.hold` is written and the weekly prune is skipped, so the previous image stays on disk. Each skipped prune is mailed the next morning as a reminder. Delete the file once the container is fixed.

A `semver` app, Immich or AdGuard Home, is never rolled back, because the new version may already have migrated its database or config. A failed or unhealthy update is mailed and left as it is. For AdGuard that means LAN DNS is down until it is fixed, and the mail may not get out, since the NAS resolves through it. An unhealthy one still runs the new version, so it is also an **Updated** row in the Sunday mail. A version whose recreate failed is written to `state/rejected.tsv` and not tried again.

## Images

On Sunday at noon, after the mail, images that no container is using are removed, including tagged ones. An image still used by a stopped container stays. `--dry-run`, `--notify-only` and the morning run never prune. A failed prune is mailed the next morning.

## Mail

An error is mailed that morning. The same failure is not mailed again the next morning; a different failure is. Bumps and newer pinned releases are stored and go out at noon on Sunday, including that Sunday morning's bumps. A week with nothing new sends nothing. The same release is not sent again.

If the Sunday mail does not go out, or the noon run finds the morning run still holding the lock, `state/digest.due` makes the next morning send it. If the NAS misses Sunday, a stored bump goes out on the first weekday morning once it is a week old.

The mail is one row per app, for the main container only: name on the left, version on the right. An automatic bump is an **Updated** row, from the version at the start of the week to the version it is running now. The version comes from the image label. Uptime Kuma and Dockge have none, so `version_file` reads it from their `/app/package.json`; for Cloudflared, `version_command` runs `cloudflared --version`. Immich, while a new minor waits out its week, is a **Newer releases** row.

Resend settings live in `updates/.env` (mode 600, gitignored). Copy `.env.example`. `RESEND_FROM` must use a domain verified in Resend.

`updates/state/` remembers what was already mailed. It is gitignored. A lock older than four hours belongs to a run that died; the next run clears it and mails that it did.

## Heartbeat

After every run except `--dry-run` and `--check`, `update.sh` tells an Uptime Kuma push monitor how it went: up when the run finished and any mail went out, down when it did not, such as when Resend is unreachable. Kuma alerts through ntfy on a down, and when a day passes with no heartbeat at all: cron stopped, or the run died before it could mail.

In Kuma, add a Push monitor with a heartbeat interval of 90000 seconds (25 hours) and the ntfy notification. Put the token from its push URL, the part after `/api/push/`, in `updates/.env` as `KUMA_PUSH_TOKEN`. The script calls Kuma on `127.0.0.1:3001`, not the URL Kuma shows. Without a token, no heartbeat is sent.

## Layout

The stacks directory is the parent of this folder. Nothing in the Python is pinned to a machine path.

| File | Role |
| --- | --- |
| `update.sh` | Starts the run. Under cron (root) it runs `python:3.14-alpine` as the owner of this script, with the Docker socket and the stacks directory mounted. Then sends the heartbeat. |
| `config.toml` | The apps, their policy, releases and version sources, and the compose container. |
| `config.py` | Reads `config.toml`, and refuses anything it does not understand. |
| `update.py` | Lock, mail timing, weekly state, prune. `--dry-run`, `--check`, `--notify-only`, `--digest`. |
| `jobs.py` | One path per policy: what is bumped, mailed, or left alone. |
| `releases.py` | Version rules, GitHub, Docker Hub, the Plex download feed, and QTS Live Update. |
| `mail.py` | The card mail. |
| `runtime.py` | Docker over the socket, API 1.41 or the daemon's minimum if that is newer. Compose runs inside the compose container, with the files from the container labels. |
| `test_update.py` | The config and its mistakes, the same decisions as before the config, the week rule, rollback, Immich, Compose labels, the weekly state, the lock, the prune, release-check failures, and the mail. No Docker. |

To add an app, add a table to `config.toml` and an icon as `icons/<key>.png`, then check with `--dry-run`.

```
updates/update.sh --dry-run      decide only
updates/update.sh --check        confirm the compose container can run Compose
updates/update.sh --digest       Sunday mail and prune, no updates
updates/update.sh --notify-only  mail new releases now, bump nothing
```

`python3 -m unittest test_update.py` runs the tests on a machine that has Python.
