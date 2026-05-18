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
| [`immich/`](./immich/) | Photo/video library + phone backup | Intel iGPU + OpenVINO ML; uses `stack.env` |
| [`npm/`](./npm/) | Nginx Proxy Manager — TLS + per-host routing | Rules live in NPM's SQLite, not in Git |
| [`pihole/`](./pihole/) | Recursive DNS + ad/tracker blocking + local DNS rewrites | macvlan; needs `bootstrap.sh` once per host |
| [`qbittorrent/`](./qbittorrent/) | Torrent client | WebUI bound to loopback only |
| [`uptime-kuma/`](./uptime-kuma/) | Uptime monitoring | Internal probes only |

Each stack folder typically contains:

- `docker-compose.yml`
- `.env.example` (or `stack.env.example` for Immich) — copy to `.env` / `stack.env` and fill in
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
│   │ (tunnel)     │    │ (NPM, HTTPS)   │    │ Immich, qBittorrent,     │   │
│   └──────────────┘    └────────────────┘    │ devbox, …                │   │
│                                             └──────────────────────────┘   │
│                                                                            │
│   ┌──────────────┐    ┌────────────────┐    ┌──────────────────────────┐   │
│   │ Pi-hole      │    │ Dockge         │    │ Uptime Kuma              │   │
│   │ (DNS + local │    │ (stack mgmt)   │    │ (internal probes)        │   │
│   │  rewrites)   │    │                │    │                          │   │
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

## Conventions

- **Secrets never in Git.** `.env` / `stack.env` files are ignored; only `*.example` templates are versioned. See `.gitignore` for the full list.
- **One stack = one folder = one Compose project.** Mirrors Dockge's `/opt/stacks/<name>` layout so the repo can be cloned (or symlinked) straight into Dockge's stacks dir.
- **Host-agnostic compose.** Host paths come from env vars (`UPLOAD_LOCATION`, `PIHOLE_DIR`, `NPM_DIR`, …), never hard-coded.

---

## License

Personal infrastructure. No license granted — content is shared for reference.
