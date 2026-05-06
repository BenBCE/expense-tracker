"""Zip raw receipts with descriptive filenames."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from app.reports.types import ReportData

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str | None) -> str:
    if not value:
        return "unknown"
    return _SLUG_RE.sub("-", value).strip("-") or "unknown"


def build_zip(data: ReportData, out_path: Path) -> Path:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in data.rows:
            if not r.image_path:
                continue
            src = Path(r.image_path)
            if not src.exists():
                continue
            arc = f"{r.seq:03d}_{_slug(r.date)}_{_slug(r.category)}.jpg"
            zf.write(src, arcname=arc)
    return out_path


def bundle_all(paths: list[Path], out_path: Path) -> Path:
    """Build a single combined zip when individual deliverables exceed Telegram's limit."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            if p.exists():
                zf.write(p, arcname=p.name)
    return out_path
