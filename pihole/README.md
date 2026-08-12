# Pi-hole on the NAS

Pi-hole runs on its own LAN IP (`192.168.1.2`) via a Docker **macvlan** network, so it
can bind port 53 without colliding with the NAS. Config in `.env` (copy `.env.example`).

## Architecture

```
   LAN clients  192.168.1.x
        │
        ▼
   ┌──────────────────────────────┐
   │ pihole      192.168.1.2      │   macvlan child of eth0
   └──────────────▲───────────────┘
                  │
 ═══ NAS host ════╪════════════════════════════════════════════════
                  │
    eth0 ─────────✗   the kernel drops traffic from a parent
    192.168.1.206 │   interface to its own macvlan child
                  │
    macvlan_shim0 ✓   a *sibling* child is allowed — this is the
    192.168.1.3   │   only path the NAS has to Pi-hole
                  ▲
                  │  address + /32 route, rebuilt every boot by
                  │  pihole-watchdog.sh
         ┌────────┴─────────┐
         │                  │
   host resolver       containers
   /etc/resolv.conf    127.0.0.11 ─► dockerd --dns 192.168.1.2
   nameserver          (hard-coded by Container Station)
   192.168.1.2
```

Two things must be redone on every boot, which is all `pihole-watchdog.sh` does:

1. **Build the shim.** Without it neither the host nor any container can reach Pi-hole.
2. **Overwrite `/etc/resolv.conf`.** QTS resets it to `127.0.1.1` — its own dnsmasq —
   on every boot, and that dnsmasq binds upstreams to `eth0`, the one path the kernel
   blocks, so it forwards to the router instead. Setting DNS in Control Panel does
   *not* fix this; it only edits dnsmasq's upstream list.

`autorun.sh`, installed on the DOM, runs the watchdog once per boot.

Caveat: in the ~3 min before the hook fires, the host still resolves via dnsmasq's
fallback upstream, so host lookups in that window are **not** filtered.

## Scripts

| Script | When |
| --- | --- |
| `bootstrap.sh` | Once per host, as root. Creates the macvlan network, the shim, installs the boot hook. Idempotent. |
| `pihole-watchdog.sh` | Automatic at boot. By hand only if boot didn't, or something broke the shim. |
| `install-autorun.sh` | As root, **only when `autorun.sh` changes**. Substitutes host paths into it, then writes it to the DOM (`/dev/mmcblk0p6`) — the only startup script QTS runs, and only while Control Panel → Hardware → "Run user defined processes during startup" is ticked. |
| `autorun.sh` | Template for the boot hook. Holds `__PLACEHOLDERS__` rather than real paths, so no host layout is committed — **not runnable as-is**. Retries the watchdog 10× at 15s. |
| `pihole-diag.sh` | Read-only state dump. Runs automatically around each boot repair. |

A drop in `/etc/config/autorun.d/` is **never executed** on this NAS. Only the DOM copy runs.

## Stack

```sh
sudo docker compose up -d      # bootstrap.sh must have run first
```

## Troubleshooting

```sh
cat /tmp/autorun-boot.log         # did the hook run? expect "pihole: ok on attempt 1"
cat scripts/pihole-watchdog.log   # what it repaired
cat scripts/pihole-diag.log       # full state, before and after
cat /etc/resolv.conf              # expect: nameserver 192.168.1.2
ip route show 192.168.1.2/32      # expect: dev macvlan_shim0
sudo scripts/pihole-watchdog.sh   # force a repair now
```

`127.0.1.1` in `resolv.conf` minutes after boot means the hook didn't run — check the
autorun log and that the Control Panel checkbox is still ticked.

`/etc/resolv.conf` lists only Pi-hole: if the container is down, the NAS has no DNS.
