from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_SKILLS_DIR = REPO_ROOT / "backend" / "app" / "templates" / "skills"
OFFICE_PRODUCTIVITY_SKILL = (
    REPO_ROOT / "backend" / "packs" / "office_pack" / "skills" / "office-productivity" / "SKILL.md"
)


def test_single_purpose_office_template_skills_are_retired() -> None:
    retired = {
        "docx-generator",
        "xlsx-processor",
        "pptx-generator",
        "pdf-generator",
        "weekly-report-generator",
        "meeting-minutes",
        "pitch-deck-generator",
    }

    for folder_name in retired:
        assert not (APP_SKILLS_DIR / folder_name).exists(), folder_name


def test_office_productivity_pack_consolidates_document_spreadsheet_presentation_pdf_flows() -> None:
    skill = OFFICE_PRODUCTIVITY_SKILL.read_text(encoding="utf-8")

    assert "DOCX" in skill
    assert "XLSX" in skill
    assert "PPTX" in skill
    assert "PDF" in skill
    assert "meeting minutes" in skill
    assert "weekly reports" in skill
    assert "pitch decks" in skill
    assert "office_document_create" in skill
    assert "office_document_validate" in skill
    assert "send_channel_file" in skill
    assert "feishu_sheet_info" in skill
    assert "feishu_sheet_read" in skill


def test_office_productivity_pack_is_not_the_old_heavy_howto_dump() -> None:
    skill = OFFICE_PRODUCTIVITY_SKILL.read_text(encoding="utf-8")

    assert "### Basic creation" not in skill
    assert "### Adding charts with XlsxWriter" not in skill
    assert "Deep analysis with pandas" not in skill
    assert "### Shell orchestrator" not in skill
    assert "### Step-by-step with execute_code" not in skill
    assert "Accent color selection guidance" not in skill
    assert "## Overview" not in skill
    assert "### Step 2: Select Color Palette" not in skill
    assert "### Step 5: Adding Charts" not in skill
