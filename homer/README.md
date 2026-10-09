# Homer

Static start page for the homelab. Pure nginx + a YAML file: no backend, no database, ~10–20 MB RAM.

- Config: [`assets/config.yml`](./assets/config.yml) — edit and refresh the browser, no restart.
- Bound to `127.0.0.1:8085`; served by Traefik as `dash.hgoncalves.uk`.
- Every link points at a Traefik route over HTTPS; a new service needs its route in `traefik/dynamic/routes.yml` before it goes in `config.yml`.
- Look: `assets/custom.css` (the only place styling lives) mirrors the hgoncalves.uk design (Inter, blue gradient header, soft cards, auto light/dark). `assets/logo.svg` is the site favicon, inverted.
- Icons load from the dashboard-icons CDN; drop files in `assets/tools/` and use `logo: assets/tools/x.png` to self-host them.

```
docker compose up -d
```
