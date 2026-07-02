"""Evaluator trust root — graders that decide pass/fail must
come from a PROTECTED source the candidate-under-test cannot modify.

round2 §2.3 iron law 2 + the owner's finding (§4.2): a git-tracked expected hash
placed in the SAME PR as the grader is self-deception — a reward-hacker (or a
self-evolving agent via a PR) changes the grader and the expected hash together
and CI stays green. The trust root must live OUTSIDE the candidate's writable
surface: the protected base branch / a signed artifact / CODEOWNERS-required
review. CI computes the evaluator hashes from the PR (current) and from the
protected base (trusted) and compares; any divergence is untrusted unless
explicitly authorized by protected review. Separately, evaluator code must never
live on an agent-writable workspace path (the DGM Node 114 guard).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass, field
from pathlib import Path

# Files that constitute the evaluator trust root (graders + local eval runners).
# A change to any of these requires CODEOWNERS review from the protected base —
# it can never be self-approved inside the candidate PR.
EVALUATOR_RELATIVE_PATHS: tuple[str, ...] = (
    "app/evals/adversarial_suite.py",
    "app/evals/artifact_gate.py",
    "app/evals/bakeoff_runtime.py",
    "app/evals/run.py",
    "app/evals/evaluator_integrity.py",
    "app/services/evolution_verification.py",
)

# Markers of an agent-writable path. Evaluator code must never live here, or the
# agent could rewrite its own grader (DGM Node 114 / STOP use_sandbox=False).
_AGENT_WORKSPACE_MARKERS: tuple[str, ...] = (
    "/workspace/",
    "/memory/",
    "/skills/",
    "/.agents/",
    "agent-home",
    "/data/agents/",
)


class EvaluatorIntegrityError(RuntimeError):
    """Evaluator/baseline was placed on an agent-writable path or modified off-trust-root."""


@dataclass(frozen=True)
class IntegrityReport:
    trusted: bool
    changed_paths: list[str] = field(default_factory=list)
    reason: str = ""


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_evaluator_hashes(
    root: Path,
    *,
    relative_paths: tuple[str, ...] = EVALUATOR_RELATIVE_PATHS,
) -> dict[str, str]:
    """Hash every evaluator file under ``root`` (a PR checkout or the base checkout)."""

    hashes: dict[str, str] = {}
    for rel in relative_paths:
        path = root / rel
        hashes[rel] = _hash_text(path.read_text(encoding="utf-8")) if path.is_file() else "<missing>"
    return hashes


def verify_evaluator_integrity(
    *,
    current_hashes: dict[str, str],
    trusted_hashes: dict[str, str],
    authorized_changes: bool = False,
) -> IntegrityReport:
    """Compare PR (current) evaluator hashes against the PROTECTED base (trusted).

    A path whose current hash differs from trusted is a modification to the
    evaluator. Unless ``authorized_changes`` is True — set by CI only when
    CODEOWNERS approval is surfaced from the protected context — any change makes
    the evaluator untrusted. Crucially we never compare ``current`` against an
    expected hash carried inside ``current`` itself, so embedding a self-consistent
    hash in the same PR does NOT authorize a grader change.
    """

    changed = sorted(
        path
        for path in set(current_hashes) | set(trusted_hashes)
        if current_hashes.get(path) != trusted_hashes.get(path)
    )
    if changed and not authorized_changes:
        return IntegrityReport(
            trusted=False,
            changed_paths=changed,
            reason=f"evaluator modified without protected-source authorization: {changed}",
        )
    return IntegrityReport(
        trusted=True,
        changed_paths=changed,
        reason="evaluator matches protected trust root"
        if not changed
        else "evaluator changes authorized by protected review",
    )


def assert_evaluator_outside_agent_workspace(path: Path | str) -> None:
    """Raise if an evaluator path is on an agent-writable workspace (DGM Node 114 guard)."""

    text = str(path).replace("\\", "/")
    for marker in _AGENT_WORKSPACE_MARKERS:
        if marker in text:
            raise EvaluatorIntegrityError(
                f"evaluator path is inside an agent-writable workspace: {path} (marker {marker!r})"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate evaluator integrity report from a protected trust root.")
    parser.add_argument("--current-root", type=Path, required=True, help="backend root for the current checkout")
    parser.add_argument(
        "--trusted-root", type=Path, required=True, help="backend root from the protected base checkout"
    )
    parser.add_argument("--output", type=Path, required=True, help="path to write the integrity JSON report")
    parser.add_argument(
        "--authorized-changes",
        action="store_true",
        help="set only from protected CI context after evaluator CODEOWNERS approval",
    )
    args = parser.parse_args(argv)

    try:
        assert_evaluator_outside_agent_workspace(args.current_root)
        assert_evaluator_outside_agent_workspace(args.trusted_root)
        report = verify_evaluator_integrity(
            current_hashes=compute_evaluator_hashes(args.current_root),
            trusted_hashes=compute_evaluator_hashes(args.trusted_root),
            authorized_changes=args.authorized_changes,
        )
    except Exception as exc:  # fail closed; CI decides how to surface the report
        report = IntegrityReport(trusted=False, reason=f"evaluator integrity check failed: {exc}")

    payload = asdict(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
