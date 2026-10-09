# AdGuard Home on the NAS

Recursive DNS + ad/tracker blocking, forwarding upstream over **DNS-over-HTTPS**.
Replaced the Pi-hole stack, which has since been removed from the repo. Config
in `.env` (copy `.env.example`) plus `conf/AdGuardHome.yaml.example`.

Depends on host-side plumbing in [`nas-host/`](../nas-host/): the macvlan
network, the shim that lets the NAS reach this container, and the boot hook that
points `/etc/resolv.conf` at it.

## Why

Pi-hole forwarded to `1.1.1.1` / `1.0.0.1` over plain UDP/53, so the ISP saw
every name the household resolved. Pi-hole's FTL has no DoH client of its own,
and the upstream fix the Pi-hole docs describe — `cloudflared proxy-dns` — was
deprecated by Cloudflare in November 2025 and breaks on cloudflared builds
after 2026-02-02, so that route is closed.

AdGuard Home speaks DoH/DoT/DoQ upstream natively, with no sidecar proxy.

Two honest caveats about what that buys:

- It moves DNS visibility from the ISP to Cloudflare. It does not remove an
  observer — and Cloudflare already saw these queries.
- Cleartext TLS SNI and destination IPs still reveal most of the same
  browsing picture. This closes one channel, not the category.

The larger benefit is incidental: **the whole configuration is one YAML file.**
Pi-hole's `lists.yaml` existed only because Pi-hole's list
and domain state lives in `gravity.db` (SQLite), so it had to be transcribed by
hand — and it carried a "Not yet captured" backlog (allowlists, groups,
clients, rewrites) plus a TODO for a REST-API sync script that was never
written. All of that is just `conf/AdGuardHome.yaml` now.

## Architecture

The hard part is host networking, not the DNS server, and that lives in
[`nas-host/`](../nas-host/). AdGuard Home attaches to the external macvlan
network (still named `macvlan_pihole` from the Pi-hole days) and relies on the
shim — `nas-host/scripts/dns-watchdog.sh` is driven entirely by `DNS_IP` /
`SHIM_IP` / `PARENT_IFACE` and has no idea what is listening behind the address.

```
   LAN clients  192.168.1.x
        │
        ▼
   ┌──────────────────────────────┐
   │ adguard     ADGUARD_IP       │   macvlan child of eth0
   └──────────────┬───────────────┘   (network: macvlan_pihole)
                  │ DoH / 443
                  ▼
            1.1.1.1  1.0.0.1
```

The shim, `/etc/resolv.conf`, `dockerd --dns` and the ~3-minute unfiltered
window at boot are covered in the [`nas-host/` README](../nas-host/README.md).
`ADGUARD_IP` here and `DNS_IP` in `nas-host/.env` must stay the same address.

## Layout

| Path | Tracked | What |
| --- | --- | --- |
| `docker-compose.yml` | yes | Image pinned to `v0.107.79` — see the `schema_version` note below |
| `.env.example` | yes | Copy to `.env`; also feeds the config renderer |
| `conf/AdGuardHome.yaml.example` | yes | **Source of truth.** Placeholder tokens, no secrets, no host specifics |
| `conf/AdGuardHome.yaml` | no | Live config. Rendered from the template; rewritten in place by AdGuard Home |
| `scripts/render-config.sh` | yes | Renders the template; `diff` shows drift |
| `work/` | no | Query log, statistics, downloaded lists |

## Setup

```sh
cp .env.example .env
# fill in ADGUARD_IP, LAN_SUBNET, LAN_GATEWAY, PUBLIC_ZONE, NAS_IP, and the
# bcrypt hash — the .env.example comments give the htpasswd one-liner

scripts/render-config.sh render   # writes conf/AdGuardHome.yaml (chmod 600)
sudo docker compose up -d
```

The macvlan network must already exist — `nas-host/scripts/bootstrap.sh`
creates it (once per host).

Admin UI: `http://<ADGUARD_IP>`. The pre-seeded `users:` entry skips the
first-run setup wizard — if you see the wizard instead, the config was not
rendered (most likely an empty `ADGUARD_ADMIN_PASSWORD_HASH`), and AdGuard
Home has booted on its own defaults.

