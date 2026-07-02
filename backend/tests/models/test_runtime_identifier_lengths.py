from __future__ import annotations


def test_runtime_task_subagent_identifiers_accept_nested_session_keys():
    from app.models.runtime_task import RuntimeTask

    parent_session_id = "subagent-subagent:daffd7b8-4279-4786-82df-4c2186a92053:704d194ced984b599b3e965294cab66d-iec-ael-research-d2"
    trace_id = f"subagent:{parent_session_id}:64a5d1e8e2f94f5cb5f27e4c9e6f1a90"

    assert RuntimeTask.__table__.c.parent_session_id.type.length >= len(parent_session_id)
    assert RuntimeTask.__table__.c.child_session_id.type.length >= len(parent_session_id)
    assert RuntimeTask.__table__.c.trace_id.type.length >= len(trace_id)


def test_coordination_signal_identifiers_accept_subagent_threads():
    from app.models.coordination import CoordinationSignal

    thread_id = "subagent:152384d6-1163-4155-87f0-47c9b8f29eb9:da6e23ba514c484396917608e4ddb54b"
    from_agent_id = "subagent:drone-search-3"

    assert CoordinationSignal.__table__.c.thread_id.type.length >= len(thread_id)
    assert CoordinationSignal.__table__.c.from_agent_id.type.length >= len(from_agent_id)
