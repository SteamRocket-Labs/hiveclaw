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

Original uploads default to private filesystem object storage. To select an
S3-compatible service for new objects, prepare a mode-600 JSON file containing
`endpoint`, `region`, `bucket`, `access_key_id`, `secret_access_key` and, for
temporary credentials, `session_token`. Pass its path to
`configure_self_hosted.py --object-storage-profile`. Use an HTTPS service origin
and a pre-existing private bucket with scoped `GetObject`/`PutObject` access;
the adapter uses path-style requests and conditional `If-None-Match: *` writes.
It does not create buckets or expose credentials or signed URLs to employees.
Keep this file and `installation.env` private. Literal loopback HTTP endpoints
are supported for local validation; remote HTTP, redirects and environment
proxies are rejected. Other S3 APIs and real service compatibility need their
own validation.

Changing the writer does not relocate existing originals. Saved references keep
their provider, bucket, tenant namespace and recorded endpoint/region. A missing
reader configuration, altered location or changed bytes is an unavailable
original, with no substitution from its editable workspace copy.

Offline backups include verified bytes for every available remote object along
with the database and volume. Keep the source object service readable until the
backup manifest is complete and verified; missing or corrupt objects fail the
backup. Restoration materializes those bytes in the target's private filesystem
and maps the matching restored object records in one transaction, retaining
original reference IDs, ownership, hashes and remote locator evidence. The
restore receipt reports the mapping. A recovered installation defaults to the
filesystem writer and can read these originals without the old object endpoint
or credentials. Select an S3 profile explicitly for subsequent new remote
objects. Existing backup profiles without remote objects remain supported.
Bundles with remote objects use backup schema v2, which older recovery tools
reject rather than restoring references without their required provider mapping.

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

Backups also bind the source volume's workspace locators. If maintenance and
runtime mount the same volume at different paths, pass `--runtime-agents-root`
with the runtime's absolute mount path when backing up and restoring. The
standard Compose profile mounts both at `/data/agents` and needs no override.
Restoration records authenticated relocation metadata and a matching database
witness. Pending relative file actions then use the restored employee or isolated
subtask workspace without changing the original approval's arguments, identity,
policy or envelope hash. A later backup preserves this locator lineage.

Keep `.hive-recovery.json` in the restored volume and retain the original
installation encryption key. Missing, changed or mismatched recovery metadata
holds affected approvals before consumption; it must not fall back to an old
installation path. Repair from the verified restore receipt before retrying.
Older bundles without source locators support unchanged mounts; a moved legacy
workspace without sufficient evidence requires a new review. Absolute paths to
external dependencies are not rewritten. Recovery does not approve or execute
pending actions: an authenticated reviewer and the normal fenced worker remain
required, and completed execution receipts prevent duplicate effects.

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

## Upgrade preflight and source verification

Keep the original approved source, image IDs and recovery point before building
candidate images. Do not prune original images during the procedure. Building a
candidate does not update the running containers. Record each service separately;
a public backend health response cannot establish the API or frontend source.

The frontend build emits `build-identity.json` from its source files, paths and
modes. Backend health already exposes the corresponding server-derived source
identity. These are source identities, not signed image attestations or business
acceptance. A time-stamped version label alone is insufficient.

Use a private operator directory outside the Agent volume. With `dc` and the
profile from the preceding sections, build the candidate from the reviewed source
and package its frontend and backend identities without starting application
entrypoints:

```bash
HIVE_UPGRADE_RECORDS=/secure/upgrade-records
mkdir -p "$HIVE_UPGRADE_RECORDS"
chmod 700 "$HIVE_UPGRADE_RECORDS"
dc images --format json > "$HIVE_UPGRADE_RECORDS/original-images.json"
dc build backend frontend
dc run --rm -T --no-deps --entrypoint cat frontend \
  /usr/share/nginx/html/build-identity.json > "$HIVE_UPGRADE_RECORDS/frontend-identity.json"
dc run --rm -T --no-deps --volume "$HIVE_UPGRADE_RECORDS:/upgrade" \
  --entrypoint python prepare -m app.scripts.self_hosted_upgrade package \
  --frontend-identity /upgrade/frontend-identity.json --output /upgrade/release.json
```

Review and retain the reported release SHA-256. Stop all writers and take a new
verified offline backup using the preceding backup procedure. The backup must
contain the original keys and exactly the current Agent files. A catalog head is
not a fingerprint of all database rows: the operator must also prevent database
and filesystem writes between backup, preflight and migration.

Mount the same verified backup and operator directory for preflight and apply:

