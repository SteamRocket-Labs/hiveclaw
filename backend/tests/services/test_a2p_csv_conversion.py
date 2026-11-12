"""CSV preserves its Markdown table while avoiding unrelated format classifiers."""

import pytest

from app.services.document_conversion import DocumentConversionRequest, DocumentConversionService


@pytest.mark.parametrize("data, expected", [
    (b"region,sales\nWest,42\n", "| region | sales |\n| --- | --- |\n| West | 42 |"),
    (b'name,value\n"West, Coast",42\nshort\nextra,2,ignored\n',
        "| name | value |\n| --- | --- |\n| West, Coast | 42 |\n| short |  |\n| extra | 2 |"),
    (b'name,note\nA,"first\nsecond"\n', "| name | note |\n| --- | --- |\n| A | first\nsecond |"),
    ("\ufeff地区,销量\n华东,42\n".encode(), "| 地区 | 销量 |\n| --- | --- |\n| 华东 | 42 |"),
])
def test_csv_uses_native_table_conversion(monkeypatch, tmp_path, data, expected):
    source = tmp_path / "sales.csv"
    source.write_bytes(data)
    called = []
    original = DocumentConversionService._convert_with_markitdown

    def observe(self, *args):
        called.append(True)
        return original(self, *args)

    monkeypatch.setattr(DocumentConversionService, "_convert_with_markitdown", observe)
    result = DocumentConversionService().convert(DocumentConversionRequest(
        source_path=source, workspace_root=tmp_path, source_uri=None,
        tenant_id=None, agent_id=None, user_id=None))
    assert result.markdown == expected
    assert not called
    assert result.engine == "legacy_python_extractors"
    assert (tmp_path / result.artifact_markdown_path).read_text() == expected


def test_csv_filename_does_not_override_pdf_bytes(monkeypatch, tmp_path):
    source = tmp_path / "disguised.csv"
    source.write_bytes(b"%PDF-1.4\nDocument body")
    called = []

    def pdf(self, *args):
        called.append(True)
        return "# Extracted PDF"

    monkeypatch.setattr(DocumentConversionService, "_convert_with_markitdown", pdf)
    result = DocumentConversionService().convert(DocumentConversionRequest(
        source_path=source, workspace_root=tmp_path, source_uri=None,
        tenant_id=None, agent_id=None, user_id=None))
    assert called == [True] and result.markdown == "# Extracted PDF"
