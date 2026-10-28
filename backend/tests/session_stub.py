"""Session mechanics for scripted unit-test databases, never RLS evidence."""

from types import SimpleNamespace


class ActiveTransactionSessionStub:
    """Expose checkout and session info while leaving SQL responses to the test.

    These doubles model an existing transaction, so checkout does not begin a
    new one or fire after_begin. Tenant pinning still executes its explicit SET
    through the double's unchanged execute method. Fresh-transaction hooks and
    isolation remain covered by the real PostgreSQL tests.
    """

    @property
    def sync_session(self):
        if not hasattr(self, "_stub_sync_session"):
            self._stub_sync_session = SimpleNamespace(info={})
        return self._stub_sync_session

    @sync_session.setter
    def sync_session(self, value):
        self._stub_sync_session = value

    async def connection(self):
        return self
