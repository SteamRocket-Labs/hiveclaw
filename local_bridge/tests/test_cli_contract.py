from __future__ import annotations

import pytest

from hive_bridge.cli import build_parser


def test_python_cli_p0_surface_does_not_expose_mcp(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])

    output = capsys.readouterr().out
    assert "login" in output
    assert "status" in output
    assert "upload" in output
    assert "run" in output
    assert "mcp" not in output.lower()
