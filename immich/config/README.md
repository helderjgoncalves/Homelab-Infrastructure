# Immich settings as code

Immich reads all of its admin settings from a JSON file when
`IMMICH_CONFIG_FILE` is set ([docs](https://docs.immich.app/install/config-file)).
This folder is mounted read-only at `/config`; the file goes here as
`immich.json`, tracked in Git.

## Adopting it

1. Administration → Settings → **Export as JSON**. The export is the full
   config, defaults included.
2. Save it as `config/immich.json`. It may be trimmed to the keys that differ
   from the defaults: any key left out takes Immich's default.
3. **Check it for secrets before committing.** Immich does not interpolate
   variables in this file, so an enabled SMTP relay
   (`notifications.smtp.transport.password`) or OAuth
   (`oauth.clientSecret`) would put the credential in Git. If either is in
   use, this file cannot be tracked as is — leave Immich on UI settings.
4. Uncomment `IMMICH_CONFIG_FILE=/config/immich.json` in `.env`, then
   `docker compose up -d --force-recreate immich-server`.

From then on the admin UI is read-only: change a setting in the file and
recreate `immich-server` (it does not reload the file on its own).

## Traps

- An invalid value **stops the server** while a config file is in use;
  without one Immich only logs it. Check `docker compose logs immich-server`
  after every change.
- `IMMICH_LOG_LEVEL`, if set, overrides `logging.level` from the file.
