"""Static guardrails for imported or generated SKILL.md packages."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SkillGuardFinding:
    severity: str
    category: str
    path: str
    message: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "path": self.path,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class SkillGuardReport:
    allowed: bool
    risk_level: str
    findings: tuple[SkillGuardFinding, ...]
    source: str

    @property
    def blocking_findings(self) -> tuple[SkillGuardFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "block")

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk_level": self.risk_level,
            "source": self.source,
            "findings": [finding.to_dict() for finding in self.findings],
        }


_REMOTE_SHELL_PIPE_RE = re.compile(r"\b(?:curl|wget)\b[^\n|;&]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|python3?|ruby|perl)\b", re.I)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|SLACK_BOT_TOKEN|"
    r"STRIPE_SECRET_KEY|SECRET_KEY|JWT_SECRET_KEY|PASSWORD|ACCESS_TOKEN)\s*=\s*['\"]?[A-Za-z0-9_./+=:-]{8,}",
    re.I,
)
_DESTRUCTIVE_ROOT_RE = re.compile(r"\b(?:sudo\s+)?rm\s+-[^\n;&]*[rf][^\n;&]*\s+/(?:\s|$)", re.I)
_PERSISTENCE_RE = re.compile(r"\b(?:crontab|launchctl|systemctl|schtasks|rc\.local)\b", re.I)
_EXFIL_RE = re.compile(r"(?:~/.ssh|/etc/passwd|/etc/shadow|\bscp\b|\brsync\b|\bnc\s+-|\bnetcat\b)", re.I)


def _is_unsafe_path(raw_path: str) -> bool:
    if not raw_path or "\x00" in raw_path or raw_path.startswith("/"):
        return True
    normalized = posixpath.normpath(raw_path.replace("\\", "/"))
    return normalized == ".." or normalized.startswith("../") or normalized.startswith(".git/")


def _risk_level(findings: tuple[SkillGuardFinding, ...]) -> str:
    if any(finding.severity == "block" for finding in findings):
        return "critical"
    if any(finding.severity == "warning" for finding in findings):
        return "medium"
    return "low"


def scan_skill_files(files: Iterable[dict[str, Any]], *, source: str = "unknown") -> SkillGuardReport:
    """Scan a skill package before it becomes active.

    This is intentionally static and conservative. It blocks payloads that can
    escape the package, embed secrets, install remote shell scripts, or run
    destructive root-level commands. Lower-confidence operational risks are
    warnings so platform admins can still inspect them.
    """
    findings: list[SkillGuardFinding] = []

    for item in files:
        path = str(item.get("path") or "")
        content = str(item.get("content") or "")

        if _is_unsafe_path(path):
            findings.append(
                SkillGuardFinding(
                    severity="block",
                    category="path_escape",
                    path=path,
                    message="Skill file path escapes the skill package boundary.",
                    evidence={"path": path},
                )
            )
        if "\x00" in content:
            findings.append(
                SkillGuardFinding(
                    severity="block",
                    category="binary_or_null_content",
                    path=path,
                    message="Skill file contains null bytes and cannot be safely treated as text instructions.",
                    evidence={"path": path},
                )
            )
        if _PRIVATE_KEY_RE.search(content) or _SECRET_ASSIGNMENT_RE.search(content):
            findings.append(
                SkillGuardFinding(
                    severity="block",
                    category="secret_material",
                    path=path,
                    message="Skill content appears to contain embedded credentials or private key material.",
                    evidence={"path": path},
                )
            )
        if _REMOTE_SHELL_PIPE_RE.search(content):
            findings.append(
                SkillGuardFinding(
                    severity="block",
                    category="remote_shell_pipe",
                    path=path,
                    message="Skill asks to pipe a remote network payload directly into a shell/interpreter.",
                    evidence={"path": path},
                )
            )
        if _DESTRUCTIVE_ROOT_RE.search(content):
            findings.append(
                SkillGuardFinding(
                    severity="block",
                    category="destructive_root_command",
                    path=path,
                    message="Skill contains a root-level destructive command pattern.",
                    evidence={"path": path},
                )
            )
        if _PERSISTENCE_RE.search(content):
            findings.append(
                SkillGuardFinding(
                    severity="warning",
                    category="persistence_mechanism",
                    path=path,
                    message="Skill references OS persistence mechanisms that require manual review.",
                    evidence={"path": path},
                )
            )
        if _EXFIL_RE.search(content):
            findings.append(
                SkillGuardFinding(
                    severity="warning",
                    category="exfiltration_surface",
                    path=path,
                    message="Skill references sensitive local paths or network copy utilities.",
                    evidence={"path": path},
                )
            )

    result = tuple(findings)
    return SkillGuardReport(
        allowed=not any(finding.severity == "block" for finding in result),
        risk_level=_risk_level(result),
        findings=result,
        source=source,
    )
