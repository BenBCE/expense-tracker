"""PDF report generator using reportlab Platypus."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image as PlatypusImage,
)
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.reports.types import ReportData

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CAPTION_RESERVE = 22 * mm


def _format_amount(amount: Decimal | None) -> str:
    if amount is None:
        return "—"
    return f"{amount:,.2f}"


def _build_expenses_table(data: ReportData, body_small: ParagraphStyle) -> Table:
    headers = [
        "#",
        "Date",
        "Vendor",
        "Category",
        "Subtotal",
        "VAT",
        "Total",
        "Cur",
        "Note",
    ]
    rows: list[list[str | Paragraph]] = [headers]
    for r in data.rows:
        rows.append(
            [
                str(r.seq),
                r.date or "—",
                Paragraph(r.vendor or "—", body_small),
                r.category or "—",
                _format_amount(r.subtotal),
                _format_amount(r.vat),
                _format_amount(r.total),
                r.currency or "—",
                Paragraph(r.note or "", body_small),
            ]
        )

    available_width = PAGE_W - 2 * MARGIN
    fixed_widths: list[float | None] = [
        8 * mm,
        20 * mm,
        34 * mm,
        20 * mm,
        18 * mm,
        15 * mm,
        18 * mm,
        12 * mm,
        None,
    ]
    used = sum(w for w in fixed_widths if w is not None)
    note_width = max(20 * mm, available_width - used)
    col_widths = [w if w is not None else note_width for w in fixed_widths]

    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305496")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("ALIGN", (4, 1), (6, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f5f7fb")],
                ),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _scaled_image(image_path: Path) -> PlatypusImage:
    reader = ImageReader(str(image_path))
    iw, ih = reader.getSize()
    avail_w = PAGE_W - 2 * MARGIN
    avail_h = PAGE_H - 2 * MARGIN - CAPTION_RESERVE
    scale = min(avail_w / iw, avail_h / ih, 1.0)
    return PlatypusImage(str(image_path), width=iw * scale, height=ih * scale)


def build_pdf(data: ReportData, out_path: Path) -> Path:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"{data.trip_name} expenses",
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    body_style = styles["BodyText"]
    body_small = ParagraphStyle(
        "body_small", parent=body_style, fontSize=8, leading=10
    )
    caption_style = ParagraphStyle(
        "caption", parent=body_style, fontSize=10, leading=12
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    story: list = [
        Paragraph(f"Trip: {data.trip_name}", title_style),
        Paragraph(
            f"Generated: {generated} · Receipts: {len(data.rows)}", body_style
        ),
        Spacer(1, 10),
        _build_expenses_table(data, body_small),
    ]

    for r in data.rows:
        if not r.image_path:
            continue
        path = Path(r.image_path)
        if not path.exists():
            continue
        story.append(PageBreak())
        try:
            story.append(_scaled_image(path))
        except Exception:
            story.append(
                Paragraph(f"#{r.seq} — image unavailable", body_style)
            )
        total_str = (
            f"{_format_amount(r.total)} {r.currency}"
            if r.total is not None and r.currency
            else "—"
        )
        caption = (
            f"<b>#{r.seq}</b> — {r.vendor or 'unknown'} — "
            f"{r.date or '—'} — {total_str}"
        )
        story.append(Spacer(1, 6))
        story.append(Paragraph(caption, caption_style))

    doc.build(story)
    return out_path
