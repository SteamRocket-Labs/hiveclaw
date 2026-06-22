from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class WorkResult:
    result: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


WorkResultValue = WorkResult | str | dict[str, Any]


class WorkRequestAdapter(Protocol):
    def handle(self, message: dict[str, Any]) -> WorkResultValue:
        ...


def coerce_work_result(value: WorkResultValue) -> WorkResult:
    if isinstance(value, WorkResult):
        return value
    if isinstance(value, dict):
        result = value.get("result", value.get("content", ""))
        return WorkResult(
            result=str(result),
            attachments=list(value.get("attachments") or []),
            metadata=dict(value.get("metadata") or {}),
        )
    return WorkResult(result=str(value))


@dataclass
class NoopAdapter:
    def handle(self, message: dict[str, Any]) -> WorkResult:
        return WorkResult(
            result=(
                "Received work_request locally, but no runtime adapter is configured.\n\n"
                f"Content:\n{message.get('content', '')}"
            ),
            metadata={"runtime": "noop"},
        )


@dataclass
class CommandAdapter:
    command: list[str]
    timeout_seconds: int = 600

    def handle(self, message: dict[str, Any]) -> WorkResult:
        proc = subprocess.run(
            self.command,
            input=message.get("content", ""),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        output = (proc.stdout or "").strip()
        error = (proc.stderr or "").strip()
        metadata: dict[str, Any] = {
            "runtime": "command",
            "command": self.command,
            "exit_code": proc.returncode,
        }
        if proc.returncode == 0:
            return WorkResult(result=output or "(command completed with no output)", metadata=metadata)
        return WorkResult(
            result=(
                f"Command adapter failed with exit code {proc.returncode}.\n\n"
                f"STDOUT:\n{output}\n\n"
                f"STDERR:\n{error}"
            ),
            metadata=metadata,
        )


RuntimeFactory = Callable[..., WorkRequestAdapter]


class RuntimeRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, RuntimeFactory] = {}

    def register(self, name: str, factory: RuntimeFactory) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("Runtime name cannot be empty")
        self._factories[normalized] = factory

    def names(self) -> list[str]:
        return sorted(self._factories)

    def create(self, name: str, **kwargs: Any) -> WorkRequestAdapter:
        normalized = name.strip().lower()
        factory = self._factories.get(normalized)
        if factory is None:
            raise ValueError(f"Unknown local runtime '{name}'. Available runtimes: {', '.join(self.names())}")
        return factory(**kwargs)


def create_default_runtime_registry() -> RuntimeRegistry:
    registry = RuntimeRegistry()
    registry.register("noop", lambda **_kwargs: NoopAdapter())

    def command_factory(**kwargs: Any) -> CommandAdapter:
        command = kwargs.get("command")
        if not command:
            raise ValueError("The command runtime requires a command list")
        return CommandAdapter(
            command=[str(part) for part in command],
            timeout_seconds=int(kwargs.get("timeout_seconds") or 600),
        )

    registry.register("command", command_factory)

    def acp_factory(**kwargs: Any) -> WorkRequestAdapter:
        from hive_bridge.acp_runtime import ACPAdapter

        command = kwargs.get("command")
        if not command:
            raise ValueError("The acp runtime requires a command list")
        return ACPAdapter(
            command=[str(part) for part in command],
            work_dir=str(kwargs.get("work_dir") or "."),
            timeout_seconds=int(kwargs.get("timeout_seconds") or 600),
        )

    registry.register("acp", acp_factory)
    return registry
