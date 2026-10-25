"""Request-local snapshots captured by the actual RuntimeTask SQL readers."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from sqlalchemy import inspect


class NativeTaskReadCapture:
    def __init__(self):
        self._observations = []

    def observe(self, task, record):
        snapshot = SimpleNamespace(**{column.key: deepcopy(getattr(task, column.key))
            for column in inspect(task).mapper.column_attrs})
        self._observations.append((deepcopy(record), snapshot, datetime.now(timezone.utc).isoformat()))

    def matching(self, record):
        return next(((task, timestamp) for original, task, timestamp in reversed(self._observations)
            if original == record), None)

    def records_for(self, task_id):
        identity = uuid.UUID(str(task_id)).hex
        return [record for record, _task, _timestamp in reversed(self._observations) if record["task_id"] == identity]


_active_capture = ContextVar("native_runtime_task_read", default=None)


@contextmanager
def capture_native_task_reads():
    capture = NativeTaskReadCapture()
    token = _active_capture.set(capture)
    try:
        yield capture
    finally:
        _active_capture.reset(token)


def observe_native_task_read(task, record):
    capture = _active_capture.get()
    if capture is not None:
        capture.observe(task, record)
