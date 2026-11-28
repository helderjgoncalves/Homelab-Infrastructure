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
| `floating` | Pull the tag the containers run and recreate only if the image changed. The tag is how far it moves: a minor tag (`v3.7`) takes patches, a major tag (`2`) minors too, `latest` everything. The newest release past the tag is a **Newer releases** row in the Sunday mail, moved to by hand by changing the tag. With `hold_major = true`, for images with no major tag, a pulled image whose version label is another major gets the old image's tag back and is a **Newer releases** row instead. With `image`, any other tag is reported and left alone. | Patches only: Home Assistant (`2026.11`), Zigbee2MQTT (`2.14`), Mosquitto (`2.1-alpine`). Within a major: Traefik (`v3`), Uptime Kuma (`2`), Beszel and its agent (`0`), Dockge (`1`), Maintainerr (`3`), Seerr (`v3`); Radarr, Sonarr, Prowlarr and qBittorrent (`latest` with `hold_major`). Everything: Cloudflared and Homer (`latest`, calendar versions) |
| `semver` | Follows the GitHub releases. A patch is taken that morning. A new minor is taken that morning too, unless the app sets `wait_days`: then its `.0` waits that many days, and a patch of that minor meanwhile is taken that morning. Immich has `wait_days = 7`. The exact tag is pulled and retagged as the tag the containers run (`:v3`), then Compose starts it with `--pull never`. Another major is never taken; it is a **Newer releases** row. With `pin_minor = true` only patches are; a newer minor is a **Newer releases** row too. Not rolled back unless `rollback = true`. | Immich server and machine learning (`:v3`, no rollback); AdGuard Home (`:v1.0`, a local tag because AdGuard publishes no minor tag, patches only, rolled back) |
| `report` | Never bumped. A newer release is included once, in the Sunday mail. | OpenCloud (its upgrade is a procedure, see `opencloud/AGENTS.md`), Plex, QTS |

The services the house or the network stops without (AdGuard Home, Home Assistant, Zigbee2MQTT, Mosquitto) take patches only: their minors may carry breaking changes, so those are read and moved to by hand, or judged by Workers AI where the app has `auto`. Traefik carries all ingress but takes v3 minors: its behaviour changes ship in patches as often as in minors, so a minor tag would add work without avoiding them. The rest take everything but a new major.

`critical = true` is on Immich, Traefik and AdGuard Home: their new majors are always installed by hand, never asked about, even with `auto`. `update.sh --check` lists what a new patch, minor and major does to each app, and fails if a critical app's tag would take a major by itself.

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

Immich, a `semver` app without `rollback`, is never rolled back, because the new server may already have migrated its database. A failed or unhealthy Immich update is mailed and left as it is. An unhealthy one still runs the new version, so it is also an **Updated** row in the Sunday mail. A version whose recreate failed is written to `state/rejected.tsv` and not tried again.

AdGuard Home is LAN DNS, so it has `rollback = true`, and `pin_minor = true`, because since v1.0 its minors may break things. Before the update, `rollback_files` (its `AdGuardHome.yaml`) are copied out of the container, with their owner and mode. If the new version fails to start or is not healthy, `:v1` is tagged back to the previous image, the container is created without starting, the saved config is put back over the one the new version may have migrated, and only then does it start. The failure is mailed, and the version goes to `state/rejected.tsv` like a floating image. If the config cannot be copied out, nothing is updated. Its healthcheck asks AdGuard for a name its `*.PUBLIC_ZONE` rewrite answers, on its LAN address, so healthy means DNS answers, and an update that breaks DNS is rolled back within about two minutes.

## By itself (`auto`)

Most releases left to a hand need nothing but the new tag. With `auto` set on an app, Workers AI reads the release notes and says whether that is so. It decides one thing only: whether the tag change is all it takes on this setup. Everything else, a file to edit, a command to run, a setting to change, stays by hand, and the push and the mail say why and what to do. When it is held because breaking changes were found, the push says who found them: "Breaking changes detected by AI." or, when a check in code did (below), "Breaking changes detected in the notes." The push gives the first reason and every step; the mail gives every reason.

