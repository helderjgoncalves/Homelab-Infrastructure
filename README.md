# Homelab Infrastructure

A NAS-based private cloud for the family — self-hosted, declarative, and version-controlled. Everything in this repository should be sufficient to rebuild the stack from scratch on a fresh box.

> **Status:** living repository. Anything in production is described here; anything not described here shouldn't be in production.

---

## Goals

- **Family-grade reliability** — services my family relies on (photos, media, files) should "just work".
- **Privacy by default** — data lives at home, on hardware I control.
- **Reproducibility** — declarative config in Git; no hand-tuned servers, no undocumented snowflakes.
- **Zero trust** — no service is exposed without authentication, even on the LAN.
- **Boring tech** — favour well-known building blocks over bleeding-edge novelty.

---

## Status

What follows describes the **target state**. The checklists below track what is actually
versioned in this repository. A box is only ticked when the corresponding config lives
here in Git — running on the NAS but undocumented does **not** count.

Legend: `[x]` done · `[~]` in progress · `[ ]` not started

### Foundations
- [x] Repository initialised (`.gitignore`, `.gitattributes`, `README.md`)
- [~] Folder layout scaffolded — `services/`, `ingress/`, `dns/`, `observability/` exist; `access/`, `infra/`, `scripts/` pending
- [ ] `docs/` with architecture diagram and ADR template
- [ ] Secret-handling convention documented (`*.example` files, secret store of record)
- [ ] Contributing / change-management notes

### Host & orchestration
- [ ] NAS bootstrap notes (`infra/nas/`)
- [ ] Docker / Compose baseline (versions, networks, common labels)

### Services
- [x] Immich (`services/immich/`)
- [ ] OpenCloud (`services/opencloud/`)
- [x] Portainer (`services/portainer/`)
- [x] qBittorrent (`services/qbittorrent/`)

### Ingress
- [x] Cloudflare Tunnel config (`ingress/cloudflared/`)
- [x] Reverse proxy deployment — Nginx Proxy Manager (`ingress/reverse-proxy/`)
- [ ] Reverse proxy: routing rules *(NPM stores these in SQLite, not in Git)*
- [ ] Reverse proxy: TLS config (internal HTTPS everywhere)
- [ ] Reverse proxy: shared middlewares (auth, rate-limit, headers)

### DNS
- [x] Pi-hole deployment (`dns/pihole/`)
- [ ] Local DNS rewrites (public hostnames → internal proxy) tracked in Git *(live in `pihole.toml`; capture pending)*
- [~] Blocklists / allowlists tracked in Git — adlists + deny-exact + deny-regex snapshotted in `dns/pihole/lists.yaml`; allow entries and groups still pending; no sync script yet

### Security (zero-trust)
- [ ] Cloudflare Access apps & policies as code (`access/cloudflare/`)
- [ ] OAuth / SSO identity provider wired up
- [ ] mTLS enforced on machine-to-machine routes
- [ ] Service tokens issued & rotated for automated clients
- [ ] Origin validation (signed JWT / client cert) enforced at the proxy
- [ ] HTTPS everywhere — no plaintext hop in the path

### Observability
- [~] Uptime monitoring — Uptime Kuma deployed (`observability/uptime/`); internal probes only, external probes pending
- [ ] Centralised logging stack
- [ ] Metrics & dashboards
- [ ] Alerting routes (who gets paged, how)

### Operations
- [ ] Backup jobs defined and scheduled
- [ ] Off-site backup target configured
- [ ] **Restore drill performed at least once** (and dated below)
- [ ] Disaster-recovery runbook in `docs/`
- [ ] Cert / token expiry monitoring

> Last restore drill: _never_

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
│   │ (tunnel)     │    │ (HTTPS, routes)│    │ Immich, OpenCloud,       │   │
│   └──────────────┘    └────────────────┘    │ Portainer, monitoring …  │   │
│                                             └──────────────────────────┘   │
│                                                                            │
│   ┌──────────────┐    ┌────────────────┐    ┌──────────────────────────┐   │
│   │ Pi-hole      │    │ Portainer      │    │ Observability stack      │   │
│   │ (DNS + local │    │ (Docker mgmt)  │    │ (logs, metrics, uptime,  │   │
│   │  rewrites)   │    │                │    │  alerting)               │   │
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
- **Declarative.** Every service is described as code (`docker-compose.yml`, env templates, proxy config, DNS rewrites, access policies). Config lives here; runtime data does not.

---

## Stack

### Compute & orchestration
- **NAS** — primary host (storage + Docker runtime).
- **Docker / Docker Compose** — service definition and lifecycle.
- **Portainer** — web UI for container ops; stacks deployed from this repo.

### Networking & ingress
- **Cloudflare Tunnel (`cloudflared`)** — outbound-only ingress from Cloudflare to the homelab.
- **Reverse proxy** — terminates TLS internally, routes by hostname to backend containers, enforces per-route auth.
- **Pi-hole** — recursive DNS for the LAN, ad/tracker blocking, **local DNS rewrites** mapping public hostnames → internal proxy IP so the same URL works inside and outside the house.

### Self-hosted services (family-facing)
- **Immich** — photo/video library and backup target for phones.
- **OpenCloud** — files, sync, sharing.
- *(plus ancillary services — see `services/`)*

### Out of scope (running on the NAS, not tracked here)
- **Plex** / **Plexamp** — managed outside this repository.

### Security
- **Cloudflare Access** — SSO (OAuth/OIDC) policies in front of human-facing apps.
- **mTLS** — required for machine-to-machine and selected sensitive routes.
- **Service tokens** — short-lived, scoped credentials for automated clients (mobile apps, sync agents, CI).
- **Origin validation** — backends verify the Cloudflare-signed JWT and/or client certificate; direct origin hits are rejected.
- **Secrets** — never in this repo. Stored in a password manager / secret store; injected via `.env` files and Docker secrets at deploy time. Templates (`*.example`) are versioned.

### Observability
- **Uptime monitoring** — synthetic checks from inside and outside the network.
- **Centralised logging** — container logs aggregated to a single store, queryable.
- **Metrics & dashboards** — host, container, and service-level metrics.
- **Alerting** — actionable notifications for outages, disk pressure, cert expiry, backup failures.

---

## Repository layout

> Target shape. The repo grows into this as items in **Status** get ticked off.

```
.
├── README.md                  # this file
├── .gitignore
├── .gitattributes
│
├── docs/                      # runbooks, diagrams, decisions (ADRs)
│
├── services/                  # one folder per stack, each is self-contained
│   ├── immich/
│   │   ├── docker-compose.yml
│   │   └── .env.example
│   ├── opencloud/
│   ├── portainer/
│   └── …
│
├── ingress/
│   ├── cloudflared/           # Cloudflare Tunnel (token-based; routes in CF dashboard)
│   │   ├── docker-compose.yml
│   │   └── .env.example
│   └── reverse-proxy/         # NPM container (rules/TLS live in NPM's SQLite, not Git)
│       ├── docker-compose.yml
│       └── .env.example
│
├── dns/
│   └── pihole/                # local DNS rewrites, blocklists, custom records
│
├── access/
│   └── cloudflare/            # Access apps & policies (as code)
│
├── observability/
│   ├── logging/
│   ├── metrics/
│   ├── uptime/
│   └── alerts/
│
├── infra/                     # host-level setup, IaC if/when adopted
│   ├── nas/                   # NAS bootstrap notes / scripts
│   └── ansible/               # optional, for repeatable host config
│
└── scripts/                   # maintenance helpers (backups, restores, audits)
```

---

## License

Personal infrastructure. No license granted — content is shared for reference.
