# Zigbee2MQTT

Coordinator for the SONOFF Dongle-M. Separate from the Home Assistant stack so an HA restart does not drop the Zigbee network.

- Dongle-M at `tcp://192.168.1.109:6638`, Ember adapter, channel 25.
- Mosquitto listens on `127.0.0.1:1883` only. User `zigbee`. Password in `data/secret.yaml`.
- Frontend: `https://z2m.hgoncalves.uk` (LAN + Tailscale). Token in `data/secret.yaml` (`auth_token`).
- Home Assistant MQTT integration: broker `127.0.0.1`, port `1883`, user `zigbee`, same password. Discovery is on.

Images: Zigbee2MQTT on `2.14`, Mosquitto on `2.1-alpine`. [`updates/`](../updates/) takes their patches each morning and rolls one back if it is not healthy. A new minor is in the Sunday mail; read its breaking changes and move the tag in `docker-compose.yml` by hand.

Do not enable ZHA while this stack is running. The dongle accepts one TCP client. The two SONOFF plugs have to be paired again: this is a new network, not a copy of the ZHA one.

```
docker compose up -d
```
