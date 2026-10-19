"""Spawn-importable network fixture for the owned company transport process."""

def controlled_sdk_worker(messages, finished, app_key, app_secret, connection, home, lifeline):
    """Replace only the network SDK in the child; retain the actual pipe boundary."""
    import asyncio
    import json
    import os
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from app.services.company_dingtalk_stream import _sdk_process
    class Client:
        def __init__(self, _credential):
            self.handler = None
        def register_callback_handler(self, _topic, handler):
            self.handler = handler
        def start_forever(self):
            assert not any(key in os.environ for key in ('DATABASE_URL', 'SECRET_KEY', 'SECRETS_MASTER_KEY'))
            assert os.environ['HOME'] == home and os.getcwd() == home
            async def callbacks():
                replies = [await self.handler.process(SimpleNamespace(data=body)) for body in messages]
                Path(home, 'acks.json').write_text(json.dumps(replies))
                finished.set()
                await asyncio.Event().wait()
            asyncio.run(callbacks())
    sys.modules['dingtalk_stream'] = SimpleNamespace(DingTalkStreamClient=Client, Credential=lambda **kw: kw,
        ChatbotHandler=object, ChatbotMessage=SimpleNamespace(TOPIC='/v1.0/im/bot/messages/get'),
        AckMessage=SimpleNamespace(STATUS_OK=200, STATUS_SYSTEM_EXCEPTION=500))
    _sdk_process(app_key, app_secret, connection, home, lifeline)



def controlled_owner(database_url, tenant_id, config_id, messages, finished, reports):
    """Own the real manager in a disposable parent that the regression kills."""
    import asyncio
    import os
    from functools import partial
    from multiprocessing.reduction import DupFd
    os.environ['DATABASE_URL'] = database_url
    os.environ['SCHEMA_DATABASE_URL'] = database_url
    from app.services.secrets_provider import init_secrets_provider
    init_secrets_provider(os.environ['SECRETS_MASTER_KEY'])
    from app.database import engine, tenant_scoped_session
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.services.company_dingtalk_stream import CompanyDingTalkStreamManager
    async def own():
        manager = CompanyDingTalkStreamManager(worker_target=partial(controlled_sdk_worker, messages, finished))
        try:
            async with tenant_scoped_session(tenant_id) as db:
                config = await db.get(TenantChannelConfig, config_id)
            await manager.reconcile(config)
            record = manager.workers[config_id]
            reports.send({'child_pid': record['process'].pid, 'home': record['home'].name,
                'sentinel': DupFd(record['process'].sentinel)})
            await asyncio.Event().wait()
        except BaseException as exc:
            reports.send({'error_class': type(exc).__name__})
            raise
        finally:
            await manager.stop_all()
            await engine.dispose()
    asyncio.run(own())
