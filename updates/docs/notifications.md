# Prune, mail, push and heartbeat

Part of [Container updates](../README.md).

## Images

On Sunday at noon, after the mail, images that no container is using are removed, including tagged ones. An image still used by a stopped container stays. `--dry-run`, `--notify-only` and the morning run never prune. A failed prune is mailed the next morning.

## Mail

An error is mailed that morning. The same failure is not mailed again the next morning; a different failure is. Bumps and newer pinned releases are stored and go out at noon on Sunday, including that Sunday morning's bumps. A week with nothing new sends nothing. The same release is not sent again.

If the Sunday mail does not go out, or the noon run finds the morning run still holding the lock, `state/digest.due` makes the next morning send it. If the NAS misses Sunday, a stored bump goes out on the first weekday morning once it is a week old.

The mail is one row per app, for the main container only: name on the left, version on the right. An automatic bump is an **Updated** row, from the version at the start of the week to the version it is running now. The version comes from the image label. Uptime Kuma and Dockge have none, so `version_file` reads it from their `/app/package.json`; for Cloudflared, `version_command` runs `cloudflared --version`. Immich, while a new minor waits out its week, is a **Newer releases** row.

Resend settings live in `updates/.env` (mode 600, gitignored). Copy `.env.example`. `RESEND_FROM` must use a domain verified in Resend.

## Push

Each update is also pushed to ntfy that morning: "Traefik 3.7.19 → 3.7.20", with the app's icon and a **Release notes** button. It goes to `NTFY_TOPIC` in `updates/.env` on ntfy.sh (`NTFY_URL` for another server), a topic of its own. The icon is the one in `icons/`, fetched from this public repo: the `icon` field for Android and the web app, and an attachment, because iOS shows only that. A push that fails is only logged; the Sunday mail still lists the update. Without a topic, nothing is pushed.

Every release that needs a hand is pushed once, the first morning it is seen: "Radarr 7.0.3.10918 is out", "You run 6.4.4.10685. A new major, install it by hand." That is a new major, a new AdGuard minor, a newer Plex, QTS or OpenCloud, and an Immich minor whose publish date can't be read. A release that installs by itself, such as an Immich minor waiting out its first week, is not pushed: its update push comes when it is installed. `state/pushed.tsv` keeps what was pushed, so the next mornings stay quiet; a push that failed is tried again the next morning, and a newer release of the same app is pushed again. Once the Sunday mail lists a release, its row is dropped. Failures are mailed, not pushed.

`updates/state/` remembers what was already mailed. It is gitignored. A lock older than four hours belongs to a run that died; the next run clears it and mails that it did.

## Heartbeat

After every run except `--dry-run` and `--check`, `update.sh` tells an Uptime Kuma push monitor how it went: up when the run finished and any mail went out, down when it did not, such as when Resend is unreachable. Kuma alerts through ntfy on a down, and when a day passes with no heartbeat at all: cron stopped, or the run died before it could mail.

In Kuma, add a Push monitor with a heartbeat interval of 90000 seconds (25 hours) and the ntfy notification. Put the token from its push URL, the part after `/api/push/`, in `updates/.env` as `KUMA_PUSH_TOKEN`. The script calls Kuma on `127.0.0.1:3001`, not the URL Kuma shows. Without a token, no heartbeat is sent.
