"""CCPlus V1 reconciliation §7 — "no bypass" static audit.

Reconciliation §7 row "no bypass" requires *structural* proof that no runtime
entry point bypasses the two governed choke points:

1. Tool execution goes through ``ToolRuntimeService.execute`` ->
   ``run_tool_governance`` (``app/tools/service.py`` + the wiring in
   ``app/services/agent_tools.py``). A raw executor call (``registry.try_execute``
   / a fallback executor) must NEVER appear in the live path ahead of the
   governance block, and must not appear outside ``ToolRuntimeService`` at all.

2. ``invoke_agent`` is the single kernel funnel: ``AgentKernel`` is instantiated
   in exactly one place and ``AgentKernel.handle`` is called in exactly one
   place, and no API route calls ``AgentKernel().handle`` directly.

These are *guard* tests: they parse / scan the real source files and assert the
invariants. If someone later adds a bypass (instantiates ``AgentKernel`` in a
route, calls ``.handle`` outside the invoker, or calls ``registry.try_execute``
from a new module / before governance), the relevant test FAILS.
"""

from __future__ import annotations

import ast
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
API_ROOT = APP_ROOT / "api"

INVOKER_PATH = APP_ROOT / "runtime" / "invoker.py"
INVOCATION_OWNER_PATH = APP_ROOT / "runtime" / "invocation_orchestrator.py"
SERVICE_PATH = APP_ROOT / "tools" / "service.py"
TOOL_EXECUTION_OWNER_PATH = APP_ROOT / "tools" / "execution_pipeline.py"
AGENT_TOOLS_PATH = APP_ROOT / "services" / "agent_tools.py"
GOVERNANCE_PATH = APP_ROOT / "tools" / "governance.py"

# The raw executor entry points that must stay funnelled inside
# ToolRuntimeService and behind governance.
_RAW_EXECUTOR_ATTRS = frozenset({"try_execute"})