## Migration from Pi-hole

AdGuard Home was staged on a spare address next to Pi-hole, tested from a LAN
client, then took over `192.168.1.2`. Pi-hole's stack, its `etc-pihole/` data
and `lists.yaml` have been removed, so there is no container-swap rollback any
more. Going back would mean rebuilding Pi-hole from scratch. The sections below
record what was carried across, and how.

## What was translated

Taken from Pi-hole's live `pihole.toml` and generated `dnsmasq.conf`:

| Pi-hole | AdGuard Home | |
| --- | --- | --- |
| `upstreams = ["1.1.1.1","1.0.0.1"]` (UDP/53) | `upstream_dns:` two `https://` entries | set |
| `server=/lan/<router>` | `upstream_dns: '[/<LOCAL_DOMAIN>/]<LAN_GATEWAY>'` | set |
| `rev-server=192.168.1.128/25,<router>` | `local_ptr_upstreams` | set |
| `local-service`, `listeningMode = LOCAL` | `allowed_clients: <LAN_SUBNET>` | set |
| 14 × `address=/host.zone/<nas>` + `local=/host.zone/` | 12 × `filtering.rewrites` (2 dropped, below) | set |
| 5 adlists from `lists.yaml` | `filters:` — 4 carried over, 1 dropped, 1 added (below) | set |
| `deny_exact tracker1.bt.moack.co.kr` | dropped with the regex below | — |
| `deny_regex tracker\..*` | dropped — blocked public BitTorrent trackers | — |
| `dnssec` / `dns.dnssec = true` | `enable_dnssec` | default |
| `filter-rr=ANY` (RFC 8482) | `refuse_any` | default |
| `use_private_ptr_resolvers` for the above rev-server | `use_private_rdns` | default |
| `rateLimit` count=1000 / interval=60 | `ratelimit: 20` q/s — ~16.7 vs 20 | default |
| `cache-size=10000` entries | `cache_size` 4 MiB (**bytes**, not entries) | default |
| `dhcp.active = false` | `dhcp.enabled` | default |

The template only carries the **set** rows. Everything marked **default**
already matches Pi-hole's behaviour out of the box — verified against
`internal/home/config.go` at `v0.107.79` — so pinning it in the file would be
noise. `use-stale-cache=3600` has no cheap equivalent and was dropped; AdGuard
Home's `cache_optimistic` defaults off.

### Traps worth knowing

- **DoH upstreams use IP literals**, not `https://dns.cloudflare.com/dns-query`.
  A hostname upstream needs a bootstrap lookup, and Container Station hard-codes
  `dockerd --dns` at this very resolver — so the hostname form risks asking the
  resolver to resolve the thing it needs in order to resolve. Cloudflare's
  certificate carries `1.1.1.1` and `1.0.0.1` as IP SANs, so the literal form
  validates cleanly.
- **There is deliberately no `fallback_dns`.** Its default is empty, and it
  must stay that way: a plaintext fallback would silently leak over UDP/53
  whenever DoH hiccuped. Prefer hard failure to silent downgrade.
- **`schema_version: 34` is coupled to the pinned image.** It is
  `v0.107.79`'s `LastSchemaVersion`. Declaring it means AdGuard Home loads the
  file as-is instead of migrating it. A value *higher* than the image supports
  is a hard startup error, so bump the tag and this number together. A newer
  image migrates forward safely on its own, then rewrites the file — re-sync the
  template afterwards.
- **The rewrites are explicit, not a `*.<zone>` wildcard.** A wildcard would
  also capture every other name in that zone, including any meant to resolve to
  the public Cloudflare edge from inside the LAN.

### Two of Pi-hole's records were dropped

Pi-hole carried 14 split-horizon entries; only 12 came across. The two dropped
were verified stale against Nginx Proxy Manager's own database
(`npm/data/database.sqlite`, the authoritative list of what is actually
served):

