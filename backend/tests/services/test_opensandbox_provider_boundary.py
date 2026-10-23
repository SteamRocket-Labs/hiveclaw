"""Concrete profile and archive boundaries for the opt-in compute candidate."""

import hashlib
import io
import json
import tarfile

import pytest

from app.services.code_execution.opensandbox_provider import _outputs, load_profile
from app.services.execution_environments import EnvironmentError


def test_report_verdict_does_not_approve_a_compute_profile(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"status": "passed", "production_adoption": "approved"}))
    monkeypatch.setenv("HIVE_OPENSANDBOX_PROFILE_PATH", str(path))
    monkeypatch.delenv("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256", raising=False)
    with pytest.raises(EnvironmentError, match="compute_profile_not_approved"):
        load_profile()
    monkeypatch.setenv("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256", "0" * 64)
    with pytest.raises(EnvironmentError, match="compute_profile_changed"):
        load_profile()
    monkeypatch.setenv("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(EnvironmentError, match="compute_profile_unavailable"):
        load_profile()


@pytest.mark.parametrize("names", [["../escape"], ["/absolute"], ["file", "file/child"], ["same", "./same"]])
def test_archive_is_validated_before_any_staged_file_mutation(names):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in names:
            entry = tarfile.TarInfo(name)
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(EnvironmentError, match="compute_output_archive_invalid"):
        _outputs(buffer.getvalue())


def test_regular_skill_handoff_preserves_exact_bytes():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo(".agents/skills/candidate/SKILL.md")
        entry.size, entry.mode = 4, 0o644
        archive.addfile(entry, io.BytesIO(b"test"))
    assert _outputs(buffer.getvalue()) == {".agents/skills/candidate/SKILL.md": (b"test", 0o644)}
