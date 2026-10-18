#!/usr/bin/env python3
"""Create a private, non-overwriting profile for the existing application images."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import tempfile
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]


def configure(directory: Path, *, public_origin: str, port: int) -> Path:
    origin = urlsplit(public_origin)
    if (origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password
        or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("public-origin must be an HTTP(S) origin without credentials, path, query or fragment")
    if any(char.isspace() for char in public_origin):
        raise ValueError("public-origin must not contain whitespace")
    hostname = origin.hostname.encode("idna").decode("ascii").lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if origin.port is not None and origin.port != {"http": 80, "https": 443}[origin.scheme]:
        authority += f":{origin.port}"
    public_origin = f"{origin.scheme}://{authority}"
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    values = {"HIVE_INSTALL_ID": "hive_" + uuid.uuid4().hex[:20], "HIVE_INSTALL_SOURCE_ROOT": str(ROOT),
        "HIVE_INSTALL_FRONTEND_PORT": str(port),
        "HIVE_INSTALL_CORS_ORIGINS": json.dumps([public_origin.rstrip("/")]),
        **{key: secrets.token_hex(32) for key in ("HIVE_INSTALL_SCHEMA_PASSWORD", "HIVE_INSTALL_RUNTIME_PASSWORD",
            "HIVE_INSTALL_REDIS_PASSWORD", "HIVE_INSTALL_SECRET_KEY", "HIVE_INSTALL_JWT_SECRET_KEY", "HIVE_INSTALL_SECRETS_MASTER_KEY")}}
    if any(any(char in value for char in "\r\n\0") for value in values.values()):
        raise ValueError("configuration values must not contain line breaks or NUL")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "installation.env"
    # A hard link publishes a complete file atomically and never replaces an old profile.
    descriptor, temporary = tempfile.mkstemp(prefix=".installation-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key, value in values.items():
                quoted = value.replace("'", "\\'")
                handle.write(f"{key}='{quoted}'\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--port", type=int, default=3008)
    args = parser.parse_args()
    try:
        profile = configure(args.directory, public_origin=args.public_origin, port=args.port)
    except FileExistsError:
        print("Profile already exists; it was not replaced. Reuse installation.env to retain keys and data.")
        return 2
    except (ValueError, OSError) as exc:
        print(f"Profile was not initialized ({type(exc).__name__}). Check the origin, port and directory permissions.")
        return 2
    command = ["docker", "compose", "--env-file", str(profile), "-f", str(ROOT / "deploy/self-hosted/compose.yml")]
    print("Private installation profile created. Back up installation.env; it contains credential encryption keys.")
    print(shlex.join([*command, "config", "--quiet"]))
    print(shlex.join([*command, "up", "--build", "-d"]))
    print("Configuration and container startup do not prove readiness; use the company opening guide after signing in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