```bash
dc run --rm -T --no-deps \
  --volume "$HIVE_RECOVERY_BACKUPS:/backups:ro" \
  --volume "$HIVE_UPGRADE_RECORDS:/upgrade" --entrypoint python prepare \
  -m app.scripts.self_hosted_upgrade preflight --offline \
  --release /upgrade/release.json --release-sha256 REVIEWED_RELEASE_SHA256 \
  --backup /backups/snapshot --backup-sha256 VERIFIED_BACKUP_SHA256 \
  --output /upgrade/plan.json
```

Preflight checks the current candidate source, exact backup and keys, full file
inventory, database target, known Alembic revision path and supported Session
writer generations. An unversioned database, unknown revision, unsupported writer,
live client or changed files requires reconciliation before proceeding. It does
not replace a recovery rehearsal or judge task quality. The output lists the
actual migration revisions; review its plan SHA-256 before applying.

```bash
dc run --rm -T --no-deps \
  --volume "$HIVE_RECOVERY_BACKUPS:/backups:ro" \
  --volume "$HIVE_UPGRADE_RECORDS:/upgrade" --entrypoint python prepare \
  -m app.scripts.self_hosted_upgrade apply --offline \
  --release /upgrade/release.json --release-sha256 REVIEWED_RELEASE_SHA256 \
  --backup /backups/snapshot --backup-sha256 VERIFIED_BACKUP_SHA256 \
  --plan /upgrade/plan.json --plan-sha256 REVIEWED_PLAN_SHA256 \
  --receipt /upgrade/attempt.json
```

Apply repeats preflight before effects, creates the attempt receipt without
overwriting an earlier attempt, then invokes the same canonical preparation used
for first-run installation. Preparation verifies the migrated catalog and RLS
before returning `prepared`. An interrupted attempt stays `unknown` or retains
its `started` receipt; keep traffic stopped and inspect it rather than blindly
replaying the command. The original recovery point remains available for a new,
empty recovery project. No automatic data downgrade or old-image compatibility
claim is made.

After preparation succeeds, start all three candidate services and verify them
from the Compose network:

```bash
dc up -d backend-api backend frontend
dc run --rm -T --no-deps --volume "$HIVE_UPGRADE_RECORDS:/upgrade" \
  --entrypoint python prepare -m app.scripts.self_hosted_upgrade observe \
  --release /upgrade/release.json --release-sha256 REVIEWED_RELEASE_SHA256 \
  --backend-url http://backend:8000 --api-url http://backend-api:8000 \
  --frontend-url http://frontend --output /upgrade/observed.json
```

The observer requires the exact packaged source on every service and the correct
runtime/API roles. It records health separately, so `source_verified` does not
turn a degraded component into a healthy one. Finally check the signed-in work
paths, older files, approvals and worker recovery before reopening ingress.
Native migration and source observation do not constitute Linux/container,
clean-machine, rollback or full enterprise acceptance.

## Storage retention

Company administrators can open **Workspace → Company Management → Storage and retention** to pause periodic cleanup or change the retention period. Saving records a versioned company policy; the next employee maintenance pass reads it. Saving does not start an immediate purge. Reload the saved policy after a conflict or lost response before saving again. Without a company policy, `CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS` supplies the installation default (30 days).

Cleanup covers expired, unreferenced delivery snapshots and orphan Office previews. Published artifact versions, active work, work awaiting reconciliation and pending approval evidence stay protected. Publishing and cleanup share a per-employee transaction lock, so an uncommitted delivery cannot be collected as an orphan. This policy does not remove workspace source files, company knowledge, memory, runtime databases or backups. A corrupt policy pauses this cleanup until repaired; disabling cleanup also preserves orphan previews.

New delivery snapshots reuse one immutable payload per employee, full content hash and file suffix. Each delivery keeps its own identity, source authority and small retention reference. Editable workspace files remain separate copies. Reusing an old payload does not renew its timestamp: cleanup also checks recent delivery references and protected work before removing bytes. An unreadable or aliased reference preserves uncertain shared payloads and reports `manifest_errors` for operator reconciliation; unrelated expired legacy snapshots can still be collected. This does not pool files across employee stores or move snapshots to object storage.

The `chat_snapshot_payload_refs_1021` catalog revision is a reader compatibility boundary. Older applications must not start against it. Downgrade refuses while shared-format artifact rows or server artifact references remain; reverting the image alone is insufficient. Keep a verified compatible backup for data rollback, and recover it into an empty target before using an older release. Current backups preserve the shared payloads and their references together.

Platform administrators can see total, used and free capacity of the runtime's shared employee volume on this page. These are physical volume figures, not company usage or quotas. Organization administrators manage their own policy without receiving shared capacity figures. An unavailable volume returns an unavailable state; the no-volume API does not substitute its own filesystem. Keep verified backups under a separate operator retention policy.