It applies to the releases the run would otherwise leave to a hand: the newest release past a floating tag (`2026.11` to `2026.12`, `v3` to `v4`), a major that `hold_major` keeps back, and a semver major or `pin_minor` minor. A release a semver app installs by itself anyway is not asked about. A new major is asked about too, except for an app with `critical = true`, whose majors stay by hand, unasked: Immich, Traefik and AdGuard Home. For Home Assistant a major is the new year (`2026.12` to `2027.1`). A release the tag takes by itself is asked about only with `breaking_marker`: a Home Assistant patch (`2026.11.3` to `2026.11.4`) goes in by itself unless a line of its notes is marked `(breaking-change)` for an integration set up (`([zha docs])`) or for Home Assistant itself (no integration named), and is held for Workers AI if so. Over the two years to November 2026, 10 of 91 patches had such a line, none for an integration set up here.

- **When.** A release is asked about the first morning the run sees it, and with `auto = "on"` it goes in that same run. An app with `wait_days` is asked once the first release of the new line (`2026.12.0` for `2026.12`, `4.0.0` for `v4`) is that many days old; until then it is a **Newer releases** row that is not pushed, nor marked as mailed, so its verdict is pushed when it comes.
- **What it is shown.** The notes of every release after the running one up to the new one, each after the notes of its betas, trimmed to their breaking changes, deprecations, removals, migrations and changed settings, each kept line under its heading. With `linked_notes`, also the pages a stable release's notes link as release notes, a migration guide or breaking changes, a link or a bare URL on such a line or alone in the notes (Home Assistant's blog post, Uptime Kuma's migration guide), only the linked section when the link names one, and cut at 60,000 characters (a discussion page carries every comment). Then what the updater does by itself (the tag, the pull, the `rollback_files` it saves, the health check and rollback), so none of it is listed as a step for you; the app's compose service as written but without its comments, whole-line or trailing, which describe the routine by hand (values from `.env` are never read, override files are left out); the names in `inventory_file` (for Home Assistant, the domain of each integration set up; for Zigbee2MQTT, the model of each paired device; nothing else of those files); and the `context_files` (Home Assistant's `configuration.yaml`, automations and scripts, so a change to a trigger can be checked against the ones written), with the value of any key named like `webhook_id`, `token`, `password`, `secret` or `api_key` shown as `(hidden)`. Never a file with secrets: everything shown goes to Cloudflare.
- **How it decides.** Most releases need nothing but the image, and an unhealthy start is rolled back anyway, so it says manual only for a step, or a breaking change, removal, rename or changed default, that applies to something it can see this setup uses, or a step the notes ask of everyone. A change for a feature, integration or device that is not in what it is shown is not a reason, and neither is general advice such as backing up. Missing or cut notes are.
- **Code can only say no, and then nothing is asked.** Checks in code say "manual" when the notes name something in the inventory or an environment key or flag the compose service sets, or say a change can't be undone, or when a page they link was cut, and so do notes that are missing, too long, or may skip a release. Workers AI is asked only when none of them does, since its answer could not change the decision; the push then gives the check's reason. Code never says "auto".
- **Asked once, for what it was shown.** The answer stays in `state/verdicts.tsv`, with a fingerprint of everything Workers AI was shown. Each morning the question is put together again, and if the notes, the compose service, the inventory, the files or the model changed, it is asked again; otherwise the answer stands. A release rolled back after it went in by itself stays by hand whatever is shown later.
- **No answer yet.** When Workers AI can't be reached, or the question can't be put together, the release waits for an answer: a **Newer releases** row saying so, not pushed and not marked as mailed, asked again the next morning. After three mornings with no answer it is pushed and mailed as one to install by hand, and still asked each morning; when the answer comes, it is pushed once more with it. `state/asking.tsv` keeps the first of those mornings. The Sunday mail and `--notify-only` never ask, so a release they have no answer for waits for the next morning; `--dry-run` asks and keeps nothing but the journal line.
- **No tokens.** When Workers AI answers that the day's free allocation is used up (HTTP 429, or error 4006) or refuses the token (401, 403), or `.env` has no Cloudflare settings, nothing more is asked that run, and each release it would have been asked about is pushed and mailed at once as one to install by hand, as without `auto`, with "Workers AI was not asked: it has no tokens left today (HTTP 429)." or the reason. It is asked again the next morning: it goes in by itself then if the answer allows it, and otherwise the answer is pushed once more, even if the Sunday mail listed the release meanwhile.
- **The journal.** Every question goes in `state/workers-ai/<year>-<month>.jsonl`, one JSON line each: the app, the running and new versions, the model, the messages exactly as sent, the checks that said "manual", the answer as it came back, and the decision, or the error. A release a check decided is written too, with the messages it would have been sent and `"asked": false`, so a check can be weighed against Workers AI later. A question is written once: a morning that reuses the answer adds nothing. The journal holds the files shown, such as the automations, so the folder is mode 700 and each file 600. It keeps the current month and the twelve before it.

`auto = "shadow"` only reports. A release that could have gone in by itself is still pushed and mailed as one to install by hand, with "Shadow mode: it would have gone in by itself" and the reasons, so the verdicts can be checked against what the release really needed before any of them acts.

`auto = "on"` installs it in the run that gets the answer:

1. The compose file's `image:` line is changed to the new tag, keeping its quotes and comments. The project as Compose resolves it, before and after, must differ in that image only, or the file is put back. The file must set the image plainly (no `${VAR}`, no digest), be writable by the updater, and be exactly as committed, since the commit takes the whole file; otherwise the release stays by hand that morning, unasked, and the reason is in the push and the mail. Once the file is committed, the next morning tries again.
2. The new image is pulled. A semver app pulls the exact version and tags it as the new tag.
3. With `rollback_files`, the `alpine/git` image a rollback uses is pulled if the Sunday prune removed it (if it can't be, nothing is stopped), then the container is stopped and they are copied to `state/rollback/<key>/`, streamed, so a database is copied whole and still. The copy stays until the next one. It holds keys and tokens (Traefik's `acme.json`, the Zigbee network key), so `state/rollback/` is mode 700 and each copy 600.
4. The containers are recreated and must become healthy. If not, the old tag goes back in the file, the containers are created without starting, the saved files are put back (each path cleared first, so files the new version added go too), and they start. The failure is mailed, and that release stays by hand from then on. A run stopped from step 3 on (a shutdown) puts the previous version back the same way before it exits.
5. The edited file is committed in the stacks checkout, from a one-shot `alpine/git` container (pinned by digest, no network) as the owner of this script. Only that file is committed. Nothing is pushed.

It is pushed like any update, saying Workers AI checked it, and the mail marks it with a green **Safe · AI** tag; what Workers AI said is in the journal.

Workers AI runs `@cf/openai/gpt-oss-120b` (`CLOUDFLARE_AI_MODEL` to change it), on Cloudflare's free tier, which keeps no prompts. One question costs a few hundred to a few thousand of the 10,000 Neurons a day. Put `CLOUDFLARE_ACCOUNT_ID` and an API token with only **Account › Workers AI › Read** on your account (`CLOUDFLARE_AI_TOKEN`; running a model needs nothing more, and the template's Edit only manages fine-tunes), in `updates/.env`. Without them, nothing is asked and every release stays by hand, as before, its push saying that Workers AI was not asked.

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

