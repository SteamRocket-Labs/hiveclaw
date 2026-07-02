"""Unified command registry for CC/Codex parity.

Commands are the user/session control surface. They are related to, but not the
same as, LLM tools: the prompt should usually see a compact command index and
load the full schema only when a command is selected.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.coding_pack_manifest import CODING_PACK_COMMANDS, CODING_PACK_PERMISSION_MODE

CommandSource = Literal["builtin", "tool", "skill", "plugin", "workflow", "mcp", "team", "agent", "diagnostic"]
CommandExecutionMode = Literal["metadata", "runtime", "tool", "workflow", "external"]


class CommandDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    category: str
    source: CommandSource
    execution_mode: CommandExecutionMode
    handler_ref: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    aliases: tuple[str, ...] = ()
    permission_mode: str = "default"
    bridge_safe: bool = True
    remote_safe: bool = True
    visible_to_model: bool = True
    visible_to_user: bool = True

    def index_entry(self, *, surface: Literal["agent_prompt", "user"] = "agent_prompt") -> dict[str, Any]:
        """Return the prompt/UI index entry without the heavy input schema."""
        user_name = self.aliases[0] if surface == "user" and self.aliases else self.name
        return {
            "name": user_name,
            "canonical_name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "category": self.category,
            "source": self.source,
            "execution_mode": self.execution_mode,
            "permission_mode": self.permission_mode,
            "bridge_safe": self.bridge_safe,
            "remote_safe": self.remote_safe,
        }


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, CommandDefinition] = {}
        self._aliases: dict[str, str] = {}

    def register(self, command: CommandDefinition) -> None:
        existing = self._commands.get(command.name)
        if existing is not None and existing.handler_ref != command.handler_ref:
            raise ValueError(f"duplicate command {command.name!r}")
        if existing is not None and existing.source != command.source:
            raise ValueError(f"duplicate command {command.name!r} from source {command.source!r}")
        self._commands[command.name] = command
        for alias in command.aliases:
            owner = self._aliases.get(alias)
            if owner is not None and owner != command.name:
                raise ValueError(f"duplicate command alias {alias!r}")
            self._aliases[alias] = command.name

    def get(self, name: str) -> CommandDefinition:
        canonical = self._aliases.get(name, name)
        return self._commands[canonical]

    def values(self) -> list[CommandDefinition]:
        return [self._commands[name] for name in sorted(self._commands)]

    def visible_index(self, *, surface: Literal["agent_prompt", "user"] = "agent_prompt") -> list[dict[str, Any]]:
        if surface == "agent_prompt":
            commands = [command for command in self.values() if command.visible_to_model]
        else:
            commands = [command for command in self.values() if command.visible_to_user]
        return [command.index_entry(surface=surface) for command in commands]


def _command(
    name: str,
    description: str,
    *,
    category: str,
    source: CommandSource = "builtin",
    execution_mode: CommandExecutionMode = "metadata",
    handler_ref: str | None = None,
    input_schema: dict[str, Any] | None = None,
    aliases: tuple[str, ...] = (),
    permission_mode: str = "default",
    bridge_safe: bool = True,
    remote_safe: bool = True,
    visible_to_model: bool = True,
    visible_to_user: bool = True,
) -> CommandDefinition:
    return CommandDefinition(
        name=name,
        aliases=aliases,
        description=description,
        category=category,
        source=source,
        execution_mode=execution_mode,
        handler_ref=handler_ref or f"builtin:{name}",
        input_schema=input_schema or {"type": "object", "properties": {}},
        permission_mode=permission_mode,
        bridge_safe=bridge_safe,
        remote_safe=remote_safe,
        visible_to_model=visible_to_model,
        visible_to_user=visible_to_user,
    )


def _coerce_command_definition(value: CommandDefinition | dict[str, Any]) -> CommandDefinition:
    if isinstance(value, CommandDefinition):
        return value
    return CommandDefinition(**value)


def register_dynamic_commands(
    registry: CommandRegistry,
    dynamic_commands: list[CommandDefinition | dict[str, Any]] | None,
) -> None:
    """Register Skill/Workflow/MCP/plugin commands discovered at runtime."""
    for raw in dynamic_commands or []:
        registry.register(_coerce_command_definition(raw))


def _optional_coding_pack_commands(
    *, visible_to_model: bool = False, visible_to_user: bool = False
) -> list[CommandDefinition]:
    return [
        _command(
            spec.name,
            spec.description,
            category="coding_pack",
            source="plugin",
            execution_mode="external",
            handler_ref=spec.handler_ref,
            input_schema=spec.input_schema,
            aliases=spec.aliases,
            permission_mode=CODING_PACK_PERMISSION_MODE,
            bridge_safe=False,
            remote_safe=False,
            visible_to_model=visible_to_model,
            visible_to_user=visible_to_user,
        )
        for spec in CODING_PACK_COMMANDS
    ]


def build_default_command_registry(
    *,
    dynamic_commands: list[CommandDefinition | dict[str, Any]] | None = None,
    include_optional_coding_pack: bool = False,
    optional_coding_pack_model_visible: bool = False,
) -> CommandRegistry:
    registry = CommandRegistry()
    commands = [
        _command(
            "plan",
            "Enter Plan Mode for the next agent turn.",
            category="plan",
            execution_mode="runtime",
            permission_mode="plan",
        ),
        _command("resume", "Resume a previous session from durable transcript evidence.", category="session"),
        _command(
            "checkpoints",
            "List user-turn session checkpoints available for rewind or rollback.",
            category="session",
            visible_to_user=False,
        ),
        _command(
            "rewind",
            "Rewind the current session active projection to a selected user-turn checkpoint.",
            category="session",
        ),
        _command(
            "rollback",
            "Roll back N user turns by updating the current session active projection.",
            category="session",
            input_schema={
                "type": "object",
                "properties": {
                    "num_turns": {"type": "integer", "minimum": 1, "default": 1},
                    "checkpoint_event_id": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            visible_to_user=False,
        ),
        _command("branch", "Fork a conversation from a transcript event.", category="session"),
        _command(
            "btw",
            "Ask a one-turn side question in a durable side session without polluting the main thread.",
            category="session",
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "anchor_event_id": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["question"],
            },
            visible_to_user=False,
        ),
        _command(
            "turn_steer",
            "Steer the currently active turn by queuing an additional user message.",
            category="session",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "expected_turn_id": {"type": "string"},
                },
                "required": ["content"],
            },
            visible_to_user=False,
        ),
        _command(
            "interrupt",
            "Interrupt the current active session turn.",
            category="session",
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                },
            },
            visible_to_user=False,
        ),
        _command("rename", "Rename the current session.", category="session", visible_to_user=False),
        _command("tag", "Tag the current session for later lookup.", category="session", visible_to_user=False),
        _command(
            "export",
            "Export session transcript, artifacts, plan, and ledger evidence.",
            category="session",
            visible_to_user=False,
        ),
        _command(
            "copy",
            "Return assistant response content for client clipboard or file copy.",
            category="session",
            input_schema={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "minimum": 1, "default": 1},
                    "index": {"type": "integer", "minimum": 1},
                },
            },
            visible_to_user=False,
        ),
        _command("clear", "Start a clean context boundary while retaining durable evidence.", category="session"),
        _command("compact", "Run manual compaction with pre/post compaction hooks.", category="session"),
        _command(
            "team_create",
            "Create a CC-style Team container under the current session. Spawn teammates with spawn_subagent team_name + name.",
            category="team",
            execution_mode="runtime",
            permission_mode="write",
            aliases=("team",),
            input_schema={
                "type": "object",
                "properties": {
                    "team_name": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "agent_type": {"type": "string"},
                },
                "anyOf": [{"required": ["team_name"]}, {"required": ["name"]}],
            },
        ),
        _command(
            "team_delete",
            "Close or delete a team workspace.",
            category="team",
            execution_mode="runtime",
            visible_to_user=False,
        ),
        _command(
            "task_create",
            "Create a FreeCode-style task: current-session Work Ledger step by default, or delegated agent work when kind=delegation.",
            category="task",
            execution_mode="runtime",
            aliases=("task",),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["todo", "delegation"], "default": "todo"},
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                    "owner": {"type": "string"},
                    "agent_name": {"type": "string"},
                    "target_agent_id": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        ),
        _command(
            "task_list",
            "List current Work Ledger tasks, or delegated async tasks when kind=delegation.",
            category="task",
            execution_mode="runtime",
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["todo", "delegation"], "default": "todo"}},
            },
        ),
        _command(
            "task_get",
            "Get one Work Ledger task, or a delegated async task when kind=delegation.",
            category="task",
            execution_mode="runtime",
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["todo", "delegation"], "default": "todo"},
                    "task_id": {"type": "string"},
                },
            },
        ),
        _command(
            "task_update",
            "Update a current-session Work Ledger task.",
            category="task",
            execution_mode="runtime",
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    "owner": {"type": "string"},
                },
                "required": ["task_id"],
            },
        ),
        _command(
            "task_output",
            "Read output from an executable RuntimeTask.",
            category="task",
            execution_mode="runtime",
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {"runtime_task_id": {"type": "string"}},
                "required": ["runtime_task_id"],
            },
        ),
        _command(
            "task_stop",
            "Stop an executable RuntimeTask, or a delegated async task when kind=delegation.",
            category="task",
            execution_mode="runtime",
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["runtime", "delegation"], "default": "runtime"},
                    "runtime_task_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        ),
        _command(
            "goal_start",
            "Start a session-scoped goal with bounded continuation.",
            category="goal",
            execution_mode="runtime",
            aliases=("goal",),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "token_budget": {"type": "integer"},
                    "max_continuation_turns": {"type": "integer"},
                },
                "required": ["objective"],
            },
        ),
        _command(
            "goal_update",
            "Update the active session goal.",
            category="goal",
            execution_mode="runtime",
            visible_to_user=False,
        ),
        _command(
            "goal_stop",
            "Pause, complete, or cancel the active session goal.",
            category="goal",
            execution_mode="runtime",
            visible_to_user=False,
        ),
        _command(
            "schedule_create",
            "Create a session-requested scheduled task draft or enabled cron wake policy.",
            category="schedule",
            execution_mode="runtime",
            permission_mode="write",
            aliases=("schedule",),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "instruction": {"type": "string"},
                    "cron_expr": {"type": "string"},
                    "is_enabled": {"type": "boolean", "default": False},
                    "delivery_target_json": {"type": "object"},
                    "confirmed_plan_id": {"type": "string"},
                    "confirmed_plan_version": {"type": "integer"},
                    "confirmed_plan_hash": {"type": "string"},
                    "plan_mode_decision": {"type": "string"},
                    "plan_recommendation_id": {"type": "string"},
                },
                "required": ["name", "instruction", "cron_expr"],
            },
        ),
        _command(
            "schedule_once",
            "Create a session-requested one-time task draft or enabled one-shot wake policy.",
            category="schedule",
            execution_mode="runtime",
            permission_mode="write",
            aliases=("once",),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "instruction": {"type": "string"},
                    "at": {
                        "type": "string",
                        "description": "ISO-8601 timestamp for the one-time wake, e.g. 2026-06-26T09:00:00Z.",
                    },
                    "is_enabled": {"type": "boolean", "default": False},
                    "delivery_target_json": {"type": "object"},
                    "confirmed_plan_id": {"type": "string"},
                    "confirmed_plan_version": {"type": "integer"},
                    "confirmed_plan_hash": {"type": "string"},
                    "plan_mode_decision": {"type": "string"},
                    "plan_recommendation_id": {"type": "string"},
                },
                "required": ["name", "instruction", "at"],
            },
        ),
        _command(
            "advanced_plan",
            "Run a detached advanced planning task that returns a plan artifact for approval.",
            category="plan",
            execution_mode="runtime",
            permission_mode="plan",
            visible_to_model=False,
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {"objective": {"type": "string"}, "context": {"type": "object"}},
                "required": ["objective"],
            },
        ),
        _command(
            "verify_plan",
            "Verify plan execution against success criteria and evidence refs.",
            category="plan",
            execution_mode="runtime",
            permission_mode="read",
            visible_to_model=False,
            visible_to_user=False,
            input_schema={
                "type": "object",
                "properties": {
                    "plan_json": {"type": "object"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "completed_criteria": {"type": "array", "items": {"type": "string"}},
                    "failed_criteria": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["plan_json"],
            },
        ),
        _command(
            "load_skill",
            "Load a progressive disclosure skill capsule.",
            category="skill",
            source="skill",
            execution_mode="tool",
            handler_ref="tool:load_skill",
            visible_to_user=False,
        ),
        _command(
            "start_workflow",
            "Start a governed workflow runtime.",
            category="workflow",
            source="workflow",
            execution_mode="workflow",
            handler_ref="tool:start_workflow",
            visible_to_user=False,
        ),
        _command(
            "preview_workflow",
            "Preview a workflow before execution.",
            category="workflow",
            source="workflow",
            execution_mode="workflow",
            handler_ref="tool:preview_workflow",
            visible_to_user=False,
        ),
        _command(
            "mcp",
            "List or call approved MCP command wrappers.",
            category="mcp",
            source="mcp",
            execution_mode="tool",
            handler_ref="mcp:command",
        ),
        _command("permissions", "Inspect or request permission changes through governed hooks.", category="governance"),
        _command("config", "Inspect or update safe runtime configuration.", category="config", visible_to_user=False),
        _command(
            "skill",
            "List or invoke installed Skills. Individual Skills also appear as slash commands.",
            category="skill",
            source="skill",
            execution_mode="runtime",
            handler_ref="product:skill",
        ),
        _command(
            "agent",
            "Delegate a task to a Sub-Agent or open the Sub-Agent catalog.",
            category="agent",
            source="agent",
            execution_mode="runtime",
            handler_ref="product:agent",
        ),
        _command(
            "workflow",
            "Open the Dynamic Workflow surface or draft a Dynamic Workflow request.",
            category="workflow",
            source="workflow",
            execution_mode="workflow",
            handler_ref="product:dynamic_workflow",
        ),
    ]
    for command in commands:
        registry.register(command)
    for name in ("status", "usage", "cost", "stats", "context", "doctor", "version"):
        registry.register(
            _command(
                name,
                f"Read-only {name} diagnostic command.",
                category="diagnostic",
                source="diagnostic",
                execution_mode="metadata",
                handler_ref=f"diagnostic:{name}",
                visible_to_user=name in {"usage", "context"},
            )
        )
    if include_optional_coding_pack:
        for command in _optional_coding_pack_commands(
            visible_to_model=optional_coding_pack_model_visible,
            visible_to_user=False,
        ):
            registry.register(command)
    register_dynamic_commands(registry, dynamic_commands)
    return registry
