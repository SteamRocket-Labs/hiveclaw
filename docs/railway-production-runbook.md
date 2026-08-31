# Railway Production Deployment Runbook

Use this runbook only after the owner explicitly authorizes a HiveClaw production
deployment. It records the current three-service upload shape outside the always-on
agent instructions so deployment detail can evolve independently.

## Scope and evidence boundary

Production consists of three Railway services:

- `backend`
- `backend-api`
- `frontend`

Deploy all three from the same exact committed source. A successful upload or public
health response proves bounded deployment freshness and transport health; it does not
by itself prove signed-in business journeys, tenant correctness, or full production
acceptance.

Before submitting:

1. confirm explicit production-deploy authority and the exact target project;
2. record `git rev-parse HEAD` and ensure the intended changes are committed;
3. review `git status` so uncommitted or unrelated changes are not mistaken for the
   deployed artifact;
4. confirm Railway authentication and selected production environment without printing
   credentials; and
5. identify the required post-deploy health and user-path checks.

## Service source layout

- `backend`: Railway uses `rootDirectory=backend`; upload an archive that retains a
  top-level `backend/` directory.
- `backend-api`: Railway uses no root directory and `configFile=/railway.json`; upload
  from the backend package root.
- `frontend`: Railway uses `rootDirectory=frontend`; upload an archive that retains a
  top-level `frontend/` directory.

## Submit the exact committed source

Run from the repository root. The project identifier is not a credential, but verify it
against current Railway configuration before use.

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
tmp_root=$(mktemp -d /tmp/hiveclaw-railway-upload.XXXXXX)
mkdir -p "$tmp_root/backend-root" "$tmp_root/frontend-root"

git archive --format=tar HEAD backend | tar -xf - -C "$tmp_root/backend-root"
git archive --format=tar HEAD frontend | tar -xf - -C "$tmp_root/frontend-root"

cd "$tmp_root/backend-root"
railway up --service backend --environment production --project "$PROJECT_ID" \
  --detach -m "deploy exact committed backend archive-root"

cd "$tmp_root/backend-root/backend"
railway up --service backend-api --environment production --project "$PROJECT_ID" \
  --detach -m "deploy exact committed backend-api root"

cd "$tmp_root/frontend-root"
railway up --service frontend --environment production --project "$PROJECT_ID" \
  --detach -m "deploy exact committed frontend archive-root"
```

Keep the validated temporary directory only until upload provenance is established;
then remove that exact directory without touching broader paths.

## Verify all three services

Poll each target until its newest deployment reaches a terminal state:

```bash
railway deployment list --service backend --environment production \
  --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service backend-api --environment production \
  --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service frontend --environment production \
  --project "$PROJECT_ID" --limit 1 --json
```

All three must report `SUCCESS` for the intended deployment. `backend-api` is not
publicly exposed, so a public backend response cannot prove its freshness.

Then check the public transport surfaces:

```bash
curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/
```

Record exact deployment IDs, source commit, terminal status, and health results. Run
the separately authorized signed-in or tenant-specific acceptance journey when the
change requires it. Report deployment, health, and business acceptance as separate
claims.

If any service fails or remains stale, stop the acceptance claim. Preserve the
deployment evidence, diagnose the concrete service, and obtain authority before a
rollback, resubmission with changed source, or production data operation.
