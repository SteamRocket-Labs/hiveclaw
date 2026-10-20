"""Separate a real output recipient's source access from business inspection."""

from contextlib import contextmanager
from contextvars import ContextVar

_RECIPIENT_READ = ContextVar("source_recipient_read", default=None)


@contextmanager
def source_recipient_read(*, session_id, user_id):
    """Trusted readers set purpose; neither content nor model arguments set it."""
    token = _RECIPIENT_READ.set((str(session_id), str(user_id)))
    try:
        yield
    finally:
        _RECIPIENT_READ.reset(token)


def business_inspection_allowed():
    return _RECIPIENT_READ.get() is None


def current_source_recipient():
    return _RECIPIENT_READ.get()
