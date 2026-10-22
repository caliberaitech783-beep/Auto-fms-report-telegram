"""A4 portrait Scania shift report matching the supplied workbook rules."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, portrait
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BASE = Path(__file__).resolve().parent
PAGE_SIZE = portrait(A4)
HEADERS = [
    "Registration<br/>number", "Engine<br/>running time<br/>(h:m)", "Distance<br/>driven<br/>(km)",
    "Consumption<br/>liquid fuel<br/>(km/l)", "Consumption<br/>liquid fuel<br/>(litres)",
    "Consumption<br/>liquid fuel<br/>(litres/h)", "Idling (% of<br/>engine running<br/>time)",
    "Idling<br/>(h:m)", "Performance<br/>mode - Power<br/>(km)",
    "Performance<br/>mode - Power<br/>(% of distance)", "Powertrain<br/>coasting<br/>(% of distance)",
    "Adjusted<br/>Idling<br/>(%)",
]
KEYS = [
    "Registration number", "Engine running time", "Distance driven", "Consumption km/l",
    "Consumption litres", "Consumption litres/h", "Idling %", "Idling time",
    "Power km", "Power %", "Coasting %", "Adjusted Idling %",
]
THRESHOLDS = {
    "minimum_engine_minutes": 30, "minimum_distance_km": 15, "idling_pct": 28, "power_km": 30,
    "power_pct": 1, "coasting_pct": 20,
}


def _num(value):
    return float(value) if isinstance(value, (int, float)) else None


def _average(rows, key):
    values = [_num(row.get(key)) for row in rows]
    values = [value for value in values if value is not None and value > 0]
    return sum(values) / len(values) if values else 0.0


def _minutes(value):
    try:
        hours, minutes = str(value).split(":", 1)
        return int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return 0


def _hhmm(minutes):
    minutes = max(0, round(minutes))
    return f"{minutes // 60}:{minutes % 60:02d}"


def _summary_row(rows):
    engine_minutes = sum(row.get("engine_minutes") or 0 for row in rows)
    idle_minutes = sum(_minutes(row.get("Idling time")) for row in rows)
    distance = sum(_num(row.get("Distance driven")) or 0 for row in rows)
    fuel = sum(_num(row.get("Consumption litres")) or 0 for row in rows)
    power_km = sum(_num(row.get("Power km")) or 0 for row in rows)
    coast_km = sum(
        (_num(row.get("Distance driven")) or 0)
        * (_num(row.get("Coasting %")) or 0) / 100
        for row in rows
    )
    return {
        "Registration number": f"Summary ({len(rows)})",
        "Engine running time": _hhmm(engine_minutes),
        "Distance driven": distance,
        "Consumption km/l": distance / fuel if fuel else None,
        "Consumption litres": fuel,
        "Consumption litres/h": fuel / (engine_minutes / 60) if engine_minutes else None,
        "Idling %": idle_minutes / engine_minutes * 100 if engine_minutes else None,
        "Idling time": _hhmm(idle_minutes),
        "Power km": power_km,
        "Power %": power_km / distance * 100 if distance else None,
        "Coasting %": coast_km / distance * 100 if distance else None,
        "Adjusted Idling %": _average(rows, "Adjusted Idling %"),
    }


def apply_adjusted_idling(rows):
    """Apply fleet-normalized idling using only valid operating vehicles.

    Low-activity rows can contain short-window counter interpolation anomalies
    (for example, a large distance/fuel delta with less than 30 engine minutes).
    They must not distort the fleet litres/hour baseline used for page 1.
    """
    eligible_lph = [
        _num(row.get("Consumption litres/h"))
        for row in rows
        if (row.get("engine_minutes") or 0) >= THRESHOLDS["minimum_engine_minutes"]
        and (_num(row.get("Distance driven")) or 0) > THRESHOLDS["minimum_distance_km"]
        and (_num(row.get("Consumption litres/h")) or 0) > 0
    ]
    fleet_avg_lph = sum(eligible_lph) / len(eligible_lph) if eligible_lph else 20.0
    for row in rows:
        raw_idle = _num(row.get("Idling %"))
        lph = _num(row.get("Consumption litres/h"))
        row["Adjusted Idling %"] = (
            round(raw_idle * min(lph / fleet_avg_lph, 1.0), 1)
            if raw_idle is not None and lph is not None and fleet_avg_lph > 0
            else None
        )
    return fleet_avg_lph


def build_pdf(rows: list[dict], site: str, shift: str, start: datetime, stop: datetime,
              output_dir: str | Path | None = None) -> Path:
    apply_adjusted_idling(rows)
    operating_rows = [
        row for row in rows
        if (row.get("engine_minutes") or 0) >= THRESHOLDS["minimum_engine_minutes"]
        and (_num(row.get("Distance driven")) or 0) > THRESHOLDS["minimum_distance_km"]
    ]
    low_activity_rows = [row for row in rows if row not in operating_rows]
    operating_rows.sort(key=lambda row: -(_num(row.get("Adjusted Idling %")) or 0))
    low_activity_rows.sort(
        key=lambda row: (
            -(_num(row.get("Distance driven")) or 0),
            -(row.get("engine_minutes") or 0),
        )
    )
    avg_kmpl = _average(operating_rows, "Consumption km/l")
    avg_lph = _average(operating_rows, "Consumption litres/h")
    output = Path(output_dir or BASE / "output" / "pdf")
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{site.replace(' ', '_')}_{shift}_{start:%Y%m%d}_{stop:%H%M}.pdf"

    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Normal"], fontName="Helvetica-Bold",
                           fontSize=10, leading=12, alignment=1)
    head = ParagraphStyle("head", parent=styles["Normal"], fontName="Helvetica-Bold",
                          textColor=colors.white, fontSize=5.5, leading=6, alignment=1)
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=6, leading=7)
    low_cell = ParagraphStyle("low_cell", parent=cell, fontSize=5.2, leading=5.8)
    low_flag = ParagraphStyle("low_flag", parent=low_cell, fontName="Helvetica-Bold",
                              textColor=colors.HexColor("#B46400"))
    summary_cell = ParagraphStyle("summary", parent=cell, fontName="Helvetica-Bold")
    pale, pale_text, red = colors.HexColor("#FFC7CE"), colors.HexColor("#9C0006"), colors.red

    def make_table(table_rows, include_summary=False, low_activity=False):
        data = [[Paragraph(value, head) for value in HEADERS]]
        if not table_rows:
            data.append([
                Paragraph(
                    "No operating vehicles met both the 00:30 engine-time and over-15-km distance rules.",
                    ParagraphStyle("empty", parent=styles["Normal"], fontSize=7, leading=9,
                                   alignment=1, textColor=colors.HexColor("#666666")),
                )
            ] + [""] * (len(HEADERS) - 1))
        if include_summary and table_rows:
            summary = _summary_row(table_rows)
            summary_values = []
            for key in KEYS:
                value = summary.get(key)
                if isinstance(value, float):
                    value = f"{value:.2f}"
                summary_values.append(
                    Paragraph(str(value if value is not None else "-"), summary_cell)
                )
            data.append(summary_values)
        for row in table_rows:
            values = []
            for key in KEYS:
                value = "IGNITION ONLY" if low_activity and key == "Adjusted Idling %" else row.get(key)
                if isinstance(value, float):
                    value = f"{value:.2f}"
                value_style = low_flag if low_activity and key == "Adjusted Idling %" else (
                    low_cell if low_activity else cell
                )
                values.append(Paragraph(str(value if value is not None else "-"), value_style))
            data.append(values)

        summary_offset = 1 if include_summary and table_rows else 0
        table = Table(
            data, colWidths=[41, 33, 32, 36, 34, 35, 36, 29, 36, 40, 37, 45],
            repeatRows=1 + summary_offset,
        )
        commands = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 1 if low_activity else 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1 if low_activity else 2),
        ]
        if not table_rows:
            commands.extend([
                ("SPAN", (0, 1), (-1, 1)),
                ("ALIGN", (0, 1), (-1, 1), "CENTER"),
                ("TOPPADDING", (0, 1), (-1, 1), 8),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
            ])
        elif include_summary:
            commands.extend([
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#E7E6E6")),
                ("LINEBELOW", (0, 1), (-1, 1), 0.8, colors.black),
            ])

        for index, row in enumerate(table_rows, 1 + summary_offset):
            def highlight(column, fill, text=colors.black):
                commands.extend([
                    ("BACKGROUND", (column, index), (column, index), fill),
                    ("TEXTCOLOR", (column, index), (column, index), text),
                ])
            kmpl = _num(row.get("Consumption km/l"))
            lph = _num(row.get("Consumption litres/h"))
            if kmpl is not None and kmpl < avg_kmpl:
                highlight(3, pale, pale_text)
            if lph is not None and lph > avg_lph:
                highlight(5, pale, pale_text)
            if not low_activity and (_num(row.get("Adjusted Idling %")) or 0) > THRESHOLDS["idling_pct"]:
                highlight(11, red)
            if not low_activity and (_num(row.get("Power km")) or 0) > THRESHOLDS["power_km"]:
                highlight(8, red, colors.white)
            if not low_activity and (_num(row.get("Power %")) or 0) > THRESHOLDS["power_pct"]:
                highlight(9, pale, pale_text)
            coast = _num(row.get("Coasting %"))
            if coast is not None and coast < THRESHOLDS["coasting_pct"]:
                highlight(10, pale, pale_text)
            if low_activity:
                commands.extend([
                    ("TEXTCOLOR", (11, index), (11, index), colors.HexColor("#B46400")),
                    ("FONTNAME", (11, index), (11, index), "Helvetica-Bold"),
                ])
        table.setStyle(TableStyle(commands))
        return table

    operating_table = make_table(operating_rows, include_summary=True)

    legend = (
        "<b>Thresholds:</b> Page 1 = engine time at least 00:30 and distance over 15 km | "
        "Page 2 = engine time under 00:30 or distance at/below 15 km (ignition/workshop) | "
        "km/l below fleet average = light red | litres/h above fleet average = light red | "
        "adjusted idling &gt; 28% = red | Power distance &gt; 30 km = red | "
        "Power share &gt; 1% = light red | coasting &lt; 20% = light red | "
        "sorted by adjusted idling % descending"
    )
    story = [
        Paragraph(f"Scania Vehicle Report | {site} | Shift {shift}", title),
        Paragraph(
                  f"{start:%d-%b-%Y %H:%M} to {stop:%d-%b-%Y %H:%M} | "
                  f"Operating: {len(operating_rows)} | Low activity: {len(low_activity_rows)}",
                  ParagraphStyle("sub", parent=styles["Normal"], fontSize=7, alignment=1)),
        Spacer(1, 5), operating_table, Spacer(1, 5),
        Paragraph(legend, ParagraphStyle("legend", parent=styles["Normal"], fontSize=6, leading=7)),
    ]
    if low_activity_rows:
        low_banner = ParagraphStyle(
            "low_banner", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=9, leading=11, alignment=1, textColor=colors.white,
            backColor=colors.HexColor("#666666"), borderPadding=4,
        )
        story.extend([
            PageBreak(),
            Paragraph(f"Scania Vehicle Report | {site} | Shift {shift}", title),
            Paragraph(f"{start:%d-%b-%Y %H:%M} to {stop:%d-%b-%Y %H:%M}",
                      ParagraphStyle("low_sub", parent=styles["Normal"], fontSize=7, alignment=1)),
            Spacer(1, 5),
            Paragraph(
                "LOW ACTIVITY VEHICLES (distance at/below 15 km | ignition/workshop time)",
                low_banner,
            ),
            Spacer(1, 4),
            make_table(low_activity_rows, low_activity=True),
            Spacer(1, 5),
            Paragraph(legend, ParagraphStyle("low_legend", parent=styles["Normal"],
                                             fontSize=6, leading=7)),
        ])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 6)
        canvas.drawRightString(PAGE_SIZE[0] - 8 * mm, 5 * mm, f"Page {doc.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(path), pagesize=PAGE_SIZE, leftMargin=7 * mm, rightMargin=7 * mm,
        topMargin=7 * mm, bottomMargin=9 * mm,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return path
