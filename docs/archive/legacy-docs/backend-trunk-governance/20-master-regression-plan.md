# 20 Master Regression Plan

Run this after any H1-H6 trunk change:

```bash
cd ${REPO_ROOT}/backend
source .venv/bin/activate

pytest
ruff check app tests
alembic heads

cd ${REPO_ROOT}/frontend
npm test
npm run build

cd ${REPO_ROOT}
git diff --check
```

Expected current backend test count after the production Harness Validation Run endpoint: `1931 passed, 7 skipped, 4 warnings`. Update this file only from real pytest output, never from memory.

Target invariant:

```text
All tests green
Ruff clean
Alembic single head
Frontend test/build green
No whitespace errors
```
