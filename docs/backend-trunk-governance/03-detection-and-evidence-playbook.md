# 03 Detection And Evidence Playbook

## Static Evidence

Use `rg` to find trunk drift:

```bash
rg -n "from app.services.agent_tools import|execute_direct\\(|_request_approval_compat|memory_context|build_runtime_prompt" backend/app backend/tests
rg -n "ChatSession\\(|source_channel=|external_conv_id=|SessionContext\\(" backend/app
rg -n "focus.md|agent_objectives|RuntimeTask|task_type=\"trigger\"|task_type=\"heartbeat\"" backend/app backend/tests
```

## Runtime Evidence

Use these tests as proof that the current trunk still holds:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/architecture tests/tools/test_governance.py tests/tools/test_service.py tests/services/test_trigger_daemon.py tests/services/test_runtime_task_service.py
```

## Railway Evidence

For production, verify from platform admin APIs:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://<railway-domain>/api/admin/autonomous-audit?lookback_hours=24"
```

The report must show focus/trigger/runtime/session facts and must not mutate state.

## Merge Evidence

Before accepting any feature migration:

- The feature must not introduce a second autonomy trigger model.
- The feature must not bypass `ToolRuntimeService`.
- The feature must not write objectives into memory files.
- The feature must pass the master regression plan.
