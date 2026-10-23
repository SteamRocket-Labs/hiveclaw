"""The operator preflight cannot turn profile declarations into adoption proof."""

import json
from types import SimpleNamespace

import pytest

from app.scripts import probe_opensandbox_profile as probe
from app.scripts.probe_opensandbox_lifecycle import SDK_VERSION

IMAGE = "synthetic/workload@sha256:" + "a" * 64


def config(kind="kata", runtime="kata-runtime"):
    return {"runtime": {"type": "docker", "execd_image": IMAGE},
        "secure_runtime": {"type": kind, "docker_runtime": runtime}, "egress": {"image": IMAGE},
        "server": {"api_key": "DO_NOT_EXPORT_CONTROL_KEY"}, "database": {"url": "DO_NOT_EXPORT_DATABASE"}}


def test_ready_profile_still_requires_actual_conformance_and_adoption():
    result = probe.inspect_profile(config(), image=IMAGE, runtime_names=["kata-runtime", "runc"])
    assert result["status"] == "ready_for_conformance" and result["issues"] == []
    assert result["sdk_version"] == SDK_VERSION
    assert result["production_adoption"] == "unproven" and result["resource_effects"] == "none"
    assert result["next_action"] == "run_the_bounded_lifecycle_conformance_probe"
    assert "DO_NOT_EXPORT" not in json.dumps(result)


@pytest.mark.parametrize("kind,runtime,available,code", [
    ("", "runc", ["runc"], "strong_isolation_profile_required"),
    ("kata", "kata-runtime", ["runc"], "secure_runtime_unavailable"),
    ("kata", "", ["runc"], "docker_runtime_required"),
    ("gvisor", "runsc", ["runsc"], "probe_egress_runtime_incompatible"),
    ("kata", "kata-runtime", None, "docker_inspection_unavailable"),
])
def test_unsupported_or_unavailable_profile_keeps_a_reachable_action(kind, runtime, available, code):
    result = probe.inspect_profile(config(kind, runtime), image=IMAGE, runtime_names=available)
    assert result["status"] in {"unsupported", "unavailable"}
    assert code in {issue["code"] for issue in result["issues"]}
    assert result["next_action"] and result["production_adoption"] == "unproven"


@pytest.mark.parametrize("field", ["workload", "execd", "egress"])
def test_mutable_or_secret_bearing_image_input_is_not_exported_or_accepted(field):
    value = config()
    image = IMAGE
    unsafe = "registry.invalid/DO_NOT_EXPORT_IMAGE:latest"
    if field == "workload":
        image = unsafe
    else:
        value["runtime" if field == "execd" else "egress"]["execd_image" if field == "execd" else "image"] = unsafe
    result = probe.inspect_profile(value, image=image, runtime_names=["kata-runtime"])
    assert result["image_pins"][field] is None
    assert "immutable_images_required" in {issue["code"] for issue in result["issues"]}
    assert "DO_NOT_EXPORT" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("runtime", 7), ("secure_runtime", ["kata"]), ("secure_type", ["kata"])])
def test_invalid_toml_value_types_return_a_recoverable_receipt(field, value):
    data = config()
    if field == "secure_type":
        data["secure_runtime"]["type"] = value
    else:
        data[field] = value
    result = probe.inspect_profile(data, image=IMAGE, runtime_names=["kata-runtime"])
    assert result["status"] == "unavailable" and result["reason_code"] == "profile_config_invalid"
    assert result["next_action"] and result["resource_effects"] == "none"


def test_pinned_docker_probe_does_not_accept_a_firecracker_kubernetes_profile():
    result = probe.inspect_profile(config("firecracker", "kata-fc"), image=IMAGE, runtime_names=["kata-fc"])
    assert result["status"] == "unsupported"
    assert "docker_secure_runtime_unsupported" in {issue["code"] for issue in result["issues"]}
    assert result["production_adoption"] == "unproven"


def test_cli_only_reads_explicit_context_and_preserves_existing_evidence(tmp_path, monkeypatch, capsys):
    source = tmp_path / "server.toml"
    source.write_text(f'''[runtime]\ntype="docker"\nexecd_image="{IMAGE}"\n[egress]\nimage="{IMAGE}"\n[secure_runtime]\ntype="kata"\ndocker_runtime="kata-runtime"\n[server]\napi_key="DO_NOT_EXPORT_CONTROL_KEY"\n''')
    before = source.read_bytes()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] and kwargs["timeout"] == 10
        return SimpleNamespace(returncode=0, stdout='{"runc":{},"kata-runtime":{}}')

    monkeypatch.setattr(probe.subprocess, "run", run)
    output = tmp_path / "evidence"
    args = ["--config", str(source), "--docker-context", "explicit-context", "--image", IMAGE, "--output-dir", str(output)]
    assert probe.main(args) == 0
    assert calls == [["docker", "--context=explicit-context", "info", "--format", "{{json .Runtimes}}"]]
    assert source.read_bytes() == before
    assert "DO_NOT_EXPORT" not in (output / "report.json").read_text() + capsys.readouterr().out
    with pytest.raises(FileExistsError):
        probe.main(args)
    assert len(calls) == 1


def test_config_parse_failure_does_not_disclose_input_or_contact_docker(tmp_path, monkeypatch, capsys):
    source = tmp_path / "bad.toml"
    source.write_text('api_key = "DO_NOT_EXPORT_UNTERMINATED')
    monkeypatch.setattr(probe.subprocess, "run", lambda *_a, **_k: pytest.fail("Invalid input contacted Docker"))
    output = tmp_path / "evidence"
    assert probe.main(["--config", str(source), "--docker-context", "explicit", "--image", IMAGE,
        "--output-dir", str(output)]) == 1
    assert "DO_NOT_EXPORT" not in capsys.readouterr().out + (output / "report.json").read_text()
    assert json.loads((output / "report.json").read_text())["reason_code"] == "profile_config_unavailable"
