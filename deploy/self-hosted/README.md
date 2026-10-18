# Self-hosted first-run profile

This profile builds the existing frontend image and one shared backend image for
`backend-api` and `backend`, matching the current Nginx routes. It is a first-run
candidate, not a claim of production, Linux, upgrade or backup acceptance. Use the
intended source checkout; local image tags do not prove an immutable release.

From the repository root, with Python 3 and Docker Compose available:

```bash
python3 scripts/configure_self_hosted.py ~/.local/share/hiveclaw/company \
  --public-origin http://127.0.0.1:3008
```

The command prints a configuration check and startup command using that exact
private `installation.env`. It creates independent random database, Redis,
application, JWT and encryption keys; the file is mode `0600`. An existing profile
is never overwritten. Back it up securely: changing the encryption key loses access
to stored credentials. Use the same profile/project ID when restarting.

Compose uses the explicit profile and dedicated named volumes. It never adopts
this checkout's `.env`, development databases, source bind mounts or Docker socket.
The schema-owner password reaches PostgreSQL and the one-shot `prepare` service.
API/runtime processes receive only `app_rls` credentials and enforce strict RLS.
Preparation checks the two URLs target the same database, runs the canonical
migrations, schedule migration, role grants and schema-readiness gate, then fixes
only the Agent volume directory's ownership. It does not inspect private work.

Downstream services wait for preparation to succeed. A failed/unknown preparation
stage blocks their initial startup; repair the reported phase and retry with the
same profile, retaining the database and keys. Inspect private service/database
logs when diagnosing a failed phase; preparation does not print child SQL or
connection secrets. Do not regenerate keys or remove volumes as a retry strategy.
Compose's [explicit environment files](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
and [dependency conditions](https://docs.docker.com/compose/how-tos/startup-order/)
are used for configuration and ordering. Keep `HIVE_INSTALL_*` shell overrides
unset unless intentionally overriding this profile. `config --quiet` validates the
recipe without printing resolved credentials.

The frontend is published only on loopback; databases and backend ports are not
published. Register the initial administrator locally before enabling a public
TLS reverse proxy. Set `--public-origin` to the intended browser origin and choose
`--port` if the loopback port is already occupied. Initial-owner protection belongs
at this ingress boundary while registration uses the existing first-user policy;
a future authenticated bootstrap can support a direct public-binding option.

After signing in, use **Control Plane → Get started with your company** to configure
an approved model, create/select an employee and run the native readiness check.
Configuration, a healthy container and a prepared schema are separate observations;
provider calls, authorized effects and a real first task still require their own
execution evidence. Connected employees use their own runtime. Existing work does
not require every optional SSO/channel/directory or computer integration.

Current validation covers the Compose CLI model/build plan and owned macOS native
processes with PostgreSQL 17/SCRAM and authenticated Redis. The recipe reuses the
repository's PostgreSQL 15/Redis 7 container baselines; their container execution,
Nginx/TLS, host sandbox capabilities and end-to-end installation remain unverified
while Linux testing is deferred. No default UI administrator password is installed.
