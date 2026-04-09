from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / "backend" / "app" / "templates" / "skills"


def test_xlsx_processor_is_cloud_first_thin_skill() -> None:
    skill = (SKILLS_DIR / "xlsx-processor" / "SKILL.md").read_text(encoding="utf-8")

    assert "feishu_sheet_info" in skill
    assert "feishu_sheet_read" in skill
    assert "run_command" in skill or "execute_code" in skill
    assert "Success Criteria" in skill
    assert "Fallbacks" in skill
    assert "### Basic creation" not in skill
    assert "### Adding charts with XlsxWriter" not in skill
    assert "Deep analysis with pandas" not in skill


def test_docx_generator_is_cloud_first_thin_skill() -> None:
    skill = (SKILLS_DIR / "docx-generator" / "SKILL.md").read_text(encoding="utf-8")

    assert "execute_code" in skill
    assert "write_file" in skill or "send_channel_file" in skill
    assert "Success Criteria" in skill
    assert "Fallbacks" in skill
    assert "### Step 1: Create document with page setup" not in skill
    assert "### Step 2: Define styles" not in skill
    assert "### Step 4: Add tables" not in skill


def test_pdf_generator_is_cloud_first_thin_skill() -> None:
    skill = (SKILLS_DIR / "pdf-generator" / "SKILL.md").read_text(encoding="utf-8")

    assert "execute_code" in skill
    assert "send_channel_file" in skill
    assert "Success Criteria" in skill
    assert "Fallbacks" in skill
    assert "### Shell orchestrator" not in skill
    assert "### Step-by-step with execute_code" not in skill
    assert "Accent color selection guidance" not in skill


def test_pptx_generator_is_cloud_first_thin_skill() -> None:
    skill = (SKILLS_DIR / "pptx-generator" / "SKILL.md").read_text(encoding="utf-8")

    assert "execute_code" in skill
    assert "send_channel_file" in skill
    assert "Success Criteria" in skill
    assert "Fallbacks" in skill
    assert "## Overview" not in skill
    assert "### Step 2: Select Color Palette" not in skill
    assert "### Step 5: Adding Charts" not in skill
