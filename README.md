# Homelab Infrastructure

A NAS-based private cloud — self-hosted, declarative, version-controlled. Everything here is enough to redeploy the stack from scratch on a fresh host.

> **Status:** living repository. Anything in production is described here; anything not described here shouldn't be in production.

---

## Stacks

One folder per stack at the repo root. Each is a self-contained Compose project — drop into [Dockge](https://github.com/louislam/dockge) under `/opt/stacks/<name>` or run with `docker compose up -d` from inside the folder.

| Stack | Purpose | Notes |
| --- | --- | --- |
| [`beszel/`](./beszel/) | Lightweight server + container monitoring | Hub + agent on the NAS; SQLite history |
| [`cloudflared/`](./cloudflared/) | Cloudflare Tunnel — outbound-only ingress | Token-based; routes managed in CF dashboard |
| [`devbox/`](./devbox/) | Ubuntu 22.04 SSH dev environment | Built locally from `Dockerfile`; mounts the repo at `/projects` |
| [`dockge/`](./dockge/) | Compose stack manager (web UI) | Manages every stack in this repo from `/opt/stacks/<name>` |
| [`immich/`](./immich/) | Photo/video library + phone backup | Intel iGPU + OpenVINO ML |
| [`npm/`](./npm/) | Nginx Proxy Manager — TLS + per-host routing | Rules live in NPM's SQLite, not in Git |
| [`ntfy/`](./ntfy/) | Push notification server | Alert sink for Kuma + Beszel; iOS via the ntfy.sh upstream relay |
| [`opencloud/`](./opencloud/) | File sync + collaboration suite | OpenCloud + Collabora + Keycloak + LDAP behind Traefik |
| [`pihole/`](./pihole/) | Recursive DNS + ad/tracker blocking + local DNS rewrites | macvlan; needs `bootstrap.sh` once per host |
| [`qbittorrent/`](./qbittorrent/) | Torrent client | WebUI bound to loopback only |
| [`uptime-kuma/`](./uptime-kuma/) | Uptime monitoring | Liveness probes + outbound deadman heartbeat |

Each stack folder typically contains:

- `docker-compose.yml`
- `.env.example` — copy to `.env` and fill in
- optional `bootstrap.sh` for one-time host prep (e.g. Pi-hole's macvlan network)
- optional `Dockerfile` for locally-built images (e.g. devbox)

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
- **Loopback-bound UIs.** Service WebUIs bind to `127.0.0.1` so the only public path is via NPM + Cloudflare Access — no LAN bypass.
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

**Status page:** `status.hgoncalves.pt` — Better Stack-hosted, lists user-facing services only (Immich, OpenCloud). Bookmark for "is it me or is it down" before contacting support.

**Heartbeat wiring:** the Kuma monitor that probes the Better Stack heartbeat URL doubles as the deadman ping — no cron, no extra moving parts. Period 5 min + grace 5 min = ~10 min worst-case detection.

### Why not just one tool?

- Kuma alone: silent if the NAS dies, blind to thresholds, can't see the public path.
- Beszel alone: doesn't probe HTTP endpoints or alert on "container missing".
- Better Stack alone: 10 free monitors won't cover every container; no resource metrics.

Each tool does what it's cheapest at. Total cost: $0 + ~150 MB RAM.

---

## Conventions

- **Secrets never in Git.** `.env` files are ignored; only `*.example` templates are versioned. See `.gitignore` for the full list.
- **One stack = one folder = one Compose project.** Mirrors Dockge's `/opt/stacks/<name>` layout so the repo can be cloned (or symlinked) straight into Dockge's stacks dir.
- **Stack-relative paths.** Because each stack folder *is* the Dockge stack dir, runtime data lives next to the compose file (`./data`, `./appdata`, …). Host-specific paths (shares, external media) are passed in via env vars (`UPLOAD_LOCATION`, `QBT_DOWNLOADS_DIR`, …) so the compose stays portable across hosts.
- **`opencloud/` is a submodule** pointing at [`helderjgoncalves/opencloud-compose`](https://github.com/helderjgoncalves/opencloud-compose) (a fork of `opencloud-eu/opencloud-compose`). Clone with `--recurse-submodules`, or after cloning run `git submodule update --init`. To bump the pin: update inside `opencloud/`, then `git add opencloud && git commit` at the repo root.

---

## License

Personal infrastructure. No license granted — content is shared for reference.
