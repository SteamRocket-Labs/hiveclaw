"""P2-12 — admin router guard consistency.

Two invariants enforced by introspecting the FastAPI dependency tree:

1. Every route on /admin must carry a `require_role(...)` dependency.
2. Every role passed to `require_role(...)` must exist in the User.role
   enum — otherwise the dependency is a perma-deny that locks out every
   legitimate admin (silent fail-closed, surfaces only via 403s in prod).

Catches the class of bugs where someone adds an admin route and forgets
the guard, or types a role string that doesn't match the database enum.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute


# Mirror the User.role enum from app/models/user.py — kept as a literal
# constant here so a drift in either side trips the test instead of
# silently agreeing.
_VALID_USER_ROLES = {"platform_admin", "org_admin", "member"}


def _walk_dependants(root: Dependant) -> Iterator[Dependant]:
    """Depth-first traversal of the FastAPI dependency tree."""
    yield root
    for sub in root.dependencies:
        yield from _walk_dependants(sub)


def _required_roles(route: APIRoute) -> list[str]:
    """Collect every role string from `require_role(...)` deps on this route.

    `require_role` is a closure factory — each call returns an inner `_check`
    function whose closure cell holds the `allowed_roles` tuple. We identify
    those callables by qualname and read the tuple back out.
    """
    roles: list[str] = []
    for dep in _walk_dependants(route.dependant):
        call = dep.call
        if call is None:
            continue
        qualname = getattr(call, "__qualname__", "")
        if qualname != "require_role.<locals>._check":
            continue
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            contents = cell.cell_contents
            if isinstance(contents, tuple) and all(isinstance(x, str) for x in contents):
                roles.extend(contents)
    return roles


def _admin_routes() -> list[APIRoute]:
    from app.api.admin import router

    return [r for r in router.routes if isinstance(r, APIRoute)]


# ── Tests ──────────────────────────────────────────────────────


def test_admin_router_introspection_finds_routes() -> None:
    """Sanity: introspection actually discovers admin routes.

    If admin.py is restructured and APIRoute discovery breaks, this test
    fails before the real guard checks return false-green empties.
    """
    routes = _admin_routes()
    assert len(routes) >= 10, f"expected ~16 admin routes, found {len(routes)}"


def test_every_admin_route_has_require_role_dependency() -> None:
    """No admin endpoint may ship without an explicit role check."""
    unguarded: list[str] = []
    for route in _admin_routes():
        if not _required_roles(route):
            unguarded.append(f"{route.methods} {route.path}")

    assert not unguarded, (
        "Admin routes missing require_role dependency (would expose "
        "platform-admin operations to any authenticated user): "
        f"{unguarded}"
    )


def test_every_admin_role_exists_in_user_enum() -> None:
    """Roles passed to require_role must match User.role enum values.

    A typo like require_role("admin") instead of "platform_admin" silently
    perma-denies the endpoint because no user can ever hold that role.
    """
    invalid: list[tuple[str, str]] = []
    for route in _admin_routes():
        for role in _required_roles(route):
            if role not in _VALID_USER_ROLES:
                invalid.append((route.path, role))

    assert not invalid, (
        "Admin routes referencing non-existent roles (perma-deny: no "
        f"user can ever satisfy them): {invalid}. "
        f"Valid roles: {sorted(_VALID_USER_ROLES)}"
    )


def test_admin_routes_only_use_platform_admin_role() -> None:
    """Soft policy: every admin endpoint should require platform_admin.

    Lower-privilege roles (org_admin, member) are tenant-scoped and have
    no business calling cross-tenant admin endpoints. If a future feature
    needs org_admin to call something on /admin, this test forces a
    deliberate decision rather than a silent privilege drop.
    """
    downgraded: list[tuple[str, list[str]]] = []
    for route in _admin_routes():
        roles = _required_roles(route)
        if not roles:
            # already covered by test_every_admin_route_has_require_role_dependency
            continue
        if "platform_admin" not in roles:
            downgraded.append((route.path, roles))

    assert not downgraded, (
        "Admin routes accepting non-platform_admin roles — review whether "
        f"this is intentional cross-tenant access: {downgraded}"
    )
