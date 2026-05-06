"""PDF report generator using reportlab."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.reports.types import ReportData

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


def _format_amount(amount: Decimal | None) -> str:
    if amount is None:
        return "—"
    return f"{amount:,.2f}"


def _draw_header(c: canvas.Canvas, data: ReportData) -> None:
    c.setFont("Helvetica-Bold", 18)
    c.drawString(MARGIN, PAGE_H - MARGIN, f"Trip: {data.trip_name}")
    c.setFont("Helvetica", 10)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    c.drawString(MARGIN, PAGE_H - MARGIN - 14, f"Generated: {generated}")
    c.drawString(MARGIN, PAGE_H - MARGIN - 28, f"Receipts: {len(data.rows)}")


def _draw_summary_table(c: canvas.Canvas, data: ReportData, top_y: float) -> float:
    by_cat: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for r in data.rows:
        if r.total is None:
            continue
        by_cat[(r.category or "other", r.currency or "")].append(Decimal(str(r.total)))

    headers = ["Category", "Currency", "Total", "Count"]
    col_x = [MARGIN, MARGIN + 60 * mm, MARGIN + 95 * mm, MARGIN + 140 * mm]

    y = top_y
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.HexColor("#305496"))
    c.rect(MARGIN, y - 4, PAGE_W - 2 * MARGIN, 16, fill=1, stroke=0)
    c.setFillColor(colors.white)
    for i, h in enumerate(headers):
        c.drawString(col_x[i], y, h)
    c.setFillColor(colors.black)
    y -= 18

    c.setFont("Helvetica", 10)
    grand_by_currency: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for (cat, cur), totals in sorted(by_cat.items()):
        cat_total = sum(totals, Decimal("0"))
        grand_by_currency[cur] += cat_total
        c.drawString(col_x[0], y, cat)
        c.drawString(col_x[1], y, cur or "—")
        c.drawRightString(col_x[2] + 30 * mm, y, _format_amount(cat_total))
        c.drawRightString(col_x[3] + 30 * mm, y, str(len(totals)))
        y -= 14

    y -= 6
    c.setFont("Helvetica-Bold", 11)
    for cur, total in sorted(grand_by_currency.items()):
        c.drawString(col_x[0], y, "GRAND TOTAL")
        c.drawString(col_x[1], y, cur or "—")
        c.drawRightString(col_x[2] + 30 * mm, y, _format_amount(total))
        y -= 14

    return y


def _draw_receipt_page(c: canvas.Canvas, seq: int, image_path: Path, caption: str) -> None:
    try:
        img = ImageReader(str(image_path))
        iw, ih = img.getSize()
    except Exception:
        c.setFont("Helvetica", 11)
        c.drawString(MARGIN, PAGE_H / 2, f"#{seq} — image unavailable")
        c.setFont("Helvetica-Oblique", 10)
        c.drawString(MARGIN, PAGE_H / 2 - 14, caption)
        return

    avail_w = PAGE_W - 2 * MARGIN
    avail_h = PAGE_H - 2 * MARGIN - 30
    scale = min(avail_w / iw, avail_h / ih)
    draw_w = iw * scale
    draw_h = ih * scale
    x = (PAGE_W - draw_w) / 2
    y = MARGIN + 30
    c.drawImage(img, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, MARGIN + 12, caption)


def build_pdf(data: ReportData, out_path: Path) -> Path:
    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle(f"{data.trip_name} expenses")

    _draw_header(c, data)
    _draw_summary_table(c, data, PAGE_H - MARGIN - 56)
    c.showPage()

    for r in data.rows:
        if not r.image_path:
            continue
        path = Path(r.image_path)
        if not path.exists():
            continue
        total_str = (
            f"{_format_amount(r.total)} {r.currency}" if r.total is not None and r.currency else "—"
        )
        caption = f"#{r.seq} — {r.vendor or 'unknown'} — {r.date or '—'} — {total_str}"
        _draw_receipt_page(c, r.seq, path, caption)
        c.showPage()

    c.save()
    return out_path
