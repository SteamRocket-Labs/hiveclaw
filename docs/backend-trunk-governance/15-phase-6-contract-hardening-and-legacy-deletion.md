# 15 Contract Hardening And Legacy Deletion

## Current Principle

Delete or fence old compatibility paths only after tests prove the current trunk.

## Deletion Criteria

A compatibility path can be removed when:

- A current architecture test names the desired trunk.
- Service/API tests cover behavior.
- Full backend pytest and ruff pass.
- Frontend tests/build pass if UI contracts are affected.
- Railway verification has an equivalent read-only check when production state is involved.

## Never Delete Blindly

Do not delete:

- Migration helpers still needed by old Railway databases.
- Legacy read-only fallback paths without production evidence.
- User runtime files.
- Uncommitted user changes.

## Preferred Pattern

```text
test red -> minimal trunk implementation -> local regression -> docs -> production audit
```
