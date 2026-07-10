from pathlib import Path


def test_generated_typescript_thread_item_contract_is_current() -> None:
    from app.scripts.export_thread_items import render_typescript_contract

    generated_path = (
        Path(__file__).resolve().parents[3] / "frontend" / "src" / "api" / "domains" / "threadItems.generated.ts"
    )

    assert generated_path.read_text(encoding="utf-8") == render_typescript_contract()
