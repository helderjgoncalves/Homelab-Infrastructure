# Home Assistant

Home automation, as the Home Assistant Container install: no Supervisor, so no add-on store. Anything that would be an add-on runs as its own container in this stack.

- Host networking for device discovery (mDNS, SSDP, HomeKit, Cast). HTTP is bound to `127.0.0.1:8123`, so the only way in is Traefik: `home.hgoncalves.uk`, `private` (LAN + the owner's Tailscale devices). Not on the Cloudflare tunnel.
- That binding is not in Git. Since 2026.10 HA keeps its HTTP settings in the UI (Settings → System → Network), stored in `config/.storage/http`; a backup or snapshot restores it. On a fresh install, set them before anything else: listen on `127.0.0.1` only, trust `X-Forwarded-For` from proxy `127.0.0.1/32` (Traefik is on the host network too), and ban after 5 failed logins. Check with `netstat -tln | grep 8123` on the NAS: it must show `127.0.0.1:8123`, not `0.0.0.0:8123`.
- Image pinned by tag and digest. To upgrade, read the release's breaking changes, take a backup (below), bump both in `docker-compose.yml`, redeploy.
- Git tracks only `configuration.yaml`, `automations.yaml`, `scripts.yaml` and `scenes.yaml`. Everything else in `config/` is runtime state: `.storage/` holds auth tokens and integration credentials. The last three are rewritten by the UI, so yamllint skips them.

```
docker compose up -d
```

## First start

1. Open `https://home.hgoncalves.uk` and create the owner account. Enable MFA under Profile → Security.
2. Settings → System → Network → Home Assistant URL: `https://home.hgoncalves.uk` for both the local and the internet URL.
3. Settings → System → Backups: turn on automatic backups (to `config/backups`, also covered by the NAS snapshot) and store the encryption key in the password manager. A backup cannot be restored without it.
4. Uptime Kuma: add a *Docker Container* monitor for `homeassistant`. It reads the container healthcheck, since Kuma cannot reach HA's loopback port from its bridge network.

## Integrations

- **UPS (NUT):** QTS already runs a NUT server for the APC UPS on the NAS, and only accepts localhost, which is where HA connects from. Host `127.0.0.1`, port `3493`, UPS `qnapups`, user `admin` with the QTS default password. That user may also run UPS commands (for example cutting the load), which HA only offers as explicit device actions.
- **Zigbee:** Zigbee2MQTT in `../zigbee2mqtt`, not ZHA. The Dongle-M stays in coordinator mode at `tcp://192.168.1.109:6638`, channel 25. Add the MQTT integration in Home Assistant to `127.0.0.1:1883`, user `zigbee`, password in `zigbee2mqtt/data/secret.yaml`. Do not enable ZHA while that stack is running. The plugs have to be paired again. Add mains-powered bulbs early so the mesh has routers.
- **Homelab services:** Immich, Uptime Kuma, AdGuard Home, qBittorrent, Sonarr, Radarr, QNAP and ntfy are all built-in integrations. Each needs an API key or credentials from that service.

## Not here on purpose

- **Matter / Thread:** the NAS has IPv6 off, which Matter needs, and Thread needs a border router. If it comes later: `matterjs-server` (the Python Matter server is archived), plus a coordinator with a Thread radio.
- **HACS:** community code installed unreviewed; add it only for a specific need.
