"""Owned CDP pipes; authority and browser launch policy belong to the caller."""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.browser_lifecycle import BrowserLifecycleSpec


class BrowserPipeError(RuntimeError):
    def __init__(self, code: str, effect: str, request_id: int | None = None):
        super().__init__(code)
        self.code, self.effect, self.request_id = code, effect, request_id
        self.pipe = None
        self.resource_closed = False


@dataclass(frozen=True)
class BrowserLaunchSpec:
    executable: Path
    arguments: tuple[str, ...]
    environment: dict[str, str]
    cwd: Path
    maximum_message_bytes: int = 64 * 1024 * 1024
    lifecycle: BrowserLifecycleSpec | None = None


class BrowserPipe:
    def __init__(self, process, maximum_message_bytes: int, on_event: Callable | None):
        self._process = process
        self._maximum_bytes = maximum_message_bytes
        self._on_event = on_event
        self._ready = asyncio.get_running_loop().create_future()
        self._pending: dict[int, asyncio.Future] = {}
        self._abandoned: set[int] = set()
        self.late_results: dict[int, dict] = {}
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self.closed = False
        self._cleanup_confirmed = False
        self.lifecycle = None
        self._reader_task = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, spec: BrowserLaunchSpec, *, on_event: Callable | None = None, timeout: float = 10):
        if os.name != "posix":
            raise BrowserPipeError("browser_pipe_platform_unsupported", "not_started")
        if (
            not spec.executable.is_absolute()
            or not spec.executable.is_file()
            or not os.access(spec.executable, os.X_OK)
        ):
            raise BrowserPipeError("browser_executable_unavailable", "not_started")
        node = shutil.which("node")
        if node is None:
            raise BrowserPipeError("browser_pipe_runtime_unavailable", "not_started")
        if not spec.cwd.is_absolute() or not spec.cwd.is_dir() or spec.maximum_message_bytes <= 0:
            raise BrowserPipeError("browser_launch_spec_invalid", "not_started")
        # The helper inherits no provider, database, platform, proxy or Node options.
        try:
            process = await asyncio.create_subprocess_exec(
                node,
                str(Path(__file__).with_name("browser_pipe_bridge.mjs")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},
                cwd=spec.cwd,
                limit=spec.maximum_message_bytes * 2 + 4096,
            )
        except OSError as exc:
            raise BrowserPipeError("browser_pipe_runtime_unavailable", "not_started") from exc
        pipe = cls(process, spec.maximum_message_bytes, on_event)
        pipe.lifecycle = spec.lifecycle
        try:
            await pipe._write(
                {
                    "type": "launch",
                    "executable": str(spec.executable),
                    "arguments": spec.arguments,
                    "cwd": str(spec.cwd),
                    "environment": spec.environment,
                    "maximum_message_bytes": spec.maximum_message_bytes,
                    **({"lifecycle": spec.lifecycle.bridge_frame()} if spec.lifecycle is not None else {}),
                }
            )
            await asyncio.wait_for(asyncio.shield(pipe._ready), timeout)
        except BaseException as exc:
            await pipe.close()
            if isinstance(exc, (asyncio.TimeoutError, OSError)):
                error = BrowserPipeError("browser_launch_unconfirmed", "unknown")
                error.resource_closed = True
                raise error from exc
            if isinstance(exc, (BrowserPipeError, asyncio.CancelledError)):
                exc.resource_closed = True
            raise
        return pipe

    async def _write(self, value):
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            if self.closed or self._process.returncode is not None:
                raise BrowserPipeError("browser_pipe_closed", "not_started")
            self._process.stdin.write(body)
            await self._process.stdin.drain()

    def _fail(self, code, effect="unknown"):
        if not self._ready.done():
            self._ready.set_exception(BrowserPipeError(code, effect))
        for request_id, future in self._pending.items():
            if not future.done():
                future.set_exception(BrowserPipeError(code, "unknown", request_id))

    async def _read(self):
        try:
            while line := await self._process.stdout.readline():
                frame = json.loads(line)
                kind = frame.get("type")
                if kind == "ready":
                    if not self._ready.done():
                        self._ready.set_result(frame["pid"])
                elif kind == "message":
                    message = frame["message"]
                    if not isinstance(message, dict):
                        raise ValueError("invalid message")
                    request_id = message.get("id")
                    if type(request_id) is int and request_id in self._pending:
                        future = self._pending[request_id]
                        if not future.done():
                            future.set_result(message)
                    elif type(request_id) is int and request_id in self._abandoned:
                        self.late_results[request_id] = message
                        self._abandoned.remove(request_id)
                    elif request_id is None and self._on_event is not None:
                        self._on_event(message)
                elif kind == "launch_failed":
                    self._cleanup_confirmed = True
                    self._fail("browser_executable_unavailable", "not_started")
                elif kind == "message_limit":
                    self._fail("browser_message_limit")
                elif kind == "closed":
                    self._cleanup_confirmed = True
                    self._fail("browser_pipe_lost")
                elif kind in {"pipe_failed", "protocol_failed", "cleanup_failed"}:
                    self._fail("browser_pipe_lost")
                else:
                    raise ValueError("invalid bridge frame")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self._fail("browser_protocol_failed")
        finally:
            self.closed = True
            self._fail("browser_pipe_lost")

    async def call(
        self, method: str, parameters: dict[str, Any], *, session_id: str | None = None, timeout: float = 30
    ):
        self._sequence += 1
        request_id = self._sequence
        message = {"id": request_id, "method": method, "params": parameters}
        if session_id is not None:
            message["sessionId"] = session_id
        # Reject before writing; once written an absent ACK never authorizes replay.
        if len(json.dumps(message, ensure_ascii=False).encode()) > self._maximum_bytes:
            raise BrowserPipeError("browser_message_limit", "not_started", request_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        sent = False
        try:
            if self.closed:
                raise BrowserPipeError("browser_pipe_closed", "not_started", request_id)
            sent = True
            await self._write({"type": "command", "message": message})
            response = await asyncio.wait_for(asyncio.shield(future), timeout)
            if "error" in response:
                raise BrowserPipeError("browser_protocol_rejected", "unknown", request_id)
            return response.get("result", {})
        except asyncio.TimeoutError as exc:
            self._abandoned.add(request_id)
            raise BrowserPipeError("browser_response_timeout", "unknown", request_id) from exc
        except asyncio.CancelledError as exc:
            if sent:
                self._abandoned.add(request_id)
            exc.request_id = request_id
            raise
        except (OSError, ConnectionError) as exc:
            self._abandoned.add(request_id)
            raise BrowserPipeError("browser_pipe_lost", "unknown", request_id) from exc
        finally:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                if request_id in self._abandoned and future.exception() is None:
                    self.late_results[request_id] = future.result()
                    self._abandoned.remove(request_id)
                else:
                    future.exception()
            else:
                future.cancel()

    async def close(self):
        if self._process.returncode is None:
            try:
                await self._write({"type": "close"})
            except (OSError, BrowserPipeError):
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except asyncio.TimeoutError:
                # The helper handles SIGTERM by reaping its child before exiting.
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), 5)
                except asyncio.TimeoutError as exc:
                    error = BrowserPipeError("browser_cleanup_unconfirmed", "unknown")
                    error.pipe = self
                    raise error from exc
        await self._reader_task
        self.closed = True
        if not self._cleanup_confirmed:
            error = BrowserPipeError("browser_cleanup_unconfirmed", "unknown")
            error.pipe = self
            raise error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()
