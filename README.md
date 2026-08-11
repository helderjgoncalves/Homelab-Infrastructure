# Homelab Infrastructure

A NAS-based private cloud — self-hosted, declarative, version-controlled. This repo holds every Compose definition and env template needed to stand the stacks back up on a fresh host. It is not a complete disaster-recovery kit on its own: see [What is not in Git](#what-is-not-in-git) for the state that lives elsewhere.

> **Status:** living repository. Anything in production is described here; anything not described here shouldn't be in production.

---

## Stacks

One folder per stack at the repo root. Each is a self-contained Compose project — managed by [Dockge](https://github.com/louislam/dockge), which reads them from the directory named in its `STACKS_DIR` (the repo checkout itself), or run with `docker compose up -d` from inside the folder.

| Stack | Purpose | Notes |
| --- | --- | --- |
| [`beszel/`](./beszel/) | Lightweight server + container monitoring | Hub + agent on the NAS; SQLite history |
| [`cloudflared/`](./cloudflared/) | Cloudflare Tunnel — outbound-only ingress | Token-based; routes managed in CF dashboard |
| [`devbox/`](./devbox/) | Debian bookworm SSH dev environment | Built from `dockerfile_inline` in the compose file; mounts the repo at `/projects`. SSH on `2222` is a deliberate LAN exception |
| [`dockge/`](./dockge/) | Compose stack manager (web UI) | Manages every stack in this repo from `STACKS_DIR` |
| [`homeassistant/`](./homeassistant/) | Home automation | `network_mode: host` + `privileged: true` for device/mDNS discovery |
| [`immich/`](./immich/) | Photo/video library + phone backup | Intel iGPU passthrough (`/dev/dri`); CPU machine-learning image |
| [`npm/`](./npm/) | Nginx Proxy Manager — TLS + per-host routing | Rules live in NPM's SQLite, not in Git |
| [`ntfy/`](./ntfy/) | Push notification server | Alert sink for Kuma + Beszel; iOS via the ntfy.sh upstream relay |
| [`opencloud/`](./opencloud/) | File sync + collaboration suite | OpenCloud + Collabora + Keycloak + LDAP behind Traefik |
| [`pihole/`](./pihole/) | Recursive DNS + ad/tracker blocking + local DNS rewrites | macvlan; needs `bootstrap.sh` once per host |
| [`qbittorrent/`](./qbittorrent/) | Torrent client | WebUI bound to loopback only |
| [`uptime-kuma/`](./uptime-kuma/) | Uptime monitoring | Liveness probes + outbound deadman heartbeat |

Each stack folder typically contains:

- `docker-compose.yml` (or `compose.yaml` for `devbox/` and `dockge/`)
- `.env.example` — copy to `.env` and fill in. Only stacks that actually
  reference variables have one; `homeassistant/`, `uptime-kuma/` and `devbox/`
  need no configuration
- optional `bootstrap.sh` for one-time host prep (e.g. Pi-hole's macvlan network)

Out of scope: Plex / Plexamp run on the NAS but are not tracked here.

---

## Architecture overview

```
                  ┌────────────────────────────────────────────┐
                  │                Internet                    │
                  └───────────────────┬────────────────────────┘
                                      │ HTTPS only
                          ┌───────────▼───────────┐
                          │  Cloudflare (Edge)    │
                          │  - DNS (public)       │
                          │  - WAF / TLS          │
                          │  - Access (OAuth/SSO) │
                          │  - mTLS / svc tokens  │
                          └───────────┬───────────┘
                                      │ Cloudflare Tunnel
                                      │ (no inbound ports)
┌─────────────────────────────────────▼──────────────────────────────────────┐
│                                  Homelab                                   │
│                                                                            │
│   ┌──────────────┐    ┌────────────────┐    ┌──────────────────────────┐   │
│   │ cloudflared  │───▶│ Reverse Proxy  │───▶│ Self-hosted services     │   │
│   │ (tunnel)     │    │ (NPM, HTTPS)   │    │ Immich, OpenCloud,       │   │
│   └──────────────┘    └────────────────┘    │ ntfy, qBittorrent,       │   │
│                                             │ devbox (SSH)             │   │
│                                             └──────────────────────────┘   │
│                                                                            │
│   ┌──────────────┐    ┌────────────────┐    ┌──────────────────────────┐   │
│   │ Pi-hole      │    │ Dockge         │    │ Monitoring               │   │
│   │ (DNS + local │    │ (stack mgmt)   │    │ Uptime Kuma + Beszel     │   │
│   │  rewrites)   │    │                │    │ → ntfy (push alerts)     │   │
│   └──────────────┘    └────────────────┘    └──────────────────────────┘   │
│                                                                            │
│              NAS (storage, Docker host, container runtime)                 │
└────────────────────────────────────────────────────────────────────────────┘
```

### Key design choices

- **No inbound ports.** All external traffic enters via **Cloudflare Tunnel**; the home router exposes nothing.
- **One name everywhere.** Public hostnames (e.g. `photos.example.com`) resolve to Cloudflare from the internet, and to internal IPs from the LAN via **Pi-hole local rewrites**. Same URLs, same TLS, same auth — anywhere.
- **HTTPS everywhere**, including LAN-only paths. No plaintext between hops.
- **Auth at the edge.** Cloudflare Access enforces SSO/OAuth for humans; service tokens and **mTLS** for machine-to-machine. Origin services additionally validate Cloudflare's signed JWT / client cert.
- **Loopback-bound UIs.** Service WebUIs publish to `127.0.0.1` so the normal path is via NPM + Cloudflare Access rather than a LAN bypass. Three deliberate exceptions, each for a structural reason:
  - **NPM's admin UI on `:81`** — NPM runs `network_mode: host` because it proxies every other stack over the host loopback, so a bind address cannot be set. Filter port 81 at the host firewall instead.
  - **devbox SSH on `:2222`** — the LAN dev entry point, so it must work without a tunnel. Public-key auth only, root login disabled.
  - **Pi-hole on its own macvlan IP** — a DNS server has to be reachable by LAN clients, and macvlan gives the container a real LAN address, so its admin UI is reachable there too.
- **Declarative.** Every stack is described as code (`docker-compose.yml`, env templates). Config lives here; runtime data does not.

---

## Monitoring

Four layers, each catches what the others can't, all alerts converge on **ntfy**.

| Layer | Tool | Where | Watches | Why it exists |
| --- | --- | --- | --- | --- |
| 1. Liveness | [Uptime Kuma](./uptime-kuma/) | NAS (internal) | Per-container Docker state + loopback HTTP-keyword probes | Catches "app crashed" / "container restart loop" before users notice |
| 2. Resources | [Beszel](./beszel/) | NAS (internal) | CPU, RAM, disk, temperature, per-container metrics | Catches "disk filling", "OOM kills", "thermal throttling" — thresholds, not liveness |
| 3. End-to-end | [Better Stack](https://betterstack.com/) (free tier) | External | Public probes of user-facing services (Immich, OpenCloud) | Validates the full path: DNS → CF edge → tunnel → cloudflared → NPM → app. Survives the NAS being completely down. |
| 4. Deadman | Better Stack heartbeat | External (Kuma pings out) | NAS itself is alive and on the internet | Layers 1–2 can't alert when they're the thing that's down. Kuma hits the heartbeat URL on every check; absence = page. |

**Alert sink:** all four layers fan out to a self-hosted ntfy instance → push to phone. One channel, one inbox, one rate-limited topic.

**Status page:** `status.hgoncalves.uk` — Better Stack-hosted, lists user-facing services only (Immich, OpenCloud). Bookmark for "is it me or is it down" before contacting support.

**Heartbeat wiring:** the Kuma monitor that probes the Better Stack heartbeat URL doubles as the deadman ping — no cron, no extra moving parts. Period 5 min + grace 5 min = ~10 min worst-case detection.

### Why not just one tool?

- Kuma alone: silent if the NAS dies, blind to thresholds, can't see the public path.
- Beszel alone: doesn't probe HTTP endpoints or alert on "container missing".
- Better Stack alone: 10 free monitors won't cover every container; no resource metrics.

Each tool does what it's cheapest at. Total cost: $0 + ~150 MB RAM.

---

## Outbound email

Transactional user-facing mail (OpenCloud invitations, password resets, share notifications, …) is sent through **[Resend](https://resend.com/)** as an SMTP relay. Any stack that needs to send mail is configured with Resend's SMTP host + a per-stack API key; the sending domain (`hgoncalves.pt`) is DKIM/SPF-verified in Resend so mail lands in inboxes, not spam.

Kept out-of-repo: `SMTP_PASSWORD` (the Resend API key) lives in each stack's ignored `.env`, never in Git. The example templates (`.env.example`) document the key names but not the values.

Why external: running our own MTA on a residential IP is a losing game (RBLs, port 25 blocked at the ISP, DKIM/DMARC alignment friction). Resend's free tier covers homelab volume comfortably.

---

## Conventions

- **Secrets never in Git.** `.env` files are ignored; only `*.example` templates are versioned. See `.gitignore` for the full list.
- **One stack = one folder = one Compose project.** The repo checkout *is* Dockge's stacks dir: point `STACKS_DIR` at it and every folder becomes a managed stack. Dockge mounts that path to the identical path inside its container so the Docker daemon resolves compose paths correctly on the host.
- **Image tags.** Databases and anything holding state are pinned by digest, because an unattended major bump can migrate data irreversibly. Stateless services track a major (`:1`, `:2`) or `:latest` and are updated deliberately via Dockge, one stack at a time, after checking release notes — never all at once, so a regression is attributable to a single change.
- **Stack-relative paths.** Because each stack folder *is* the Dockge stack dir, runtime data lives next to the compose file (`./data`, `./appdata`, …). Host-specific paths (shares, external media) are passed in via env vars (`UPLOAD_LOCATION`, `QBT_DOWNLOADS_DIR`, …) so the compose stays portable across hosts.
- **`opencloud/` is a submodule** pointing at [`helderjgoncalves/opencloud-compose`](https://github.com/helderjgoncalves/opencloud-compose) (a fork of `opencloud-eu/opencloud-compose`). Clone with `--recurse-submodules`, or after cloning run `git submodule update --init`. To bump the pin: update inside `opencloud/`, then `git add opencloud && git commit` at the repo root.

---

## What is not in Git

Compose files and env templates live here; the state below does not, and a
rebuild needs it from elsewhere. Step-by-step restore procedures live in a
runbook kept on the NAS rather than in this repo, since they describe which
admin surfaces are reachable and how they are contained.

| State | Lives in | Recovered from |
| --- | --- | --- |
| Proxy hosts, TLS certs | NPM's SQLite (`npm/data`, `npm/letsencrypt`) | NAS snapshot; certs re-issue on demand |
| DNS rewrites, allow/deny lists | Pi-hole (`pihole/etc-pihole`) | NAS snapshot; partially mirrored in [`pihole/lists.yaml`](./pihole/lists.yaml) |
| Monitors, notification channels | Uptime Kuma SQLite (`uptime-kuma/`) | NAS snapshot |
| Dashboards, alert rules | Beszel SQLite (`beszel/data`) | NAS snapshot |
| Photos, albums, faces | Immich upload dir + Postgres | NAS snapshot for originals, plus a logical `pg_dump` for the database, restored from the same point in time |
| Tunnel routes, Access policies, DNS | Cloudflare dashboard | Cloudflare account (no local copy) |
| Public probes, status page, heartbeat | Better Stack | Better Stack account (no local copy) |
| Secrets | per-stack `.env` on the host | Recreated from `.env.example` + password manager |

---

## License

Personal infrastructure. No license granted — content is shared for reference.
There is intentionally no `LICENSE` file: absent one, default copyright
applies and no reuse rights are granted.
