from __future__ import annotations

from pathlib import Path

import pytest

from src.refinery import pdf_tools


def test_read_pdf_page_count_parses_pdfinfo_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        pdf_tools,
        "_run_command",
        lambda _args: "Title: demo\nPages:          67\nEncrypted:      no\n",
    )

    count = pdf_tools.read_pdf_page_count(tmp_path / "demo.pdf")

    assert count == 67


def test_read_pdf_text_pages_splits_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pdf_tools,
        "_run_command",
        lambda _args: "Page 1 text\n\fPage 2 text\n\f",
    )

    pages = pdf_tools.read_pdf_text_pages(tmp_path / "demo.pdf")

    assert pages == ["Page 1 text", "Page 2 text"]
