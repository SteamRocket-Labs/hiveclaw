"""Company DingTalk SDK sessions have scoped ingress and an owned stop boundary."""

import asyncio
import logging
import multiprocessing
import os
import tempfile
import threading



def _sdk_process(app_key, app_secret, connection, home, lifeline):
    def owner_lost():
        try:
            lifeline.recv_bytes()
        except (EOFError, OSError):
            pass
        finally:
            # Vendor reconnect/callback loops can swallow ordinary exceptions.
            # The isolated transport must end even with no business traffic.
            os._exit(0)
    threading.Thread(target=owner_lost, name='company-dingtalk-owner', daemon=True).start()
    permitted = {'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
        'CURL_CA_BUNDLE', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'SYSTEMROOT',
        'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED'}
    transport_environment = {key: value for key, value in os.environ.items() if key in permitted}
    os.environ.clear()
    os.environ.update(transport_environment)
    os.environ['HOME'] = home
    os.chdir(home)
    # The SDK logs handshake tickets and provider payloads. This transport-only
    # process reports typed states over its private pipe instead of exporting
    # credential-bearing vendor diagnostics into application logs.
    logging.disable(logging.CRITICAL)
    try:
        import dingtalk_stream

        class Handler(dingtalk_stream.ChatbotHandler):
            def __init__(self):
                super().__init__()
                self.serial = asyncio.Lock()

            async def process(self, callback):
                async with self.serial:
                    connection.send({'kind': 'message', 'body': callback.data})
                    reply = await asyncio.to_thread(connection.recv)
                    return (dingtalk_stream.AckMessage.STATUS_OK if reply.get('accepted')
                        else dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION), 'accepted' if reply.get('accepted') else 'retry'

        client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(client_id=app_key, client_secret=app_secret))
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
        connection.send({'kind': 'state', 'state': 'starting'})
        client.start_forever()
    except BaseException as exc:
        try:
            connection.send({'kind': 'state', 'state': 'unavailable', 'error_class': type(exc).__name__})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


async def accept_company_dingtalk_message(*, tenant_id, config_id, revision, installation_ref, body):
    """Only a callback from the credential-authenticated SDK owns this entry."""
    import dingtalk_stream
    from app.api.tenant_channels import _config_revision
    from app.database import tenant_scoped_session
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.company_channel_identity import company_channel_ready, current_company_config
    from app.services.company_channel_runtime import accept_company_message

    async with tenant_scoped_session(tenant_id) as db:
        config = await current_company_config(db, tenant_id, 'dingtalk', lock=True)
        if (not company_channel_ready(config) or config.id != config_id or _config_revision(config) != revision
            or channel_installation_ref(config, fallback='') != installation_ref):
            raise PermissionError('company_dingtalk_stream_installation_changed')
        if not isinstance(body, dict):
            raise PermissionError('company_dingtalk_message_invalid')
        expected_corp = str((config.extra_config or {}).get('corp_id') or '')
        if (body.get('senderCorpId') != expected_corp or body.get('chatbotCorpId') != expected_corp
            or body.get('robotCode') != config.app_id or not body.get('senderStaffId')):
            raise PermissionError('company_dingtalk_sender_namespace_mismatch')
        if str(body.get('conversationType') or '') != '1':
            return {'routed': False, 'state': 'company_group_not_configured'}
        kind = body.get('msgtype')
        attachment = None
        if kind == 'file':
            content = body.get('content')
            if (not isinstance(content, dict) or not isinstance(content.get('downloadCode'), str)
                or not content['downloadCode'] or not isinstance(content.get('fileName'), str) or not content['fileName']):
                raise PermissionError('company_dingtalk_file_invalid')
            attachment = {'kind': 'file', 'filename': content['fileName'], 'resource_key': content['downloadCode']}
            text = ''
        elif kind != 'text':
            return {'routed': False, 'state': 'unsupported_message_type'}
        else:
            message = dingtalk_stream.ChatbotMessage.from_dict(body)
            text = ' '.join(message.get_text_list() or [])
        result = await accept_company_message(db, config=config, subject_id=body['senderStaffId'], text=text,
            event_id=str(body.get('msgId') or ''), attachment=attachment,
            delivery_target={'session_webhook': str(body.get('sessionWebhook') or ''),
                'conversation_id': str(body.get('conversationId') or '')})
        return result


