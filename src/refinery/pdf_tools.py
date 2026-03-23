from __future__ import annotations

import subprocess
from pathlib import Path


def read_pdf_page_count(path: Path) -> int | None:
    result = _run_command(["pdfinfo", str(path)])
    if result is None:
        return None

    for line in result.splitlines():
        if not line.startswith("Pages:"):
            continue
        _, _, value = line.partition(":")
        value = value.strip()
        if value.isdigit():
            return int(value)
    return None


def read_pdf_text_pages(
    path: Path,
    *,
    first_page: int | None = None,
    last_page: int | None = None,
) -> list[str]:
    args = ["pdftotext", "-layout", "-enc", "UTF-8"]
    if first_page is not None:
        args.extend(["-f", str(first_page)])
    if last_page is not None:
        args.extend(["-l", str(last_page)])
    args.extend([str(path), "-"])

    result = _run_command(args)
    if result is None:
        return []

    pages = []
    for page in result.replace("\r\n", "\n").split("\f"):
        normalized = page.strip()
        if normalized:
            pages.append(normalized)
    return pages


def _run_command(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    return completed.stdout
