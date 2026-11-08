"""Empty bootstrap text is not session-derived resident knowledge."""

from uuid import uuid4

from app.memory.profile_plane import load_resident_memory
from app.services.agent_manager import EMPTY_SELF_CONTENT


def test_only_the_unfilled_self_scaffold_is_omitted_from_model_context(tmp_path):
    agent_id = uuid4()
    path = tmp_path / str(agent_id) / "memory" / "self" / "self.md"
    path.parent.mkdir(parents=True)
    path.write_text(EMPTY_SELF_CONTENT, encoding="utf-8")
    empty = load_resident_memory(agent_id=agent_id, data_root=tmp_path, include_overlay=False)
    assert not empty.source_refs and "我对自己的认识" not in empty.text

    content = EMPTY_SELF_CONTENT + "\n### Learned from a session\nCreate a report assistant.\n"
    path.write_text(content, encoding="utf-8")
    populated = load_resident_memory(agent_id=agent_id, data_root=tmp_path, include_overlay=False)
    assert "Create a report assistant." in populated.text
    assert populated.source_refs[0]["source_kind"] == "agent_resident_memory"
