-- Frozen 0c902b5a G0 statement: row-set oracle, not application SQL.

        WITH selected_sources AS (
        WITH RECURSIVE sources(kind, id) AS (
            SELECT value->>'source_kind', value->>'source_id'
            FROM jsonb_array_elements(CAST(:dependencies AS jsonb))
            UNION
            SELECT child->>'source_kind', child->>'source_id'
            FROM sources s
            LEFT JOIN content_input_receipts r ON s.kind IN ('workspace_artifact', 'explicit_memory')
                AND r.id = CASE WHEN s.id ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$'
                    THEN s.id::uuid END AND r.tenant_id = :tenant_id
            LEFT JOIN session_tool_invocations t ON s.kind = 'session_tool_invocation'
                AND t.id = CASE WHEN s.id ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$'
                    THEN s.id::uuid END AND t.tenant_id = :tenant_id
            LEFT JOIN runtime_result_objects o ON s.kind = 'runtime_result' AND o.tenant_id=:tenant_id
                AND o.id=CASE WHEN split_part(s.id,chr(10),3) ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$'
                    THEN split_part(s.id,chr(10),3)::uuid END
            LEFT JOIN runtime_result_integration_pages g ON s.kind='runtime_result_page' AND g.tenant_id=:tenant_id
                AND g.id=CASE WHEN s.id ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$' THEN s.id::uuid END
            LEFT JOIN invocation_spans q ON s.kind='runtime_team_context' AND q.tenant_id=:tenant_id
                AND q.id=CASE WHEN split_part(s.id,chr(10),3) ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$'
                    THEN split_part(s.id,chr(10),3)::uuid END
            LEFT JOIN session_model_results m ON m.tenant_id = :tenant_id AND (
                (s.kind IN ('session_model_result', '__model_request_inputs') AND m.id = CASE
                    WHEN s.id ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$' THEN s.id::uuid END)
                OR (m.session_id = t.session_id AND m.run_id = t.run_id
                    AND m.provider_request_id = t.provider_request_id)
                OR (s.kind='runtime_result' AND m.run_id=CASE
                    WHEN o.source_run_id ~* '^([a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12})$'
                    THEN o.source_run_id::uuid END))
            LEFT JOIN invocation_spans p ON p.tenant_id = :tenant_id AND p.runtime_task_id = t.run_id
                AND p.span_type IN ('workspace_output', 'workspace_input', 'memory_result')
                AND COALESCE(p.metadata_json #>> '{workspace_execution_evidence,invocation_id}',
                    p.metadata_json #>> '{workspace_input_evidence,invocation_id}',
                    p.metadata_json #>> '{memory_result,invocation_id}') = t.id::text
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(r.dependencies_json) = 'array' THEN r.dependencies_json ELSE '[]'::jsonb END
                || CASE WHEN s.kind IN ('session_tool_invocation', 'session_model_result', 'runtime_result') AND m.id IS NOT NULL
                    THEN jsonb_build_array(jsonb_build_object('source_kind', '__model_request_inputs', 'source_id', m.id::text))
                    ELSE '[]'::jsonb END
                || CASE WHEN s.kind = '__model_request_inputs'
                    AND jsonb_typeof(m.model_request_snapshot_json #> '{input_bindings,dependencies}') = 'array'
                    THEN m.model_request_snapshot_json #> '{input_bindings,dependencies}' ELSE '[]'::jsonb END
                || CASE WHEN jsonb_typeof(p.metadata_json #> '{workspace_execution_evidence,execution_inputs,sources}') = 'array'
                    THEN p.metadata_json #> '{workspace_execution_evidence,execution_inputs,sources}' ELSE '[]'::jsonb END
                || CASE WHEN jsonb_typeof(p.metadata_json #> '{memory_result,sources}') = 'array'
                    THEN p.metadata_json #> '{memory_result,sources}' ELSE '[]'::jsonb END
                || CASE WHEN p.metadata_json #>> '{workspace_input_evidence,content_input_receipt_id}' IS NOT NULL
                    THEN jsonb_build_array(jsonb_build_object('source_kind', COALESCE(
                        p.metadata_json #>> '{workspace_input_evidence,carrier_kind}', 'workspace_artifact'),
                        'source_id', p.metadata_json #>> '{workspace_input_evidence,content_input_receipt_id}'))
                    ELSE '[]'::jsonb END
                || CASE WHEN jsonb_typeof(q.metadata_json #> '{observation,sources}')='array'
                    THEN q.metadata_json #> '{observation,sources}' ELSE '[]'::jsonb END
                || COALESCE((SELECT jsonb_agg(jsonb_build_object('source_kind','runtime_result',
                    'source_id',g.parent_agent_id::text||chr(10)||g.parent_session_id::text||chr(10)
                        ||split_part(item->>'result_ref','/',3))) FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof(g.manifest_json->'items')='array'
                                THEN g.manifest_json->'items' ELSE '[]'::jsonb END) item),'[]'::jsonb)
            ) child
        ) SELECT kind, id FROM sources WHERE kind != '__model_request_inputs'
    ),
        results AS (
            SELECT o.id,o.tenant_id,o.source_kind,o.source_run_id,o.payload_schema,o.payload_bytes,o.sha256,o.size_bytes,o.media_type,o.encoding FROM runtime_result_objects o JOIN selected_sources s
                ON s.kind='runtime_result' AND split_part(s.id,chr(10),3)=o.id::text WHERE o.tenant_id=:tenant_id
        ), tools AS (
            SELECT t.id,t.tenant_id,t.session_id,t.run_id,t.round_id,t.provider_request_id,t.provider_tool_use_id,t.tool_name,t.provider_arguments_json,t.invocation_item_id,t.args_hash,t.effective_arguments_json,t.effective_args_hash,t.authority_snapshot_hash,t.effect_idempotency_key,t.effect_state,t.execution_fence_ref,t.receipt_ref,t.result_event_id,t.permission_item_id,t.permission_state,t.permission_request_version,t.permission_authority_snapshot_hash,t.permission_response_schema,t.permission_expires_at,t.permission_receipt_ref FROM session_tool_invocations t JOIN selected_sources s
                ON s.kind='session_tool_invocation' AND s.id=t.id::text
            WHERE t.tenant_id=:tenant_id AND t.result_event_id IS NOT NULL AND t.permission_state IS DISTINCT FROM 'waiting'
        ), models AS (
            SELECT m.id,m.tenant_id,m.session_id,m.run_id,m.round_id,m.turn_id,m.provider_request_id,m.model_request_hash,m.model_request_snapshot_json,m.state FROM session_model_results m WHERE m.tenant_id=:tenant_id
                AND m.state IN ('sealed','round_committed') AND (
                EXISTS(SELECT 1 FROM selected_sources s WHERE s.kind='session_model_result' AND s.id=m.id::text)
                OR EXISTS(SELECT 1 FROM results o WHERE o.source_run_id=m.run_id::text)
                OR EXISTS(SELECT 1 FROM tools t WHERE (t.session_id,t.run_id,t.provider_request_id)
                    =(m.session_id,m.run_id,m.provider_request_id)))
        ), events AS (
            SELECT e.id,e.tenant_id,e.agent_id,e.session_id,e.run_id,e.invocation_id,e.result_id,e.content_hash,e.item_kind,e.lifecycle,e.metadata_json,e.event_type,e.content
                FROM chat_transcript_events e JOIN tools t ON t.result_event_id=e.id
                WHERE e.tenant_id=:tenant_id
        ), spans AS (
            SELECT p.id,p.tenant_id,p.agent_id,p.user_id,p.runtime_task_id,p.session_id,p.trace_id,p.span_id,p.span_type,p.name,p.status,p.input_hash,p.metadata_json FROM invocation_spans p WHERE p.tenant_id=:tenant_id
                AND p.span_type NOT IN ('workspace_output','remote_action') AND (
                EXISTS(SELECT 1 FROM selected_sources s WHERE s.id=p.id::text)
                OR EXISTS(SELECT 1 FROM selected_sources s WHERE s.kind='runtime_team_context'
                    AND split_part(s.id,chr(10),3)=p.id::text)
                OR EXISTS(SELECT 1 FROM tools t WHERE p.runtime_task_id=t.run_id AND COALESCE(
                    p.metadata_json #>> '{workspace_input_evidence,invocation_id}',
                    p.metadata_json #>> '{memory_result,invocation_id}')=t.id::text)
                OR EXISTS(SELECT 1 FROM events e CROSS JOIN LATERAL
                    jsonb_each(CASE WHEN jsonb_typeof(e.metadata_json #> '{v2_payload,metadata}')='object'
                        THEN e.metadata_json #> '{v2_payload,metadata}' ELSE '{}'::jsonb END) pointer
                    WHERE split_part(pointer.value->>'evidence_ref',':',2)=p.id::text))
        )
        SELECT s.kind,s.id,NULL::jsonb AS fields FROM selected_sources s
        UNION ALL SELECT 'workspace_artifact',r.id::text,jsonb_build_object('id',r.id,'tenant_id',r.tenant_id,'carrier_kind',r.carrier_kind,'carrier_ref',r.carrier_ref,'revision',r.revision,'content_sha256',r.content_sha256,'dependencies_json',r.dependencies_json)
            FROM content_input_receipts r JOIN selected_sources s ON s.id=r.id::text
            AND s.kind='workspace_artifact' WHERE r.tenant_id=:tenant_id
        UNION ALL SELECT 'memory_receipt_binding',r.id::text,jsonb_build_object('id',r.id,'carrier_ref',r.carrier_ref)
            FROM content_input_receipts r JOIN selected_sources s ON s.id=r.id::text
            AND s.kind='explicit_memory' WHERE r.tenant_id=:tenant_id
        UNION ALL SELECT 'session_tool_invocation',t.id::text,jsonb_build_object('id',t.id,'tenant_id',t.tenant_id,'session_id',t.session_id,'run_id',t.run_id,'round_id',t.round_id,'provider_request_id',t.provider_request_id,'provider_tool_use_id',t.provider_tool_use_id,'tool_name',t.tool_name,'provider_arguments_json',t.provider_arguments_json,'invocation_item_id',t.invocation_item_id,'args_hash',t.args_hash,'effective_arguments_json',t.effective_arguments_json,'effective_args_hash',t.effective_args_hash,'authority_snapshot_hash',t.authority_snapshot_hash,'effect_idempotency_key',t.effect_idempotency_key,'effect_state',t.effect_state,'execution_fence_ref',t.execution_fence_ref,'receipt_ref',t.receipt_ref,'result_event_id',t.result_event_id,'permission_item_id',t.permission_item_id,'permission_state',t.permission_state,'permission_request_version',t.permission_request_version,'permission_authority_snapshot_hash',t.permission_authority_snapshot_hash,'permission_response_schema',t.permission_response_schema,'permission_expires_at',t.permission_expires_at,'permission_receipt_ref',t.permission_receipt_ref) FROM tools t
        UNION ALL SELECT 'session_command',c.id::text,jsonb_build_object('id',c.id,'tenant_id',c.tenant_id,'session_id',c.session_id,'namespace',c.namespace,'principal_type',c.principal_type,'principal_id',c.principal_id,'command_kind',c.command_kind,'request_json',c.request_json,'request_hash',c.request_hash,'target_json',c.target_json,'target_hash',c.target_hash)
            FROM session_commands c JOIN selected_sources s ON s.id=c.id::text
            AND s.kind='session_command' WHERE c.tenant_id=:tenant_id
        UNION ALL SELECT 'session_model_result',m.id::text,jsonb_build_object('id',m.id,'tenant_id',m.tenant_id,'session_id',m.session_id,'run_id',m.run_id,'round_id',m.round_id,'turn_id',m.turn_id,'provider_request_id',m.provider_request_id,'model_request_hash',m.model_request_hash,'model_request_snapshot_json',m.model_request_snapshot_json,'state',m.state) FROM models m
        UNION ALL SELECT 'event',e.id::text,jsonb_build_object('id',e.id,'tenant_id',e.tenant_id,'agent_id',e.agent_id,'session_id',e.session_id,'run_id',e.run_id,'invocation_id',e.invocation_id,'result_id',e.result_id,'content_hash',e.content_hash,'item_kind',e.item_kind,'lifecycle',e.lifecycle) ||
            jsonb_build_object('metadata_json',jsonb_build_object('v2_payload',e.metadata_json->'v2_payload')) FROM events e
        UNION ALL SELECT 'event_orm_projection',e.id::text,jsonb_build_object('id',e.id,'tenant_id',e.tenant_id,'agent_id',e.agent_id,'session_id',e.session_id,'run_id',e.run_id,'invocation_id',e.invocation_id,'result_id',e.result_id,'content_hash',e.content_hash,'item_kind',e.item_kind,'lifecycle',e.lifecycle) ||
            jsonb_build_object('metadata_json',jsonb_build_object('v2_payload',(CASE WHEN e.event_type IN ('tool_result', 'tool_result.completed') AND octet_length(e.content) > 1048576 THEN jsonb_set(e.metadata_json, '{v2_payload,content}', coalesce(to_jsonb((SELECT convert_from(substring(b FROM 1 FOR CASE WHEN octet_length(b) < 1048532 THEN octet_length(b) WHEN get_byte(b, 1048531) >= 192 THEN 1048531 WHEN get_byte(b, 1048530) >= 224 THEN 1048530 WHEN get_byte(b, 1048529) >= 240 THEN 1048529 ELSE 1048532 END), 'UTF8') || '

结果过大，只保留了前面部分。' FROM (SELECT substring(convert_to(left((e.metadata_json #>> '{v2_payload,content}'), 1048532), 'UTF8') FROM 1 FOR 1048532) AS b OFFSET 0) AS prefix)), 'null'::jsonb), false) ELSE e.metadata_json END)->'v2_payload')) FROM events e
            WHERE e.event_type IN ('tool_result','tool_result.completed') AND octet_length(e.content)>1048576
        UNION ALL SELECT 'span',p.id::text,jsonb_build_object('id',p.id,'tenant_id',p.tenant_id,'agent_id',p.agent_id,'user_id',p.user_id,'runtime_task_id',p.runtime_task_id,'session_id',p.session_id,'trace_id',p.trace_id,'span_id',p.span_id,'span_type',p.span_type,'name',p.name,'status',p.status,'input_hash',p.input_hash,'metadata_json',p.metadata_json) FROM spans p
        UNION ALL SELECT 'runtime_result',o.id::text,jsonb_build_object('id',o.id,'tenant_id',o.tenant_id,'source_kind',o.source_kind,'source_run_id',o.source_run_id,'payload_schema',o.payload_schema,'payload_bytes',o.payload_bytes,'sha256',o.sha256,'size_bytes',o.size_bytes,'media_type',o.media_type,'encoding',o.encoding) FROM results o
        UNION ALL SELECT 'runtime_result_page',g.id::text,jsonb_build_object('id',g.id,'tenant_id',g.tenant_id,'parent_agent_id',g.parent_agent_id,'parent_session_id',g.parent_session_id,'parent_user_id',g.parent_user_id,'root_runtime_task_id',g.root_runtime_task_id,'manifest_json',g.manifest_json,'manifest_sha256',g.manifest_sha256)
            FROM runtime_result_integration_pages g JOIN selected_sources s
                ON s.kind='runtime_result_page' AND s.id=g.id::text WHERE g.tenant_id=:tenant_id
