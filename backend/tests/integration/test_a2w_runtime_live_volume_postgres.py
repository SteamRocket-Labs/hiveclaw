"""Measure live runtime-result queries before changing their history scans."""

import os
from pathlib import Path
import sys

import pytest

from tests.integration.test_a2w_source_graph_reads_postgres import (
    _run20_shape, source_graph, graph, native_workspace,  # noqa: F401
)
from tests.integration._a2w_runtime_live import measure_runtime_live
from tests.integration._a2w_volume import add_unrelated_history

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_runtime_live_history_plans(source_graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    n = source_graph
    roots, closure = await _run20_shape(n, owner_sessionmaker, app_user_sessionmaker, scale=1)
    directory = Path(os.environ["FULL_RUN"]) / "evidence" / f"{sys.argv[1]}-runtime" if os.environ.get(
        "FULL_RUN") else tmp_path / "runtime"
    for volume, added in ((0, 0), (1, 1), (5, 4)):
        if added:
            await add_unrelated_history(n, owner_sessionmaker, added)
        from tests.integration.test_a2w_source_graph_reads_postgres import _closure

        assert set(await _closure(n, owner_sessionmaker, roots)) == set(closure)
        await measure_runtime_live(n, app_user_sessionmaker, roots, monkeypatch, directory, scale=1, volume=volume)
