"""Machine-authenticated A2A task transport, separate from human management APIs."""
import posixpath
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import NoMatchFound

from app.database import get_db
from app.services.a2a_machine_tasks import A2AError, MACHINE_TASK_METHOD_SCOPES, cancel_machine_task, get_machine_artifact, get_machine_task, send_machine_message, wait_for_machine_task
from app.services.machine_identity_service import authenticate_machine

router = APIRouter()

_UNSUPPORTED_METHODS = {
    'SendStreamingMessage': ('agent.execute', -32004),
    'SubscribeToTask': ('task.read', -32004),
    'GetExtendedAgentCard': ('agent.describe', -32004),
    'CreateTaskPushNotificationConfig': ('agent.execute', -32003),
    'GetTaskPushNotificationConfig': ('task.read', -32003),
    'ListTaskPushNotificationConfigs': ('task.read', -32003),
    'DeleteTaskPushNotificationConfig': ('agent.execute', -32003),
}


def machine_route_url(request: Request, name: str, **params):
    """Keep links on the same router alias when the app mounts both API versions."""
    current = request.url.path.removeprefix(request.scope.get('root_path', ''))
    paths = []
    for route in request.app.routes:
        try:
            paths.append(route.url_path_for(name, **params))
        except NoMatchFound:
            continue
    if not paths:
        raise NoMatchFound(name, params)
    path = max(paths, key=lambda candidate: len(posixpath.commonpath((current, str(candidate))).split('/')))
    return path.make_absolute_url(base_url=request.base_url)


@router.get('/{agent_id}/a2a/.well-known/agent-card.json')
async def machine_agent_card(agent_id: uuid.UUID, request: Request,
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    from app.services.interoperability import build_machine_agent_card

    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != 'bearer' or not token:
        raise HTTPException(401, detail='Machine credential required', headers={'WWW-Authenticate': 'Bearer'})
    _context, agent = await authenticate_machine(db, token=token, agent_id=agent_id, scope='agent.describe')
    card = build_machine_agent_card(agent, rpc_url=str(machine_route_url(request, 'machine_task_rpc', agent_id=agent_id)))
    await db.commit()
    return JSONResponse(card, headers={'Cache-Control': 'private, no-store', 'A2A-Version': '1.0'})


@router.post('/{agent_id}/a2a/rpc')
async def machine_task_rpc(agent_id: uuid.UUID, request: Request,
    authorization: str | None = Header(default=None), a2a_version: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db)):
    request_id = None
    try:
        try:
            payload = await request.json()
        except ValueError:
            raise A2AError(-32700, 'Invalid JSON payload') from None
        if not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0':
            raise A2AError(-32600, 'Request payload validation error')
        request_id = payload.get('id')
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            raise A2AError(-32600, 'Request payload validation error')
        method = payload.get('method')
        if not isinstance(method, str) or method not in MACHINE_TASK_METHOD_SCOPES.keys() | _UNSUPPORTED_METHODS.keys():
            raise A2AError(-32601, 'Method not found')
        scheme, token = get_authorization_scheme_param(authorization)
        if scheme.lower() != 'bearer' or not token:
            raise HTTPException(401, detail='Machine credential required', headers={'WWW-Authenticate': 'Bearer'})
        scope = MACHINE_TASK_METHOD_SCOPES.get(method) or _UNSUPPORTED_METHODS[method][0]
        context, agent = await authenticate_machine(db, token=token, agent_id=agent_id, scope=scope)
        if a2a_version != '1.0':
            raise A2AError(-32009, 'Version not supported')
        if method in _UNSUPPORTED_METHODS:
            code = _UNSUPPORTED_METHODS[method][1]
            raise A2AError(code, 'Push notifications are not supported' if code == -32003 else 'This operation is not supported')
        params = payload.get('params', {})
        if not isinstance(params, dict):
            raise A2AError(-32602, 'Invalid parameters')
        config = params.get('configuration') or {}
        if not isinstance(config, dict):
            raise A2AError(-32602, 'Invalid parameters')
        history_length = config.get('historyLength') if method == 'SendMessage' else params.get('historyLength')
        if history_length is not None and (type(history_length) is not int or history_length < 0):
            raise A2AError(-32602, 'Invalid history length')
        if method == 'SendMessage':
            result = await send_machine_message(db, context, agent, params, bearer_secret=token)
            if config.get('returnImmediately') is not True:
                result = {'task': await wait_for_machine_task(db, context, result['task'], history_length=history_length)}
        elif method == 'GetTask':
            result = await get_machine_task(db, context, params.get('id'), history_length=history_length)
        else:
            result = await cancel_machine_task(db, context, agent, params.get('id'))
        task = result.get('task', result)
        for artifact in task.get('artifacts', []):
            for part in artifact.get('parts', []):
                if 'url' in part:
                    part['url'] = str(machine_route_url(request, 'machine_task_artifact', agent_id=agent_id,
                        task_id=task['id'], artifact_id=artifact['artifactId']))
        await db.commit()
        return JSONResponse({'jsonrpc': '2.0', 'id': request_id, 'result': result}, headers={'Cache-Control': 'no-store', 'A2A-Version': '1.0'})
    except A2AError as exc:
        await db.rollback()
        return JSONResponse({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': exc.code, 'message': exc.message,
            **({'data': exc.data} if exc.data is not None else {})}},
            headers={'Cache-Control': 'no-store', 'A2A-Version': '1.0'})


@router.get('/{agent_id}/a2a/tasks/{task_id}/artifacts/{artifact_id}')
async def machine_task_artifact(agent_id: uuid.UUID, task_id: uuid.UUID, artifact_id: uuid.UUID,
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    from app.api.files import _agent_base_dir, _workspace_authority_http_error, _workspace_download_response
    from app.services.chat_artifact_delivery import resolve_chat_artifact_file
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != 'bearer' or not token:
        raise HTTPException(401, detail='Machine credential required', headers={'WWW-Authenticate': 'Bearer'})
    context, _agent = await authenticate_machine(db, token=token, agent_id=agent_id, scope='task.read')
    try:
        artifact = await get_machine_artifact(db, context, task_id, artifact_id)
        captured, source = resolve_chat_artifact_file(artifact, _agent_base_dir(agent_id))
        if captured is None or source != 'delivery_snapshot':
            raise HTTPException(404, detail='Captured task artifact not found')
        snapshot = artifact.snapshot_json or {}
        if not snapshot.get('content_hash'):
            raise HTTPException(409, detail='Captured task artifact hash is unavailable')
        response = await _workspace_download_response(agent_id, snapshot['snapshot_storage_path'],
            artifact.name, expected_sha256=snapshot['content_hash'])
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    except A2AError as exc:
        raise HTTPException(404 if exc.code == -32001 else 409, detail=exc.message) from exc
    except ContentInputReceiptError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': str(exc)}) from exc
    except WorkspaceAuthorityError as exc:
        raise _workspace_authority_http_error(exc) from exc