def _iter_app_python_files() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _attr_call_names(tree: ast.AST) -> list[str]:
    """Return the attribute name of every ``<expr>.<attr>(...)`` call in tree."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.append(node.func.attr)
    return names


def test_no_bypass_kernel_is_instantiated_in_exactly_one_module() -> None:
    """AgentKernel(...) may only be constructed inside runtime/invoker.py.

    A route or service that constructs its own AgentKernel would be a kernel
    bypass (skipping invoke_agent's quota/hook/identity assembly). This guard
    enumerates every app source file and fails if AgentKernel is constructed
    anywhere except the single funnel module.
    """
    construction_sites: list[Path] = []
    for path in _iter_app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentKernel":
                construction_sites.append(path)

    # Exactly one construction site, and it is the invoker funnel. (Line-number
    # agnostic so a benign refactor inside the invoker does not flip this guard;
    # a *second* construction site anywhere — the real bypass — still fails it.)
    assert construction_sites == [INVOKER_PATH], (
        "AgentKernel must be constructed in exactly one module (the invoker funnel), found: "
        f"{[str(p.relative_to(BACKEND_ROOT)) for p in construction_sites]}"
    )


def test_no_bypass_kernel_handle_called_only_from_invocation_owner() -> None:
    """``.handle(`` on the kernel may be called from exactly one site.

    The invoker facade funnels every entry point into a single owner-side
    ``_resolve_kernel_for_request(request).handle(kernel_request)`` call. A
    second ``.handle(`` call somewhere else would mean an entry point drives the
    kernel directly. We scan every app file (excluding the invoker itself) and
    assert none of them call a ``.handle(...)`` method.
    """
    offenders: list[str] = []
    for path in _iter_app_python_files():
        if path == INVOCATION_OWNER_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "handle" in _attr_call_names(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "handle":
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")

    assert not offenders, (
        "Only the invocation owner may call kernel.handle(); "
        f"a direct .handle() call elsewhere is a kernel bypass: {offenders}"
    )

    # And inside the owner the handle call is wired to the kernel resolver,
    # not to some other object — the single funnel really runs the kernel.
    invoker_tree = ast.parse(
        INVOCATION_OWNER_PATH.read_text(encoding="utf-8"),
        filename=str(INVOCATION_OWNER_PATH),
    )
    handle_calls = [
        node
        for node in ast.walk(invoker_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "handle"
    ]
    assert len(handle_calls) == 1, "invocation owner must contain exactly one kernel.handle() call"
    funnel_call = handle_calls[0]
    assert isinstance(funnel_call.func.value, ast.Call)
    resolver = funnel_call.func.value.func
    assert (
        isinstance(resolver, ast.Attribute)
        and resolver.attr == "resolve_kernel"
        and isinstance(resolver.value, ast.Attribute)
        and resolver.value.attr == "ports"
    ), "the single handle() call must use the typed InvocationPorts kernel resolver"


def test_no_bypass_api_routes_never_reference_agent_kernel() -> None:
    """No FastAPI router may import or construct AgentKernel.

    The Agent Kernel Invariant (CLAUDE.md) is "never call the LLM directly from
    a route handler". The cheapest bypass is a route importing AgentKernel, so
    we scan every file under app/api and assert the symbol never appears.
    """
    offenders: list[str] = []
    for path in sorted(API_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if "AgentKernel" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, f"API routes must funnel through invoke_agent, not AgentKernel: {offenders}"


def test_no_bypass_raw_executor_only_called_inside_tool_runtime_service() -> None:
    """``registry.try_execute`` (the raw executor entry) may only be called inside
    ToolRuntimeService.

    Calling the execution registry directly from another module would bypass
    ``run_tool_governance``. We enumerate every app file and assert that the
    only module that calls ``.try_execute(...)`` is ``app/tools/service.py``
    (the governed runtime service). The registry's own *definition* of
    ``try_execute`` in ``app/tools/runtime.py`` is a ``def``/``async def``, not a
    call, so it is correctly ignored.
    """
    offenders: list[str] = []
    for path in _iter_app_python_files():
        if path == SERVICE_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_EXECUTOR_ATTRS
            ):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert not offenders, (
        "The raw execution registry must only be driven by ToolRuntimeService; "
        f"a direct try_execute() call elsewhere skips governance: {offenders}"
    )


def test_no_bypass_governed_execute_runs_governance_before_any_executor() -> None:
    """In ``ToolRuntimeService.execute`` the governance block precedes the executor.

    We parse ``execute`` and assert the call to ``self.governance_runner(...)``
    textually/positionally precedes the call to ``self.execute_with_context(...)``
    (which is the method that drives ``registry.try_execute`` / the fallback
    executor). A refactor that moved execution ahead of the governance gate, or
    dropped the governance call, fails this guard.
    """
    tree = ast.parse(
        TOOL_EXECUTION_OWNER_PATH.read_text(encoding="utf-8"),
        filename=str(TOOL_EXECUTION_OWNER_PATH),
    )
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    execute_fn = functions["run_tool_execution"]
    governance_fn = functions["_apply_governance"]
    executor_fn = functions["_execute_tool"]

    governance_line: int | None = None
    execute_with_context_line: int | None = None
    for node in ast.walk(governance_fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "service" and node.func.attr == "governance_runner":
                governance_line = node.lineno
    for node in ast.walk(executor_fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "service" and node.func.attr == "execute_with_context":
                execute_with_context_line = node.lineno

    assert governance_line is not None, "execute() must call self.governance_runner(...)"
    assert execute_with_context_line is not None, "execute() must call self.execute_with_context(...)"
    owner_source = ast.unparse(execute_fn)
    assert owner_source.index("_apply_governance") < owner_source.index("_execute_tool"), (
        "The public owner must order the governance stage before the executor stage"
    )


def test_no_bypass_tool_runtime_service_is_wired_to_run_tool_governance() -> None:
    """The shared ToolRuntimeService is constructed with the real governance runner.

    A guard test only proves "governance runs first" if the runner is actually
    ``run_tool_governance``. We import the real factory, build the singleton, and
    assert ``governance_runner is run_tool_governance``. We also assert
    ``execute_tool`` (the kernel's tool dispatch path) forwards to
    ``ToolRuntimeService.execute`` so the governed choke is the live path.
    """
    from app.services import agent_tools
    from app.tools.governance import run_tool_governance

    service = agent_tools._get_tool_runtime_service()
    assert service.governance_runner is run_tool_governance, (
        "ToolRuntimeService must be wired with run_tool_governance as its governance_runner"
    )

    # execute_tool must route into ToolRuntimeService.execute (the governed
    # entry), not into a registry/fallback directly.
    execute_tool_src = ast.parse(AGENT_TOOLS_PATH.read_text(encoding="utf-8"), filename=str(AGENT_TOOLS_PATH))
    execute_tool_fn = next(
        node
        for node in ast.walk(execute_tool_src)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_tool"
    )
    called_attrs = _attr_call_names(execute_tool_fn)
    assert "execute" in called_attrs, (
        "execute_tool must dispatch through ToolRuntimeService.execute (the governed choke)"
    )
    assert not (_RAW_EXECUTOR_ATTRS & set(called_attrs)), (
        "execute_tool must not call the raw execution registry directly"
    )


def test_no_bypass_run_tool_governance_is_the_named_choke_symbol() -> None:
    """``run_tool_governance`` exists as the governance entry symbol.

    Anchors the audit to the real symbol named by reconciliation §7 — if the
    choke point were renamed/removed the import fails and every other guard that
    asserts wiring to it becomes meaningful.
    """
    from app.tools.governance import run_tool_governance

    assert callable(run_tool_governance)
    governance_src = GOVERNANCE_PATH.read_text(encoding="utf-8")
    assert "async def run_tool_governance(" in governance_src
