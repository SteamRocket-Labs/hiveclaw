from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from app.core.rls_bypass_manifest import (
    RLS_BYPASS_ALLOWLIST,
    RLS_BYPASS_SCOPES_SHA256,
    RLSBypassStaticAnalysisError,
    fingerprint_rls_bypass_scopes,
    scan_rls_bypass_callsites,
)


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_every_rls_bypass_callsite_is_registered_with_exact_query_shape() -> None:
    actual = Counter(call.signature for call in scan_rls_bypass_callsites(APP_ROOT))
    registered = Counter(grant.signature for grant in RLS_BYPASS_ALLOWLIST)

    assert actual == registered


def test_every_rls_bypass_scope_matches_the_reviewed_ast_fingerprint() -> None:
    assert fingerprint_rls_bypass_scopes(APP_ROOT) == RLS_BYPASS_SCOPES_SHA256


def test_bypass_scope_fingerprint_covers_predicates_locks_and_orm_writes(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    source = app_root / "assignment.py"
    variants = (
        "await bypass_db.execute(select(User).where(User.id == user.id).with_for_update())\n"
        "        user.role = 'org_admin'\n"
        "        bypass_db.add(AuditLog())",
        "await bypass_db.execute(select(User).where(func.lower(User.email) == email).with_for_update())\n"
        "        user.role = 'org_admin'\n"
        "        bypass_db.add(AuditLog())",
        "await bypass_db.execute(select(User).where(User.id == user.id))\n"
        "        user.role = 'org_admin'\n"
        "        bypass_db.add(AuditLog())",
        "await bypass_db.execute(select(User).where(User.id == user.id).with_for_update())\n"
        "        user.tenant_id = tenant_id\n"
        "        bypass_db.add(AuditLog())",
        "await bypass_db.execute(select(User).where(User.id == user.id).with_for_update())\n"
        "        user.role = 'org_admin'\n"
        "        bypass_db.add(SecurityAuditLog())",
    )
    fingerprints: set[str] = set()
    for body in variants:
        source.write_text(
            "async def assign(db, user, email, tenant_id):\n"
            "    async with enter_rls_bypass(db, reason='assign') as bypass_db:\n"
            f"        {body}\n",
            encoding="utf-8",
        )
        fingerprints.add(fingerprint_rls_bypass_scopes(app_root))

    assert len(fingerprints) == len(variants)


def test_bypass_scope_fingerprint_covers_contextmanager_consumers(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    source = app_root / "wrapped.py"
    fingerprints: set[str] = set()
    for predicate in ("Tenant.id == tenant_id", "Tenant.slug == slug"):
        source.write_text(
            "import contextlib\n"
            "@contextlib.asynccontextmanager\n"
            "async def privileged(db):\n"
            "    async with enter_rls_bypass(db, reason='platform admin') as bypass_db:\n"
            "        yield bypass_db\n"
            "async def load(db, tenant_id, slug):\n"
            "    async with privileged(db) as scoped_db:\n"
            f"        await scoped_db.execute(select(Tenant).where({predicate}))\n",
            encoding="utf-8",
        )
        fingerprints.add(fingerprint_rls_bypass_scopes(app_root))

    assert len(fingerprints) == 2


def test_bypass_scanner_tracks_wrapper_output_not_its_business_arguments(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "wrapped.py").write_text(
        "import contextlib\n"
        "@contextlib.asynccontextmanager\n"
        "async def worker_session(operation):\n"
        "    async with session_factory() as db:\n"
        "        async with enter_rls_bypass(db, reason=operation) as bypass_db:\n"
        "            yield bypass_db\n"
        "async def claim():\n"
        "    async with worker_session('claim') as db:\n"
        "        await db.execute(select(User))\n",
        encoding="utf-8",
    )

    calls = scan_rls_bypass_callsites(app_root)

    assert [(call.function, call.reason_expression) for call in calls] == [("worker_session", "operation")]


def test_bypass_scope_fingerprint_covers_comments_and_layout(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    source = app_root / "stable.py"
    variants = (
        "async def load(db):\n"
        "    async with enter_rls_bypass(db, reason='read') as bypass_db:\n"
        "        await bypass_db.execute(select(User).where(User.id == user_id))\n",
        "async def load(db):\n"
        "  async with enter_rls_bypass(\n"
        "      db, reason = 'read'  # reviewed\n"
        "  ) as bypass_db:\n"
        "    await bypass_db.execute( select(User).where(User.id == user_id) )\n",
    )
    fingerprints: set[str] = set()
    for body in variants:
        source.write_text(body, encoding="utf-8")
        fingerprints.add(fingerprint_rls_bypass_scopes(app_root))

    assert len(fingerprints) == 2


def test_every_rls_bypass_grant_has_owner_expiry_and_query_fields() -> None:
    for grant in RLS_BYPASS_ALLOWLIST:
        assert grant.owner.strip()
        assert grant.expires_on >= date.today()
        assert grant.allowed_query_fields
        assert grant.reason_expression.strip()


def test_bypass_scanner_rejects_missing_reason(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "bad.py").write_text(
        "async def bad(db):\n    async with enter_rls_bypass(db):\n        return None\n",
        encoding="utf-8",
    )

    calls = scan_rls_bypass_callsites(app_root)

    assert len(calls) == 1
    assert calls[0].reason_expression == ""


def test_bypass_scanner_rejects_session_aliases(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "bad.py").write_text(
        "async def bad(db):\n"
        "    async with enter_rls_bypass(db, reason='read') as bypass_db:\n"
        "        db = bypass_db\n"
        "        await db.execute(select(User))\n",
        encoding="utf-8",
    )

    with pytest.raises(RLSBypassStaticAnalysisError, match="capability aliases are unsupported"):
        scan_rls_bypass_callsites(app_root)


def test_bypass_scanner_allows_nested_audited_scope_on_tainted_session(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "nested.py").write_text(
        "async def inner(db):\n"
        "    async with enter_rls_bypass(db, reason='inner') as bypass_db:\n"
        "        await bypass_db.execute(select(User))\n"
        "async def outer(db):\n"
        "    async with enter_rls_bypass(db, reason='outer') as bypass_db:\n"
        "        await inner(bypass_db)\n",
        encoding="utf-8",
    )

    calls = scan_rls_bypass_callsites(app_root)

    assert {(call.function, call.reason_expression) for call in calls} == {
        ("inner", "'inner'"),
        ("outer", "'outer'"),
    }


def test_bypass_scanner_follows_local_instance_methods(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "local_instance.py").write_text(
        "class Reader:\n"
        "    async def read(self, db):\n"
        "        await db.execute(select(User))\n"
        "async def load(db):\n"
        "    async with enter_rls_bypass(db, reason='read') as bypass_db:\n"
        "        reader = Reader()\n"
        "        await reader.read(bypass_db)\n",
        encoding="utf-8",
    )

    calls = scan_rls_bypass_callsites(app_root)

    assert [(call.function, call.reason_expression) for call in calls] == [("load", "'read'")]


def test_bypass_fingerprint_covers_session_backed_service_methods(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    source = app_root / "service.py"
    fingerprints: set[str] = set()
    for predicate in ("User.id == user_id", "User.email == email"):
        source.write_text(
            "class Reader:\n"
            "    def __init__(self, db):\n"
            "        self.db = db\n"
            "    async def read(self):\n"
            f"        await self.db.execute(select(User).where({predicate}))\n"
            "async def load(db):\n"
            "    async with enter_rls_bypass(db, reason='read'):\n"
            "        reader = Reader(db)\n"
            "        await reader.read()\n",
            encoding="utf-8",
        )
        fingerprints.add(fingerprint_rls_bypass_scopes(app_root))

    assert len(fingerprints) == 2


def test_bypass_scanner_rejects_session_backed_object_escape(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "bad_service.py").write_text(
        "class Reader:\n"
        "    def __init__(self, db):\n"
        "        self.db = db\n"
        "async def load(db):\n"
        "    async with enter_rls_bypass(db, reason='read'):\n"
        "        reader = Reader(db)\n"
        "        return reader\n",
        encoding="utf-8",
    )

    with pytest.raises(RLSBypassStaticAnalysisError, match="cannot be returned or yielded"):
        scan_rls_bypass_callsites(app_root)


def test_bypass_scanner_covers_generated_session_backed_context_constructor(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "context.py").write_text(
        "class Context:\n"
        "    db: object\n"
        "async def load(db):\n"
        "    async with enter_rls_bypass(db, reason='read'):\n"
        "        context = Context(db=db)\n"
        "        await consume(context)\n",
        encoding="utf-8",
    )

    with pytest.raises(RLSBypassStaticAnalysisError, match="unresolved callback"):
        scan_rls_bypass_callsites(app_root)


def test_agent_runtime_bootstrap_has_no_business_row_bypass() -> None:
    callsites = scan_rls_bypass_callsites(APP_ROOT)
    forbidden_business_files = {
        "app/runtime/invoker.py",
        "app/services/agent_team_runtime_service.py",
        "app/services/command_escalation_service.py",
        "app/services/mcp_server_service.py",
        "app/services/quota_guard.py",
        "app/services/subagent_run_service.py",
        "app/services/runtime_task_fence.py",
        "app/services/web_chat_runtime.py",
        "app/tools/governance_resolver.py",
        "app/tools/handlers/subagent.py",
        "app/tools/resolver.py",
        "app/tools/workspace.py",
    }

    assert not [call for call in callsites if call.file in forbidden_business_files]


def test_workflow_runtime_uses_locator_then_tenant_scope() -> None:
    callsites = scan_rls_bypass_callsites(APP_ROOT)

    assert not [
        call
        for call in callsites
        if (
            call.file
            in {
                "app/services/workflow_launch.py",
                "app/services/workflow_signal_consumer.py",
            }
            or (
                call.file == "app/services/workflow_runtime_service.py"
                and call.function in {"_tenant_for_run", "resume_pending_runs"}
            )
        )
    ]


def test_runtime_control_business_reads_use_tenant_locators() -> None:
    callsites = scan_rls_bypass_callsites(APP_ROOT)

    assert not [
        call
        for call in callsites
        if call.file == "app/services/runtime_control_bus.py"
        and call.function in {"_load_session_lifecycle_messages", "bridge_transcript_event_to_t0"}
    ]
