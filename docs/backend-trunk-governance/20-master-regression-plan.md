# 20 Master Regression Plan

Run this after any H1-H6 trunk change:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest
ruff check app tests
alembic heads

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test
npm run build

cd /Users/rocky243/vc-saas/hiveclaw-main
git diff --check
```

Expected current backend test count after Phase 0R/H1-H6 first executable harness trunk: `1887 passed, 7 skipped, 4 warnings`. Update this file only from real pytest output, never from memory.

Target invariant:

```text
All tests green
Ruff clean
Alembic single head
Frontend test/build green
No whitespace errors
```
