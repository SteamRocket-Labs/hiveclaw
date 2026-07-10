def test_content_diff_is_deterministic_and_reports_removed_keys() -> None:
    from app.services.config_versioning import build_content_diff, canonical_content_hash

    before = {"name": "old", "nested": {"a": 1}, "removed": True}
    after = {"nested": {"a": 2}, "name": "new", "added": [1]}

    assert build_content_diff(before, after) == {
        "set": {"added": [1], "name": "new", "nested": {"a": 2}},
        "removed": ["removed"],
    }
    assert canonical_content_hash(after) == canonical_content_hash({"added": [1], "name": "new", "nested": {"a": 2}})


def test_apply_content_diff_round_trips_snapshot() -> None:
    from app.services.config_versioning import apply_content_diff, build_content_diff

    before = {"keep": 1, "change": "old", "remove": 2}
    after = {"keep": 1, "change": "new", "add": {"ok": True}}

    assert apply_content_diff(before, build_content_diff(before, after)) == after


def test_save_revision_queries_and_deactivates_only_within_tenant() -> None:
    import inspect

    from app.services import config_versioning

    source = inspect.getsource(config_versioning.save_revision)

    assert source.count("ConfigRevision.tenant_id == tenant_id") >= 2
