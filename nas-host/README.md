# NAS host plumbing

Host-side setup the LAN DNS container ([`adguard/`](../adguard/)) depends on. Not a
Compose stack: these scripts run on the QNAP host itself, as root. Config in `.env`
(copy `.env.example`).

They used to live in `pihole/scripts/`. They moved here when Pi-hole was retired, so
they no longer belong to whichever DNS app happens to be running.

## Architecture

```
   LAN clients  192.168.1.x
        │
        ▼
   ┌──────────────────────────────┐
   │ adguard     192.168.1.2      │   macvlan child of eth0
   └──────────────▲───────────────┘   (network: macvlan_pihole)
                  │
 ═══ NAS host ════╪════════════════════════════════════════════════
                  │
    eth0 ─────────✗   the kernel drops traffic from a parent
    192.168.1.206 │   interface to its own macvlan child
                  │
    macvlan_shim0 ✓   a *sibling* child is allowed — this is the
    192.168.1.3   │   only path the NAS has to the DNS container
                  ▲
                  │  address + /32 route, rebuilt every boot by
                  │  dns-watchdog.sh
         ┌────────┴─────────┐
         │                  │
   host resolver       containers
   /etc/resolv.conf    127.0.0.11 ─► dockerd --dns 192.168.1.2
   nameserver          (hard-coded by Container Station)
   192.168.1.2
```

Two things must be redone on every boot, which is all `dns-watchdog.sh` does:

1. **Build the shim.** Without it neither the host nor any container can reach the
   DNS container.
2. **Overwrite `/etc/resolv.conf`.** QTS resets it to `127.0.1.1` — its own dnsmasq —
   on every boot, and that dnsmasq binds upstreams to `eth0`, the one path the kernel
   blocks, so it forwards to the router instead. Setting DNS in Control Panel does
   *not* fix this; it only edits dnsmasq's upstream list.

`autorun.sh`, installed on the DOM, runs the watchdog once per boot.

Caveat: in the ~3 min before the hook fires, the host still resolves via dnsmasq's
fallback upstream, so host lookups in that window are **not** filtered.

### Names kept from the Pi-hole days

Two names still say "pihole"/"shim" because they exist on the live host, and renaming
them would break things:

- **`macvlan_pihole`** — the Docker network. Renaming means detaching the DNS
  container and recreating the network. It is a value in `.env` (`MACVLAN_NETWORK`),
  not hard-coded.
- **`macvlan_shim0`** — the host interface. A new name would make the watchdog build a
  second shim next to the live one, both claiming `SHIM_IP`.

## Scripts

| Script | When |
| --- | --- |
| `bootstrap.sh` | Once per host, as root. Creates the macvlan network, the shim, installs the boot hook. Idempotent. |
| `dns-watchdog.sh` | Automatic at boot. By hand only if boot didn't, or something broke the shim. |
| `install-autorun.sh` | As root, **whenever `autorun.sh` changes or this folder moves**. Substitutes host paths into it, then writes it to the DOM (`/dev/mmcblk0p6`) — the only startup script QTS runs, and only while Control Panel → Hardware → "Run user defined processes during startup" is ticked. |
| `autorun.sh` | Template for the boot hook. Holds `__PLACEHOLDERS__` rather than real paths, so no host layout is committed — **not runnable as-is**. Retries the watchdog 10× at 15s. |
| `dns-diag.sh` | Read-only state dump. Runs automatically around each boot repair. |

A drop in `/etc/config/autorun.d/` is **never executed** on this NAS. Only the DOM copy runs.

The DOM copy holds the watchdog's **absolute path**. Moving or renaming this folder or
`dns-watchdog.sh` silently breaks the next boot until `install-autorun.sh` is re-run.

## Troubleshooting

```sh
cat /tmp/autorun-boot.log         # did the hook run? expect "dns-watchdog: ok on attempt 1"
cat scripts/dns-watchdog.log      # what it repaired
cat scripts/dns-diag.log          # full state, before and after
cat /etc/resolv.conf              # expect: nameserver 192.168.1.2
ip route show 192.168.1.2/32      # expect: dev macvlan_shim0
sudo scripts/dns-watchdog.sh      # force a repair now
```

`127.0.1.1` in `resolv.conf` minutes after boot means the hook didn't run — check the
autorun log and that the Control Panel checkbox is still ticked.

`/etc/resolv.conf` lists only the DNS container: if it is down, the NAS has no DNS.
