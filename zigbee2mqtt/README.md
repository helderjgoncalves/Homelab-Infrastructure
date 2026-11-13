# Zigbee2MQTT

Coordinator for the SONOFF Dongle-M. Separate from the Home Assistant stack so an HA restart does not drop the Zigbee network.

- Dongle-M at `tcp://192.168.1.109:6638`, Ember adapter, channel 25.
- Mosquitto listens on `127.0.0.1:1883` only. User `zigbee`. Password in `data/secret.yaml`.
- Frontend: `https://z2m.hgoncalves.uk` (LAN + Tailscale). Token in `data/secret.yaml` (`auth_token`).
- Home Assistant MQTT integration: broker `127.0.0.1`, port `1883`, user `zigbee`, same password. Discovery is on.

Do not enable ZHA while this stack is running. The dongle accepts one TCP client. The two SONOFF plugs have to be paired again: this is a new network, not a copy of the ZHA one.

```
docker compose up -d
```
