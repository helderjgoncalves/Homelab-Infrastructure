# AdGuard Home on the NAS

Recursive DNS + ad/tracker blocking, forwarding upstream over **DNS-over-HTTPS**.
DNS resolvers, blocklists and DNS rewrites are tracked in `conf/AdGuardHome.yaml.example`.

Depends on host-side plumbing in [`nas-host/`](../nas-host/): the macvlan
network, the shim that lets the NAS reach this container, and the boot hook that
points `/etc/resolv.conf` at it.

## Why

AdGuard Home speaks DoH/DoT/DoQ upstream natively, with no sidecar proxy, so
the ISP no longer sees every name the household resolves.

Two honest caveats about what that buys:

- It moves DNS visibility from the ISP to Cloudflare. It does not remove an
  observer.
- Cleartext TLS SNI and destination IPs still reveal most of the same
  browsing picture. This closes one channel, not the category.

## Architecture

The hard part is host networking, not the DNS server, and that lives in
[`nas-host/`](../nas-host/). AdGuard Home attaches to the external macvlan
network (named `macvlan_pihole` for historical reasons) and relies on the
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
| `docker-compose.yml` | yes | Image pinned to `v0.107.79` |
| `.env.example` | yes | Copy to `.env`; macvlan address and network for Compose |
| `conf/AdGuardHome.yaml.example` | yes | DNS resolvers, blocklists and DNS rewrites, with placeholder tokens |
| `conf/AdGuardHome.yaml` | no | Live config, written by AdGuard Home; holds the admin password hash |
| `work/` | no | Query log, statistics, downloaded lists |

## Setup

```sh
cp .env.example .env                 # fill in ADGUARD_IP
sudo docker compose up -d
```

The macvlan network must already exist — `nas-host/scripts/bootstrap.sh`
creates it (once per host).

Then open `http://<ADGUARD_IP>`, run the setup wizard (web UI on port 80), and
in the UI:

- restrict allowed clients to the LAN subnet — empty means an open resolver;
- add the resolvers, blocklists and rewrites from
  `conf/AdGuardHome.yaml.example`.

## Traps worth knowing

- **DoH upstreams use IP literals**, not `https://dns.cloudflare.com/dns-query`.
  A hostname upstream needs a bootstrap lookup, and Container Station hard-codes
  `dockerd --dns` at this very resolver — so the hostname form risks asking the
  resolver to resolve the thing it needs in order to resolve. Cloudflare's
  certificate carries `1.1.1.1` and `1.0.0.1` as IP SANs, so the literal form
  validates cleanly.
- **There is deliberately no `fallback_dns`.** Its default is empty, and it
  must stay that way: a plaintext fallback would silently leak over UDP/53
  whenever DoH hiccuped. Prefer hard failure to silent downgrade.
- **The rewrites are a `*.<zone>` wildcard plus exceptions.** The wildcard
  captures every name in the zone, including ones meant to resolve to the
  public Cloudflare edge from inside the LAN. Each of those (`www`, `status`)
  needs its own `answer: A` rewrite, which keeps the upstream records.

## Blocklists

- **Perflyst SmartTV** looks redundant but is **79% unique** against HaGeZi
  Pro + TIF, and Pro and TIF themselves overlap only 27%, so neither subsumes
  the other.
- **TIF** is the full 2.37M-rule variant. The NAS has the memory for it; the
  reason to consider `tif.medium` (735k) instead is false positives, not
  resources.
- **HaGeZi encrypted-DNS** stops clients running their own DoH/DoT from walking
  straight past this resolver, which would make filtering optional from the
  client's side. The broader DoH/VPN/Proxy Bypass list is kept but disabled.

## Other behaviour worth knowing

- **Reverse lookups cover the whole private range.** Stable AdGuard Home has no
  per-network field for this (`dns.private_networks` is 0.108-beta only), so it
  uses the RFC 6303 defaults and forwards PTR for every private range to the
  router.
- **`EDNS0ECS` is off.** Clients reach this resolver directly on the LAN, so
  there is no NAT to see through, and forwarding ECS would hand Cloudflare a
  client-subnet hint it does not need.

## Keeping the config in Git

When you change a resolver, blocklist or rewrite in the UI, update
`conf/AdGuardHome.yaml.example` to match.

## Troubleshooting

```sh
sudo docker compose logs -f adguard         # startup errors, filter updates
sudo docker compose ps                      # health status
nslookup github.com <ADGUARD_IP>            # from a LAN client, not the NAS
cat /etc/resolv.conf                        # expect: the DNS address
ip route show <ADGUARD_IP>/32               # expect: dev macvlan_shim0
```

Container reports unhealthy while DNS plainly works: the healthcheck shells out
to busybox `wget`. If the image lacks it, drop the `healthcheck:` block; real
liveness probing belongs in [`uptime-kuma/`](../uptime-kuma/) anyway.

No resolution at all, and no obvious cause: the NAS has no other resolver — if
this container is down, the host has no DNS. Check the shim first
(`nas-host/` troubleshooting), then this container's logs.
