"""Controlled SDK receiver process; no model or production credentials."""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import socket

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as protocol
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn


async def serve(fd, directory, strict, input_required=False):
    listener = socket.socket(fileno=fd)
    directory = Path(directory)
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    card = ParseDict({'name': 'Controlled SDK context receiver', 'version': '1', 'capabilities': {},
        'supportedInterfaces': [{'url': origin + '/rpc', 'protocolBinding': 'JSONRPC', 'protocolVersion': '1.0'}],
        'defaultInputModes': ['text/plain', 'application/octet-stream'],
        'defaultOutputModes': ['text/plain']}, protocol.AgentCard())

    class Executor(AgentExecutor):
        async def execute(self, context, event_queue):
            text = context.get_user_input()
            if context.current_task is None:
                await event_queue.enqueue_event(protocol.Task(id=context.task_id, context_id=context.context_id,
                    status=protocol.TaskStatus(state=protocol.TASK_STATE_SUBMITTED), history=[context.message]))
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            # Consume only the ordinary SDK text interface, not a fixture's
            # alternate DataPart parser or a sender-side context injection.
            try:
                instruction, encoded = text.rsplit('\n', 1)
                record = json.loads(encoded)
                assert record['schema'] == 'hive.remote_chat_context.v1'
            except (ValueError, KeyError, AssertionError):
                (directory / 'missing-context.txt').write_text(text)
                await updater.failed(new_text_message('SDK text input has no complete context.'))
                return
            number = len(list(directory.glob('consumed-*.json')))
            files = []
            for binding in record.get('attachments', []):
                part = context.message.parts[binding['wire_part_index']]
                files.append({'input_id': binding['input_id'], 'part_index': binding['part_index'],
                    'filename': part.filename, 'media_type': part.media_type,
                    'sha256': hashlib.sha256(part.raw).hexdigest(),
                    'body': base64.b64encode(part.raw).decode()})
            captured = {'sdk_input': text, 'context': record, 'task_id': context.task_id,
                'context_id': context.context_id, 'pid': __import__('os').getpid(),
                'message': MessageToDict(context.message), 'files': files}
            (directory / f'consumed-{number}.json').write_text(json.dumps(captured, ensure_ascii=False))
            report = json.dumps({'instruction': instruction, 'context_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
                'soul': record['resources']['soul'], 'messages': record['messages'], 'files': files}, ensure_ascii=False)
            (directory / f'report-{number}.txt').write_text(report)
            if input_required and number == 0:
                await updater.requires_input(new_text_message('Please provide the requested detail in this original work.'))
                return
            await updater.add_artifact([protocol.Part(text=report, filename='sdk-report.txt')], name='sdk-report.txt')
            await updater.complete(new_text_message(report))

        async def cancel(self, context, event_queue):
            await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    handler = DefaultRequestHandler(Executor(), InMemoryTaskStore(), card, validate_input_modes=strict)
    @asynccontextmanager
    async def lifespan(_app):
        yield
        await handler.aclose()
    async def agent_card(_request):
        return JSONResponse(MessageToDict(card))
    app = Starlette(routes=[Route('/card', agent_card), *create_jsonrpc_routes(handler, '/rpc')], lifespan=lifespan)
    async def authenticate(request, call_next):
        if request.headers.get('authorization') != 'Bearer synthetic-remote-credential':
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return await call_next(request)
    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError('SDK receiver did not start')
        await asyncio.sleep(.01)
    print(json.dumps({'ready': True, 'pid': __import__('os').getpid()}), flush=True)
    await task


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--input-required', action='store_true')
    args = parser.parse_args()
    asyncio.run(serve(args.fd, args.directory, args.strict, args.input_required))