- `wopi` — `is_deleted=1` in NPM. The collaboration/WOPI service moved
  in-process and is served on the OpenCloud domain under `/wopi`, so it needs
  no host of its own. `opencloud/AGENTS.md` already flagged the Pi-hole record
  and NPM proxy host as vestigial.
- `homeassistant` — no NPM proxy host at all, live or deleted. Home Assistant
  runs `network_mode: host` and is reached directly on the NAS address and port.

The remaining 12 rewrites are an exact set-match with NPM's 12 live
`*.<PUBLIC_ZONE>` proxy hosts — no host served without a rewrite, no rewrite
pointing at nothing. Worth re-checking if you add or retire a proxy host.

### Blocklists differ from Pi-hole's set

Four of Pi-hole's five lists carried over verbatim — the `adblock/` paths are
already AdGuard Home's native syntax, so no conversion was needed. Two changes,
both measured rather than guessed:

- **Dropped** Dandelion Sprout's Anti-Push Notifications: 75% of its 12,135
  domains were already covered by HaGeZi Pro + TIF, leaving ~3,000 unique.
- **Added** HaGeZi DoH/VPN/Proxy Bypass (16,012 rules). Without it a client
  running its own DoH walks straight past this resolver, which makes filtering
  optional from the client's side — the upstream DoH work does nothing about
  that.

Kept despite looking redundant: Perflyst SmartTV is **79% unique** against
Pro+TIF, and Pro and TIF themselves overlap only 27%, so neither subsumes the
other.

TIF is the full 2.37M-rule variant. The NAS has the memory for it; the reason
to consider `tif.medium` (735k) instead is false positives, not resources.

### Behaviour differences, and what does not come across

- **Reverse-lookup scope widens.** Pi-hole scoped `rev-server` to a `/25`.
  Stable AdGuard Home has no per-network field for this (`dns.private_networks`
  is 0.108-beta only), so it uses the RFC 6303 defaults and forwards PTR for the
  whole private range to the router. Benign here, but wider than before.
- **Query history does not migrate.** `pihole-FTL.db` stays behind; AdGuard Home
  starts with an empty query log. Defaults apply: 90 days of query log,
  1 day of statistics. Raise either in the UI if you want more.
- **Some dnsmasq knobs have no individual equivalent**: `domain-needed`,
  `bogus-priv`, `local=//`, `localise-queries`, the RFC 6761 special-domain
  handling. AdGuard Home's defaults land in roughly the same place, but not
  knob-for-knob.
- **`EDNS0ECS` is off** rather than on. Clients reach this resolver directly on
  the LAN, so there is no NAT to see through, and forwarding ECS would hand
  Cloudflare a client-subnet hint it does not need.

## Keeping the config in Git

The template is tracked; the live file is not, because AdGuard Home rewrites it
in place and it carries the admin password hash.

```sh
scripts/render-config.sh diff
```

Expect noise the first time — AdGuard Home strips comments and expands every
default on its first write. Look past that for real setting changes, and copy
those back into `conf/AdGuardHome.yaml.example` so the tracked file stays the
source of truth.

This is the one piece of manual discipline that survives the migration. It is
still a `diff` of one YAML file, against Pi-hole's alternative of transcribing
SQLite state from UI screenshots.

## Troubleshooting

```sh
sudo docker compose logs -f adguard         # startup errors, filter updates
sudo docker compose ps                      # health status
scripts/render-config.sh diff               # has the live config drifted?
nslookup github.com <ADGUARD_IP>            # from a LAN client, not the NAS
cat /etc/resolv.conf                        # after cutover: the DNS address
ip route show <ADGUARD_IP>/32               # expect: dev macvlan_shim0
```

`schema_version` errors on startup mean the pinned image is older than the
number in the config — align the two.

Container reports unhealthy while DNS plainly works: the healthcheck shells out
to busybox `wget`. If the image lacks it, drop the `healthcheck:` block; real
liveness probing belongs in [`uptime-kuma/`](../uptime-kuma/) anyway.

No resolution at all, and no obvious cause: the NAS has no other resolver — if
this container is down, the host has no DNS. Check the shim first
(`nas-host/` troubleshooting), then this container's logs.