## Layout

The stacks directory is the parent of this folder. Nothing in the Python is pinned to a machine path.

| File | Role |
| --- | --- |
| `update.sh` | Starts the run. Under cron (root) it runs `python:3.14-alpine` as the owner of this script, with the Docker socket and the stacks directory mounted. Then sends the heartbeat. |
| `config.toml` | The apps, their policy, releases and version sources, and the compose container. |
| `config.py` | Reads `config.toml`, and refuses anything it does not understand. |
| `update.py` | Lock, mail timing, weekly state, prune. `--dry-run`, `--check`, `--notify-only`, `--digest`. |
| `jobs.py` | One path per policy: what is bumped, mailed, or left alone, and the moves `auto` makes. |
| `auto.py` | What Workers AI is asked about a release, the checks that can only say no, the rollback copies and the commit. |
| `verdict.py` | The Workers AI request and its answer. |
| `notes.py` | Release notes and the pages they link, trimmed to breaking changes. |
| `composefile.py` | A compose file's image line changed with nothing else touched. |
| `releases.py` | Version rules, GitHub, Docker Hub, the Plex download feed, and QTS Live Update. |
| `mail.py` | The card mail. |
| `push.py` | The ntfy push for each update and each release to install by hand. |
| `runtime.py` | Docker over the socket, API 1.41 or the daemon's minimum if that is newer. Compose runs inside the compose container, with the files from the container labels. |
| `test_update.py` | The config and its mistakes, the same decisions as before the config, `wait_days`, rollback, Immich, Compose labels, the weekly state, the lock, the prune, release-check failures, and the mail. No Docker. |

To add an app, add a table to `config.toml` and an icon as `icons/<key>.png`, then check with `--dry-run`.

```
updates/update.sh --dry-run      decide only
updates/update.sh --check        confirm the compose container can run Compose, and list what a new
                                 patch, minor and major does to each app
updates/update.sh --digest       Sunday mail and prune, no updates
updates/update.sh --notify-only  mail new releases now, bump nothing
```

`python3 -m unittest test_update.py` runs the tests on a machine that has Python.
