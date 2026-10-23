"""Read-only readiness for the pinned Docker lifecycle conformance workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib

SDK_VERSION = "1.1.1"
_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


def docker_runtime_names(context: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ["docker", f"--context={context}", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        runtimes = json.loads(result.stdout) if result.returncode == 0 else None
        return sorted(runtimes) if isinstance(runtimes, dict) else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def inspect_profile(config: dict, *, image: str, runtime_names: list[str] | None) -> dict:
    """Evaluate trusted configuration facts without adopting a runtime."""
    runtime = config.get("runtime", {})
    secure = config.get("secure_runtime", {})
    egress = config.get("egress", {})
    if not all(isinstance(value, dict) for value in (runtime, secure, egress)):
        return unavailable("profile_config_invalid")
    images = {"workload": image, "execd": runtime.get("execd_image"), "egress": egress.get("image")}
    pins = {key: value if isinstance(value, str) and _IMAGE.fullmatch(value) else None
        for key, value in images.items()}
    kind = secure.get("type", "")
    if not isinstance(kind, str):
        return unavailable("profile_config_invalid")
    backend = runtime.get("type")
    selected = secure.get("docker_runtime")
    issues = []

    def issue(code, action, status="unsupported"):
        issues.append({"code": code, "next_action": action, "status": status})

    if backend != "docker":
        issue("docker_profile_required", "use_the_probe_for_the_selected_backend")
    if any(value is None for value in pins.values()):
        issue("immutable_images_required", "pin_workload_execd_and_egress_images")
    if kind not in {"gvisor", "kata", "firecracker"}:
        issue("strong_isolation_profile_required", "select_a_verified_secure_runtime_profile")
    elif not isinstance(selected, str) or not selected:
        issue("docker_runtime_required", "configure_the_selected_secure_runtime")
    elif runtime_names is not None and selected not in runtime_names:
        issue("secure_runtime_unavailable", "prepare_the_selected_runtime_on_the_approved_host", "unavailable")
    if kind == "gvisor":
        # This pinned lifecycle probe always requests networkPolicy. Do not
        # remove default-deny to bypass the server's documented incompatibility.
        issue("probe_egress_runtime_incompatible", "select_a_profile_supporting_the_required_default_deny_policy")
    if kind == "firecracker":
        issue("docker_secure_runtime_unsupported", "select_a_supported_docker_profile_or_the_kubernetes_workflow")
    if runtime_names is None:
        issue("docker_inspection_unavailable", "check_the_explicit_docker_context", "unavailable")
    return {
        "schema": "hive.opensandbox.profile-preflight.v1",
        "sdk_version": SDK_VERSION,
        "runtime_backend": "docker" if backend == "docker" else None,
        "secure_runtime": kind if kind in {"gvisor", "kata", "firecracker"} else None,
        "configured_runtime_available": isinstance(selected, str) and runtime_names is not None and selected in runtime_names,
        "image_pins": pins,
        "issues": issues,
        "status": "unavailable" if any(item["status"] == "unavailable" for item in issues)
        else "unsupported" if issues else "ready_for_conformance",
        "next_action": issues[0]["next_action"] if issues else "run_the_bounded_lifecycle_conformance_probe",
        "production_adoption": "unproven",
        "resource_effects": "none",
    }


def unavailable(code: str) -> dict:
    return {"schema": "hive.opensandbox.profile-preflight.v1", "status": "unavailable", "reason_code": code,
        "next_action": "repair_the_explicit_profile_input", "production_adoption": "unproven", "resource_effects": "none"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Trusted server TOML; never modified or exported.")
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--image", required=True, help="Workload image pinned by digest.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New evidence directory; never overwritten.")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        config = tomllib.loads(args.config.read_text())
    except (OSError, UnicodeError, ValueError):
        report = unavailable("profile_config_unavailable")
    else:
        report = inspect_profile(config, image=args.image, runtime_names=docker_runtime_names(args.docker_context))
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ready_for_conformance" else 1


if __name__ == "__main__":
    raise SystemExit(main())
