"""Exact, authority-backed secret egress boundary.

Natural-language patterns are not credential authority.  This module accepts
only values resolved from a trusted credential store/binding and compares
those exact values against payloads or output text.  It never stores matched
secret bytes in receipts, logs, or result objects.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any, Awaitable, Callable, Iterable


_REDACTION = "[REDACTED_SECRET]"


class ExactSecretEgressError(RuntimeError):
    """Sanitized replacement for an escaping error that contained bound bytes."""

    def __init__(self, message: str, *, source_refs: tuple[str, ...]) -> None:
        super().__init__(message)
        self.source_refs = source_refs


@dataclass(frozen=True, slots=True)
class ExactSecretRedaction:
    text: str
    matched_refs: tuple[str, ...] = ()
    redacted_count: int = 0


@dataclass(frozen=True, slots=True)
class ExactSecretPayloadRedaction:
    value: Any
    matched_refs: tuple[str, ...] = ()
    redacted_count: int = 0


@dataclass(frozen=True, slots=True)
class _ExactSecretBinding:
    value: str = field(repr=False)
    refs: tuple[str, ...]


class ExactSecretBoundary:
    """A non-serializable in-process set of exact protected byte strings."""

    __slots__ = ("_bindings",)

    def __init__(self, bindings: tuple[_ExactSecretBinding, ...] = ()) -> None:
        self._bindings = bindings

    def __repr__(self) -> str:
        return f"ExactSecretBoundary(bindings={len(self._bindings)})"

    @classmethod
    def empty(cls) -> ExactSecretBoundary:
        return cls()

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, str]]) -> ExactSecretBoundary:
        refs_by_value: dict[str, list[str]] = {}
        for raw_ref, raw_value in pairs:
            ref = str(raw_ref or "").strip()
            value = str(raw_value or "")
            if not ref or not value:
                continue
            refs = refs_by_value.setdefault(value, [])
            if ref not in refs:
                refs.append(ref)
        bindings = tuple(
            _ExactSecretBinding(value=value, refs=tuple(refs))
            for value, refs in sorted(refs_by_value.items(), key=lambda item: (-len(item[0]), item[0]))
        )
        return cls(bindings)

    @classmethod
    def combine(cls, *boundaries: ExactSecretBoundary) -> ExactSecretBoundary:
        refs_by_value: dict[str, list[str]] = {}
        for boundary in boundaries:
            for binding in boundary._bindings:
                refs = refs_by_value.setdefault(binding.value, [])
                for ref in binding.refs:
                    if ref not in refs:
                        refs.append(ref)
        return cls.from_pairs((ref, value) for value, refs in refs_by_value.items() for ref in refs)

    @property
    def is_empty(self) -> bool:
        return not self._bindings

    @property
    def max_secret_length(self) -> int:
        return max((len(binding.value) for binding in self._bindings), default=0)

    def match_payload(self, payload: Any) -> tuple[str, ...]:
        matched: list[str] = []
        for text in _iter_payload_strings(payload):
            for ref in self._matched_refs(text):
                if ref not in matched:
                    matched.append(ref)
        return tuple(matched)

    def match_binary_chunks(self, chunks: Iterable[bytes]) -> tuple[str, ...]:
        """Match exact UTF-8 credential bytes without retaining the file body."""

        encoded_bindings = tuple(
            (binding, binding.value.encode("utf-8")) for binding in self._bindings if binding.value
        )
        max_length = max(
            (len(encoded) for _binding, encoded in encoded_bindings),
            default=0,
        )
        if max_length <= 0:
            return ()

        matched_refs: list[str] = []
        tail = b""
        for raw_chunk in chunks:
            chunk = bytes(raw_chunk)
            if not chunk:
                continue
            window = tail + chunk
            for binding, encoded in encoded_bindings:
                if encoded in window:
                    _extend_unique(matched_refs, binding.refs)
            tail = window[-(max_length - 1) :] if max_length > 1 else b""
        return tuple(matched_refs)

    def redact_text(self, text: str) -> ExactSecretRedaction:
        source = str(text)
        matches = self._non_overlapping_matches(source)
        if not matches:
            return ExactSecretRedaction(text=source)

        rendered: list[str] = []
        matched_refs: list[str] = []
        cursor = 0
        for start, end, refs in matches:
            rendered.append(source[cursor:start])
            rendered.append(_REDACTION)
            cursor = end
            for ref in refs:
                if ref not in matched_refs:
                    matched_refs.append(ref)
        rendered.append(source[cursor:])
        return ExactSecretRedaction(
            text="".join(rendered),
            matched_refs=tuple(matched_refs),
            redacted_count=len(matches),
        )

    def redact_payload(self, payload: Any) -> Any:
        return self.redact_payload_with_evidence(payload).value

    def redact_payload_with_evidence(self, payload: Any) -> ExactSecretPayloadRedaction:
        if isinstance(payload, str):
            redaction = self.redact_text(payload)
            return ExactSecretPayloadRedaction(
                value=redaction.text,
                matched_refs=redaction.matched_refs,
                redacted_count=redaction.redacted_count,
            )
        if is_dataclass(payload) and not isinstance(payload, type):
            replacements: dict[str, Any] = {}
            matched_refs: list[str] = []
            redacted_count = 0
            for data_field in fields(payload):
                redaction = self.redact_payload_with_evidence(getattr(payload, data_field.name))
                replacements[data_field.name] = redaction.value
                redacted_count += redaction.redacted_count
                _extend_unique(matched_refs, redaction.matched_refs)
            return ExactSecretPayloadRedaction(
                value=replace(payload, **replacements),
                matched_refs=tuple(matched_refs),
                redacted_count=redacted_count,
            )
        if isinstance(payload, dict):
            output: dict[Any, Any] = {}
            matched_refs: list[str] = []
            redacted_count = 0
            for key, value in payload.items():
                key_redaction = (
                    self.redact_payload_with_evidence(key)
                    if isinstance(key, str)
                    else ExactSecretPayloadRedaction(value=key)
                )
                value_redaction = self.redact_payload_with_evidence(value)
                safe_key = key_redaction.value
                if safe_key in output and safe_key != key:
                    suffix = 2
                    candidate = f"{safe_key}#{suffix}"
                    while candidate in output:
                        suffix += 1
                        candidate = f"{safe_key}#{suffix}"
                    safe_key = candidate
                output[safe_key] = value_redaction.value
                redacted_count += key_redaction.redacted_count + value_redaction.redacted_count
                _extend_unique(matched_refs, key_redaction.matched_refs)
                _extend_unique(matched_refs, value_redaction.matched_refs)
            return ExactSecretPayloadRedaction(
                value=output,
                matched_refs=tuple(matched_refs),
                redacted_count=redacted_count,
            )
        if isinstance(payload, list):
            return self._redact_sequence(payload, list)
        if isinstance(payload, tuple):
            return self._redact_sequence(payload, tuple)
        if isinstance(payload, set):
            return self._redact_sequence(payload, set)
        if isinstance(payload, frozenset):
            return self._redact_sequence(payload, frozenset)
        return ExactSecretPayloadRedaction(value=payload)

    def _redact_sequence(
        self,
        payload: Iterable[Any],
        constructor: Callable[[Iterable[Any]], Any],
    ) -> ExactSecretPayloadRedaction:
        values: list[Any] = []
        matched_refs: list[str] = []
        redacted_count = 0
        for value in payload:
            redaction = self.redact_payload_with_evidence(value)
            values.append(redaction.value)
            redacted_count += redaction.redacted_count
            _extend_unique(matched_refs, redaction.matched_refs)
        return ExactSecretPayloadRedaction(
            value=constructor(values),
            matched_refs=tuple(matched_refs),
            redacted_count=redacted_count,
        )

    def _matched_refs(self, text: str) -> tuple[str, ...]:
        refs: list[str] = []
        for binding in self._bindings:
            for _index in self._binding_occurrences(text, binding):
                for ref in binding.refs:
                    if ref not in refs:
                        refs.append(ref)
                break
        return tuple(refs)

    def _non_overlapping_matches(self, text: str) -> list[tuple[int, int, tuple[str, ...]]]:
        candidates: list[tuple[int, int, tuple[str, ...]]] = []
        for binding in self._bindings:
            for index in self._binding_occurrences(text, binding):
                candidates.append((index, index + len(binding.value), binding.refs))
        candidates.sort(key=lambda match: (match[0], -(match[1] - match[0])))

        selected: list[tuple[int, int, tuple[str, ...]]] = []
        cursor = 0
        for match in candidates:
            if match[0] < cursor:
                continue
            selected.append(match)
            cursor = match[1]
        return selected

    @staticmethod
    def _binding_occurrences(text: str, binding: _ExactSecretBinding):
        start = 0
        value = binding.value
        while True:
            index = text.find(value, start)
            if index < 0:
                return
            end = index + len(value)
            if len(value) >= 8 or (
                (index == 0 or not _is_identifier_char(text[index - 1]))
                and (end == len(text) or not _is_identifier_char(text[end]))
            ):
                yield index
            start = end


def boundary_from_channel_config(
    config: Any,
    *,
    agent_id: Any,
    channel: str,
) -> ExactSecretBoundary:
    """Build exact bindings from one already-authorized decrypted channel row."""

    if config is None:
        return ExactSecretBoundary.empty()
    prefix = f"channel-config://{agent_id}/{channel}"
    pairs: list[tuple[str, str]] = []
    for field_name in ("app_secret", "encrypt_key", "verification_token"):
        value = getattr(config, field_name, None)
        if isinstance(value, str) and value:
            pairs.append((f"{prefix}/{field_name}", value))

    try:
        from app.services.channel_secret_storage import CHANNEL_EXTRA_SECRET_KEYS

        _collect_structural_secret_pairs(
            getattr(config, "extra_config", None),
            prefix=f"{prefix}/extra_config",
            secret_keys=CHANNEL_EXTRA_SECRET_KEYS,
            pairs=pairs,
        )
    except (ImportError, RuntimeError):
        # The three typed channel columns above remain authoritative. Startup
        # wiring errors are handled by the caller's channel-config read path.
        pass
    return ExactSecretBoundary.from_pairs(pairs)


def boundary_from_reply_target(target: Any) -> ExactSecretBoundary:
    """Resolve ephemeral transport secrets from typed channel target fields."""

    if not isinstance(target, dict):
        return ExactSecretBoundary.empty()
    channel = str(target.get("channel") or "").strip().casefold()
    if channel == "teams":
        channel = "microsoft_teams"
    protected_fields = {
        "dingtalk": ("session_webhook",),
        "discord": ("interaction_token",),
        "wechat_personal": ("context_token",),
    }.get(channel, ())
    return ExactSecretBoundary.from_pairs(
        (
            f"channel-target://{channel}/{field_name}",
            str(target.get(field_name) or ""),
        )
        for field_name in protected_fields
    )


def _collect_structural_secret_pairs(
    value: Any,
    *,
    prefix: str,
    secret_keys: frozenset[str],
    pairs: list[tuple[str, str]],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}/{key}"
            if str(key).casefold() in secret_keys and isinstance(child, str) and child:
                pairs.append((child_prefix, child))
            else:
                _collect_structural_secret_pairs(
                    child,
                    prefix=child_prefix,
                    secret_keys=secret_keys,
                    pairs=pairs,
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _collect_structural_secret_pairs(
                child,
                prefix=f"{prefix}/{index}",
                secret_keys=secret_keys,
                pairs=pairs,
            )


def _iter_payload_strings(payload: Any):
    if isinstance(payload, str):
        yield payload
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str):
                yield key
            yield from _iter_payload_strings(value)
        return
    if isinstance(payload, (list, tuple, set, frozenset)):
        for value in payload:
            yield from _iter_payload_strings(value)


def _is_identifier_char(value: str) -> bool:
    return value.isalnum() or value == "_"


ChunkSink = Callable[[str], Awaitable[None] | None]


class ExactSecretStreamRedactor:
    """Incremental redactor that safely handles secrets split across chunks."""

    __slots__ = (
        "_boundary",
        "_buffer",
        "_finished",
        "_matched_refs",
        "_previous_source_char",
        "_redacted_count",
        "_sink",
    )

    def __init__(self, boundary: ExactSecretBoundary, sink: ChunkSink) -> None:
        self._boundary = boundary
        self._buffer = ""
        self._sink = sink
        self._finished = False
        self._matched_refs: list[str] = []
        self._previous_source_char = ""
        self._redacted_count = 0

    @property
    def matched_refs(self) -> tuple[str, ...]:
        return tuple(self._matched_refs)

    @property
    def redacted_count(self) -> int:
        return self._redacted_count

    def reset_pending(self) -> None:
        """Drop an undecided suffix when the provider stream is tombstoned."""

        self._buffer = ""
        self._previous_source_char = ""

    async def feed(self, chunk: str) -> None:
        if self._finished:
            raise RuntimeError("secret stream redactor is already finished")
        self._buffer += str(chunk)
        await self._drain(final=False)

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        await self._drain(final=True)

    async def _drain(self, *, final: bool) -> None:
        max_length = self._boundary.max_secret_length
        if max_length <= 0:
            if self._buffer:
                await _maybe_await(self._sink(self._buffer))
                self._buffer = ""
            return

        rendered: list[str] = []
        while self._buffer and (final or len(self._buffer) > max_length):
            matching = [
                binding for binding in self._boundary._bindings if self._matches_at_buffer_start(binding, final=final)
            ]
            if matching:
                binding = matching[0]
                rendered.append(_REDACTION)
                self._buffer = self._buffer[len(binding.value) :]
                self._previous_source_char = binding.value[-1]
                self._redacted_count += 1
                _extend_unique(self._matched_refs, binding.refs)
            else:
                source_char = self._buffer[0]
                rendered.append(source_char)
                self._buffer = self._buffer[1:]
                self._previous_source_char = source_char
        if rendered:
            await _maybe_await(self._sink("".join(rendered)))

    def _matches_at_buffer_start(
        self,
        binding: _ExactSecretBinding,
        *,
        final: bool,
    ) -> bool:
        if not self._buffer.startswith(binding.value):
            return False
        if len(binding.value) >= 8:
            return True
        if self._previous_source_char and _is_identifier_char(self._previous_source_char):
            return False
        end = len(binding.value)
        if end < len(self._buffer):
            return not _is_identifier_char(self._buffer[end])
        return final


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)
