from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from app.services.office_document_service import OFFICE_PREVIEW_CSP, OfficeDocumentService
from app.services.officecli_adapter import OfficeCLIAdapter


def _write_contract_fixtures(root: Path) -> dict[str, Path]:
    docx_path = root / "contract.docx"
    document = Document()
    document.add_heading("OfficeCLI contract", level=1)
    document.add_paragraph("DOCX preview evidence")
    document.add_table(rows=1, cols=2).rows[0].cells[0].text = "table"
    document.save(docx_path)

    xlsx_path = root / "contract.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Evidence"
    sheet["A1"] = "OfficeCLI contract"
    sheet["B1"] = "=1+1"
    workbook.save(xlsx_path)

    pptx_path = root / "contract.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    textbox = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
    textbox.text_frame.text = "OfficeCLI contract PPTX preview evidence"
    presentation.save(pptx_path)

    return {"docx": docx_path, "xlsx": xlsx_path, "pptx": pptx_path}


def _valid_view_payload(payload: dict[str, Any]) -> bool:
    return payload.get("success") is True and isinstance(payload.get("data"), str) and bool(payload["data"].strip())


def verify_officecli_binary_contract(*, binary: str | Path) -> dict[str, Any]:
    """Exercise the deployed OfficeCLI binary through the exact Hive preview contract."""

    adapter = OfficeCLIAdapter(binary=str(binary), binary_sha256="", timeout_seconds=60)
    with tempfile.TemporaryDirectory(prefix="hive-officecli-contract-") as temp_dir:
        root = Path(temp_dir)
        fixtures = _write_contract_fixtures(root)
        service = OfficeDocumentService(root, adapter=adapter, preview_max_bytes=25 * 1024 * 1024)
        format_results: dict[str, dict[str, Any]] = {}
        for office_format, path in fixtures.items():
            html_payload = adapter.run_view(path, mode="html", cwd=root)
            text_payload = adapter.run_view(path, mode="text", cwd=root)
            preview = service.render_preview(path.name)
            format_results[office_format] = {
                "html": _valid_view_payload(html_payload),
                "text": _valid_view_payload(text_payload),
                "service_preview_mode": preview.preview_mode,
                "csp": OFFICE_PREVIEW_CSP in preview.html,
                "output_bytes": preview.output_bytes,
            }

    return {
        "status": "ok",
        "version": adapter.version(),
        "formats": format_results,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the deployed OfficeCLI HTML/text preview contract")
    parser.add_argument("--binary", default=shutil.which("officecli") or "officecli")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = verify_officecli_binary_contract(binary=args.binary)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