class CompanyDingTalkStreamManager:
    def __init__(self, *, worker_target=_sdk_process):
        self.worker_target = worker_target
        self.workers = {}
        self.states = {}
        self.locks = {}
        self.tracked = {}
        self.discovery = None
        self.watch_task = None
        self.lease_engine = None
        self.stopping = False

    def start_reconciler(self, discovery=None):
        if discovery is not None:
            self.stopping = False
            self.discovery = discovery
        if self.watch_task is None or self.watch_task.done():
            self.watch_task = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.reconcile_once()
            except Exception as exc:
                # Configuration and credentials stay out of process diagnostics.
                self.states['discovery'] = {'state': 'unavailable', 'error_class': type(exc).__name__}

    async def reconcile_once(self):
        from app.api.tenant_channels import _config_revision
        from app.database import tenant_scoped_session
        from app.models.tenant_channel_config import TenantChannelConfig
        from app.models.tenant import Tenant
        from sqlalchemy import select
        if self.discovery is not None:
            for config in await self.discovery():
                self.tracked[config.id] = config.tenant_id
        for key, tenant_id in list(self.tracked.items()):
            async with tenant_scoped_session(tenant_id) as db:
                config = await db.scalar(select(TenantChannelConfig).join(Tenant, Tenant.id == TenantChannelConfig.tenant_id)
                    .where(TenantChannelConfig.id == key, Tenant.is_active.is_(True)))
            record = self.workers.get(key)
            if config is None:
                await self.stop_client(key)
            elif (record is None or record['revision'] != _config_revision(config)
                or not record['process'].is_alive() or record['task'].done() or not await self._ownership_current(record)):
                await self.reconcile(config)

    @staticmethod
    async def _ownership_current(record):
        from sqlalchemy import text
        async with record['lease_lock']:
            try:
                pid = await record['lease'].scalar(text('SELECT pg_backend_pid()'))
                await record['lease'].commit()
                return pid == record['lease_pid']
            except Exception:
                return False

    @staticmethod
    async def _release_ownership(record):
        from sqlalchemy import text
        async with record['lease_lock']:
            connection = record['lease']
            try:
                await connection.rollback()
                await connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key, 0))'),
                    {'key': record['lease_key']})
                await connection.commit()
            except Exception:
                await connection.invalidate()
            finally:
                await connection.close()

    async def reconcile(self, config):
        if self.stopping:
            return
        self.tracked[config.id] = config.tenant_id
        self.start_reconciler()
        async with self.locks.setdefault(config.id, asyncio.Lock()):
            await self._reconcile_locked(config)

    async def _reconcile_locked(self, config):
        from app.api.tenant_channels import _config_revision
        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.company_channel_identity import company_channel_ready
        from app.database import tenant_scoped_session
        from app.models.tenant import Tenant
        from app.models.tenant_channel_config import TenantChannelConfig
        from sqlalchemy import select
        key, tenant_id = config.id, config.tenant_id
        await self._stop_unlocked(key)
        async with tenant_scoped_session(tenant_id) as db:
            config = await db.scalar(select(TenantChannelConfig).join(Tenant, Tenant.id == TenantChannelConfig.tenant_id)
                .where(TenantChannelConfig.id == key, TenantChannelConfig.tenant_id == tenant_id, Tenant.is_active.is_(True)))
        if not company_channel_ready(config):
            self.states[key] = {'state': 'disabled'}
            return
        from app.models.channel_config import ChannelConfig
        async with tenant_scoped_session(tenant_id) as db:
            employee = await db.scalar(select(ChannelConfig.id).where(ChannelConfig.channel_type == 'dingtalk',
                ChannelConfig.app_id == config.app_id, ChannelConfig.is_configured.is_(True)))
        if employee is not None:
            self.states[key] = {'state': 'configuration_conflict'}
            return
        from sqlalchemy import text
        from app.database import engine, _engine_connect_args
        from app.config import get_settings
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool
        if self.lease_engine is None:
            # Session locks need dedicated connections; never exhaust the
            # request/runtime pool by reserving one pooled slot per bot.
            self.lease_engine = create_async_engine(engine.url, poolclass=NullPool,
                connect_args={**_engine_connect_args(get_settings()), 'timeout': get_settings().DB_POOL_TIMEOUT})
        lease = await self.lease_engine.connect()
        lease_key = f'company-dingtalk-stream:{config.app_id}'
        try:
            owned = await lease.scalar(text('SELECT pg_try_advisory_lock(hashtextextended(:key, 0))'), {'key': lease_key})
            lease_pid = await lease.scalar(text('SELECT pg_backend_pid()'))
            await lease.commit()
        except BaseException:
            await lease.invalidate()
            await lease.close()
            raise
        if not owned:
            await lease.close()
            self.states[key] = {'state': 'managed_elsewhere'}
            return
        ownership = {'lease': lease, 'lease_pid': lease_pid, 'lease_key': lease_key, 'lease_lock': asyncio.Lock()}
        context = multiprocessing.get_context('spawn')
        parent = child = lifeline = keeper = home = None
        try:
            parent, child = context.Pipe()
            lifeline, keeper = context.Pipe(duplex=False)
            home = tempfile.TemporaryDirectory(prefix='hive-company-dingtalk-')
            process = context.Process(target=self.worker_target,
                args=(config.app_id, config.app_secret, child, home.name, lifeline), daemon=True)
            process.start()
        except BaseException:
            for handle in (parent, child, lifeline, keeper):
                if handle is not None:
                    handle.close()
            if home is not None:
                home.cleanup()
            await self._release_ownership(ownership)
            raise
        child.close()
        lifeline.close()
        record = {'process': process, 'pipe': parent, 'lifeline': keeper, 'task': None,
            'revision': _config_revision(config), 'home': home, **ownership}
        self.workers[key] = record
        self.states[key] = {'state': 'starting'}
        record['task'] = asyncio.create_task(self._pump(key, config.tenant_id, record,
            channel_installation_ref(config, fallback='')))

    async def _pump(self, key, tenant_id, record, installation):
        connection = record['pipe']
        try:
            while True:
                data = await asyncio.to_thread(connection.recv)
                if data.get('kind') == 'state':
                    self.states[key] = {'state': str(data.get('state') or 'unavailable'),
                        'error_class': str(data.get('error_class') or '')}
                    continue
                if data.get('kind') != 'message':
                    raise PermissionError('company_dingtalk_sdk_protocol_invalid')
                try:
                    if not await self._ownership_current(record):
                        raise PermissionError('company_dingtalk_transport_ownership_lost')
                    result = await accept_company_dingtalk_message(tenant_id=tenant_id, config_id=key,
                        revision=record['revision'], installation_ref=installation, body=data.get('body'))
                    from datetime import datetime, timezone
                    self.states[key] = {'state': 'receiving', 'last_result': str(result.get('state') or 'accepted'),
                        'last_received_at': datetime.now(timezone.utc).isoformat()}
                    connection.send({'accepted': True})
                except Exception as exc:
                    self.states[key] = {'state': 'unavailable', 'error_class': type(exc).__name__}
                    connection.send({'accepted': False})
        except (EOFError, OSError):
            if self.workers.get(key) is record:
                self.states[key] = {'state': 'unavailable'}
        except Exception as exc:
            self.states[key] = {'state': 'unavailable', 'error_class': type(exc).__name__}
        finally:
            connection.close()

    async def stop_client(self, key):
        async with self.locks.setdefault(key, asyncio.Lock()):
            await self._stop_unlocked(key)
            self.tracked.pop(key, None)

    async def _stop_unlocked(self, key):
        record = self.workers.get(key)
        if record is None:
            return
        process = record['process']
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            raise RuntimeError('Company DingTalk transport did not stop')
        task = record['task']
        if task is not None:
            await task
        record['pipe'].close()
        record['lifeline'].close()
        record['home'].cleanup()
        await self._release_ownership(record)
        self.workers.pop(key, None)
        self.states[key] = {'state': 'stopped'}

    async def stop_all(self):
        self.stopping = True
        if self.watch_task is not None:
            self.watch_task.cancel()
            try:
                await self.watch_task
            except asyncio.CancelledError:
                pass
            self.watch_task = None
        for key in list(self.workers):
            await self.stop_client(key)
        self.tracked.clear()
        self.discovery = None
        if self.lease_engine is not None:
            await self.lease_engine.dispose()
            self.lease_engine = None

    def status(self):
        return {str(key): dict(value) for key, value in self.states.items()}


company_dingtalk_stream_manager = CompanyDingTalkStreamManager()


async def reconcile_company_dingtalk(tenant_id, config_id):
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.database import tenant_scoped_session
    async with tenant_scoped_session(tenant_id) as db:
        config = await db.get(TenantChannelConfig, config_id)
        if config is None:
            await company_dingtalk_stream_manager.stop_client(config_id)
        else:
            await company_dingtalk_stream_manager.reconcile(config)
