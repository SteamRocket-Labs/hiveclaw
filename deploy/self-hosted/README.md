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

## Offline backup and fresh-target recovery

The host verification command requires Python 3.11 or newer.

The opt-in maintenance image uses the same PostgreSQL major as this recipe. It
backs up PostgreSQL and the complete Agent volume together with the original
private installation profile. It does not send data to an external service.
Backups contain private work and encryption keys: keep the parent directory
private, on a separate disk or protected backup destination, and retain the
reported manifest SHA-256 separately. Hashes detect changed bytes; they are not
proof that a bundle from an untrusted party is safe to restore.

From the same approved source checkout as the installation, set these paths and
use the exact profile for every command:

```bash
HIVE_RECOVERY_PROFILE=/secure/company/installation.env
HIVE_RECOVERY_BACKUPS=/secure/backups
mkdir -p "$HIVE_RECOVERY_BACKUPS"
chmod 700 "$HIVE_RECOVERY_BACKUPS"
dc() { docker compose --env-file "$HIVE_RECOVERY_PROFILE" -f deploy/self-hosted/compose.yml "$@"; }
dc --profile maintenance build recovery
dc stop frontend backend-api backend
dc ps --all
```

Stop any other process that can write this database or Agent volume, including
one-shot preparation and external filesystem writers. `--offline` is the
operator's confirmation of that condition. The command also refuses other live
database clients and prepared transactions, but that observation alone cannot
prove that a filesystem writer has stopped. Do not copy a changing live volume
and call it a consistent recovery point.

```bash
dc --profile maintenance run --rm -T --no-deps \
  --volume "$HIVE_RECOVERY_PROFILE:/recovery/installation.env:ro" \
  --volume "$HIVE_RECOVERY_BACKUPS:/recovery/backups" \
  recovery backup --offline --profile /recovery/installation.env \
  --output /recovery/backups/snapshot
```

The destination must not exist. The command streams the database dump and file
archive, validates their hashes and file entries, and publishes `manifest.json`
only after readback. Incomplete bundles are never accepted by `verify` or
`restore`. Shared inodes, special files and setuid/setgid entries produce an
explicit failure; reconcile them before taking a new backup. Symbolic links are
stored as links without reading their targets. A link to a separately managed
external dependency remains an external dependency after recovery.

```bash
python3 scripts/self_hosted_recovery.py verify "$HIVE_RECOVERY_BACKUPS/snapshot" \
  --manifest-sha256 REPORTED_SHA256
dc up -d backend-api backend frontend
```

For a recovery drill or a replacement installation, create a **new** project with
the verified original keys. Existing profiles are never replaced. Use a different
loopback port while the old installation still exists:

```bash
python3 scripts/configure_self_hosted.py /secure/recovered-company \
  --public-origin http://127.0.0.1:3010 --port 3010 \
  --recovery-profile "$HIVE_RECOVERY_BACKUPS/snapshot/installation.env"
HIVE_RECOVERY_PROFILE=/secure/recovered-company/installation.env
dc --profile maintenance build backend recovery
dc up -d postgres redis
# Create the canonical restricted role before importing policies and grants.
dc run --rm -T --no-deps --entrypoint python prepare -m app.scripts.grant_rls_app_role
HIVE_RECOVERY_UID=$(dc run --rm -T --no-deps --entrypoint id backend -u)
HIVE_RECOVERY_GID=$(dc run --rm -T --no-deps --entrypoint id backend -g)
dc --profile maintenance run --rm -T --no-deps \
  --volume "$HIVE_RECOVERY_PROFILE:/recovery/installation.env:ro" \
  --volume "$HIVE_RECOVERY_BACKUPS:/recovery/backups:ro" \
  recovery restore --offline --profile /recovery/installation.env \
  --backup /recovery/backups/snapshot --manifest-sha256 REPORTED_SHA256 \
  --uid "$HIVE_RECOVERY_UID" --gid "$HIVE_RECOVERY_GID"
dc run --rm -T --no-deps prepare
dc up --build -d backend-api backend frontend
```

Restore requires the same PostgreSQL major, original keys, an empty application
database and an empty Agent volume. It imports the database transactionally with
SQL errors fatal, preserves RLS policies and grants, and assigns restored files to
the actual runtime account. It never merges a bundle into an existing company or
replays tool effects itself. Redis starts fresh: transient stream notifications
and leases are not a backup of durable PostgreSQL work. Reconnect clients and
review pending or unknown operations through the canonical work/recovery paths.

A failed or interrupted restore retains its staging directory and
`.hive-recovery.json` in the new volume. Preserve them for diagnosis; do not retry
over that target or erase the original installation. Use another fresh project
for a new attempt. An interrupted database command is reported as `unknown`, not
as a confirmed rollback. Only remove failed drill volumes after verifying their
exact project ownership and retaining needed evidence.

After canonical preparation and startup, verify existing login, an older saved
artifact and its permissions, encrypted connection credentials, pending approvals
and worker recovery. `restored` means the mechanical import finished; it is not
business acceptance. Returning to an older application image cannot reverse schema
or data changes: keep the original recovery point and its approved source before
an upgrade. Container execution, clean-machine recovery and a full upgrade/rollback
journey remain separate acceptance checks while Linux testing is deferred.
