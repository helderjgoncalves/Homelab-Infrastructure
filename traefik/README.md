# Traefik

Reverse proxy for every stack: TLS termination + one route per hostname, with the routing table in Git. Replaced NPM in 2026-10.

- **Routes:** [`dynamic/routes.yml`](./dynamic/routes.yml). Hot-reloaded on save — no restart.
- **Certs:** one Let's Encrypt cert per hostname, issued automatically on the first request via Cloudflare DNS-01. Never a wildcard (HTTP/2 connection coalescing). Stored in `acme/acme.json` (gitignored).
- **DNS:** AdGuard rewrites `*.hgoncalves.uk` → NAS, so new hostnames need no DNS change. `www` is excepted (answer `A`).
- **Access:** `lan-only` middleware = `192.168.1.0/24`, the same rule as NPM's "Local Only Boards" list. Public routes (Immich `photos`, OpenCloud `drive`, Collabora) omit it.
- **Dashboard:** `https://traefik.hgoncalves.uk` (LAN only).

## Adding a service

Add a router and a service to `dynamic/routes.yml`:

```yaml
    myapp:
      rule: Host(`myapp.hgoncalves.uk`)
      service: myapp
      middlewares: [lan-only]
# ...
    myapp:
      loadBalancer: {servers: [{url: http://127.0.0.1:1234}]}
```

That's it — no DNS, no cert request, no restart.

## History

Replaced Nginx Proxy Manager in 2026-10. NPM's stack was removed from the repo; its last version is in Git history (`git log --all -- npm/`).

## Router (ONT) cookie fix

NPM's custom nginx rule for `router.hgoncalves.uk` is replaced by the `router-cookie` middleware (the [htransformation](https://github.com/tomMoulard/htransformation) plugin, downloaded from the Traefik plugin catalog at startup — first boot needs internet). If the ONT login ever breaks, open the ONT by IP.

## Hostname renames (2026-10)

`immich` → `photos`, `cloud` → `drive`, `uptime` → `kuma`, `seerr` → `requests`. The old names are not served (no redirects). Outside this repo, at cutover:

- **OpenCloud:** `OC_DOMAIN=drive.hgoncalves.uk` in `opencloud/.env`, then delete `idp/identifier-registration.yaml` in the OpenCloud data dir so the built-in IdP regenerates its client redirect URIs, and redeploy. Desktop/mobile clients must re-add the account.
- **Immich:** Administration → Settings → Server → External domain = `https://photos.hgoncalves.uk`; update the server URL in the phone apps.
- **Seerr:** Settings → General → Application URL = `https://requests.hgoncalves.uk`.
- **Uptime Kuma:** Settings → General → Primary Base URL = `https://kuma.hgoncalves.uk`; update monitors that probe the old names.
- **Cloudflare:** add tunnel public hostnames (and Access apps) for `photos` and `drive`; remove `immich`/`cloud`. Update Better Stack monitors.

## Not carried over from NPM

- **"Block common exploits"** — NPM's URL regex filter. Cloudflare's WAF covers the public routes; LAN routes are allowlisted.
