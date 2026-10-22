"""
pdf_report.py - driverwise Standstill Tracking PDF (reportlab), matches the
sample layout: title, summary row, 13 columns incl. Drivers, colour highlights,
legend, and a page-2 summary report. No LibreOffice needed.
"""
import os, datetime as dt
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer)

PINK = colors.HexColor("#FFC7CE"); PINK_TX = colors.HexColor("#9C0006")
RED  = colors.HexColor("#FF0000"); HDR  = colors.HexColor("#1F4E78")

HEADERS = ["Vehicles","Drivers","Total time\n(hh:mm)","Driving\n(km)","Diesel\nengine on (l)",
           "Avg diesel\n(km/l)","Standstill\n(%)","Standstill\n(hh:mm)","I-Shift\nPerf %",
           "Above\neconomy %","Engine\nload %","Coasting\n(%)","Avg diesel\n(l/h)"]
KEYS = ["name","drivers","total_hhmm","driving_km","diesel_l","kmpl","standstill_pct",
        "standstill_hhmm","ishift_pct","economy_pct","engineload_pct","coasting_pct","lph"]
WIDTHS = [40,78,32,33,40,38,40,40,34,40,34,34,41]  # points, fits A4 PORTRAIT (fit-to-width)

def _thr():
    return {"MIN_ENGINE_MIN": 30, "MIN_DRIVING_KM": 15,
            "STANDSTILL_FLAG": 27,
            "ENGINELOAD_FLAG": 20, "ISHIFT_FLAG": 2,
            "ECONOMY_FLAG": 10, "COASTING_FLAG": 20}

def _n(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def _hhmm_to_min(s):
    try: h,m = str(s).split(":"); return int(h)*60+int(m)
    except Exception: return 0

def _fmt(v):
    if v is None or v == "": return "-"
    if isinstance(v,(int,float)): return f"{v:.2f}"
    return str(v)

def _sum_hhmm(mins):
    return f"{int(mins)//60}:{int(mins)%60:02d}"

def normalize_and_filter_rows(source_rows, thresholds=None):
    """Normalize API rows and apply eligibility rules before any calculations."""
    thr = thresholds or _thr()
    # Normalize the direct API row names to the established portrait report
    # schema used by the attached browser-generated reference PDF.
    normalized = []
    for source in source_rows:
        total = source.get("Total time") or ""
        try:
            hours, minutes = str(total).split(":")
            total_min = int(hours) * 60 + int(minutes)
        except (TypeError, ValueError):
            total_min = 0
        normalized.append({
            "name": source.get("Vehicle"), "drivers": source.get("Driver") or "Unknown",
            "total_hhmm": total, "total_min": total_min,
            "driving_km": source.get("Driving km"), "diesel_l": source.get("Diesel litres"),
            "kmpl": source.get("Avg km/l"), "standstill_pct": source.get("Standstill %"),
            "standstill_hhmm": source.get("Standstill time"),
            "ishift_pct": source.get("I-Shift %"), "economy_pct": source.get("Above economy %"),
            "engineload_pct": source.get("Engine load %"), "coasting_pct": source.get("Coasting %"),
            "lph": source.get("Avg l/h"),
        })
    return [
        row for row in normalized
        if (row.get("total_min") or 0) >= thr["MIN_ENGINE_MIN"]
        and _n(row.get("driving_km")) is not None
        and _n(row.get("driving_km")) >= thr["MIN_DRIVING_KM"]
    ]


def report_filename(site, shift, start_dt, end_dt):
    """Canonical output filename used by generation and recovery planning."""
    return (f"{site.replace(' ','_')}_Shift{shift}_{start_dt:%Y-%m-%d}_"
            f"{start_dt:%H%M}_{end_dt:%H%M}.pdf")


def build_pdf(rows, site, shift, start_dt, end_dt, out_dir=None):
    thr = _thr(); out_dir = out_dir or os.path.join(os.path.dirname(__file__), "output", "pdf")
    os.makedirs(out_dir, exist_ok=True)

    rows = normalize_and_filter_rows(rows, thr)
    if not rows: return None

    kmpls=[_n(r["kmpl"]) for r in rows if _n(r["kmpl"]) and _n(r["kmpl"])>0]
    lphs =[_n(r["lph"])  for r in rows if _n(r["lph"])  and _n(r["lph"]) >0]
    avg_kmpl = sum(kmpls)/len(kmpls) if kmpls else 0
    avg_lph  = sum(lphs)/len(lphs)  if lphs else 0

    rows.sort(key=lambda r:(_n(r["standstill_pct"]) is None, -(_n(r["standstill_pct"]) or 0)))
    N = len(rows)

    def mean(key, pos=False):
        xs=[_n(r[key]) for r in rows if _n(r[key]) is not None and (not pos or _n(r[key])>0)]
        return sum(xs)/len(xs) if xs else 0
    summary = {
        "name": f"Summary ({N})", "drivers": f"{N} vehicles",
        "total_hhmm": _sum_hhmm(sum((r.get("total_min") or 0) for r in rows)),
        "driving_km": mean("driving_km"), "diesel_l": mean("diesel_l"),
        "kmpl": mean("kmpl", True), "standstill_pct": mean("standstill_pct"),
        "standstill_hhmm": _sum_hhmm(sum(_hhmm_to_min(r["standstill_hhmm"]) for r in rows)),
        "ishift_pct": mean("ishift_pct"), "economy_pct": mean("economy_pct"),
        "engineload_pct": mean("engineload_pct"), "coasting_pct": mean("coasting_pct"),
        "lph": mean("lph", True),
    }

    styles = getSampleStyleSheet()
    hstyle = ParagraphStyle("h", parent=styles["Normal"], fontSize=6, leading=6.8,
                            textColor=colors.white, alignment=1, fontName="Helvetica-Bold")
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=6.5, leading=7.5)
    cell_l = ParagraphStyle("cl", parent=styles["Normal"], fontSize=6, leading=6.8)
    cell_lb = ParagraphStyle("clb", parent=styles["Normal"], fontSize=6, leading=6.8, fontName="Helvetica-Bold")
    def _wrap(v, bold=False):
        t = ("" if v is None else str(v)).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        return Paragraph(t, cell_lb if bold else cell_l)

    # --- build table matrix ---
    header_cells = [Paragraph(h.replace("\n","<br/>"), hstyle) for h in HEADERS]
    def row_cells(r, bold=False):
        out=[]
        for k in KEYS:
            v=r.get(k)
            if k in ("name","drivers"):
                out.append(_wrap(v, bold)); continue
            if k in ("driving_km","diesel_l","kmpl","standstill_pct","ishift_pct",
                     "economy_pct","engineload_pct","coasting_pct","lph"):
                v=_fmt(v)
            out.append(v if v is not None else "-")
        return out
    data=[header_cells, row_cells(summary, bold=True)] + [row_cells(r) for r in rows]

    ts=TableStyle([
        ("BACKGROUND",(0,0),(-1,0),HDR),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTNAME",(0,1),(-1,1),"Helvetica-Bold"),
        ("FONTSIZE",(0,1),(-1,-1),6.5),
        ("GRID",(0,0),(-1,-1),0.4,colors.black),
        ("BOX",(0,0),(-1,-1),1.0,colors.black),
        ("ALIGN",(2,1),(-1,-1),"RIGHT"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),1.5),("BOTTOMPADDING",(0,0),(-1,-1),1.5),
    ])
    # highlights (data rows start at table row 2)
    def flag(i, col, fill, tx=None):
        ts.add("BACKGROUND",(col,i),(col,i),fill)
        if tx: ts.add("TEXTCOLOR",(col,i),(col,i),tx)
    for idx,r in enumerate(rows):
        tr=idx+2
        sp=_n(r["standstill_pct"]); el=_n(r["engineload_pct"]); km=_n(r["kmpl"])
        lh=_n(r["lph"]); ish=_n(r["ishift_pct"]); ec=_n(r["economy_pct"]); co=_n(r["coasting_pct"])
        if sp is not None and sp>thr["STANDSTILL_FLAG"]: flag(tr,6,RED,colors.white)
        if el is not None and el>thr["ENGINELOAD_FLAG"]: flag(tr,10,RED,colors.white)
        if km is not None and avg_kmpl and km<avg_kmpl:  flag(tr,5,PINK,PINK_TX)
        if lh is not None and avg_lph  and lh>avg_lph:   flag(tr,12,PINK,PINK_TX)
        if ish is not None and ish>thr["ISHIFT_FLAG"]:   flag(tr,8,PINK,PINK_TX)
        if ec is not None and ec>thr["ECONOMY_FLAG"]:    flag(tr,9,PINK,PINK_TX)
        if co is not None and co<thr["COASTING_FLAG"]:   flag(tr,11,PINK,PINK_TX)

    table=Table(data, colWidths=WIDTHS, repeatRows=1)
    table.setStyle(ts)

    # --- styles + shared values ---
    title_st=ParagraphStyle("t",parent=styles["Normal"],fontSize=12,leading=14,
                            fontName="Helvetica-Bold")
    sub_st  =ParagraphStyle("u",parent=styles["Normal"],fontSize=8,leading=10)
    kpi_st  =ParagraphStyle("k",parent=styles["Normal"],fontSize=9,leading=13)
    h2      =ParagraphStyle("h2",parent=styles["Normal"],fontSize=10,fontName="Helvetica-Bold")
    date_str=f"{start_dt:%d-%b-%Y}"
    period=f"{start_dt:%I:%M %p} to {end_dt:%I:%M %p}"

    idle       =[r for r in rows if (_n(r["standstill_pct"]) or 0)>thr["STANDSTILL_FLAG"]]
    econ_hits  =[r for r in rows if (_n(r["economy_pct"])    or 0)>thr["ECONOMY_FLAG"]]

    leg=('<font color="#FF0000">&#9632;</font> Standstill &gt;{ss}% &nbsp;'
         '<font color="#9C0006">&#9632;</font> km/l below fleet avg &nbsp;'
         '<font color="#FF0000">&#9632;</font> Engine load &gt;{el}% &nbsp;'
         '<font color="#9C0006">&#9632;</font> I-Shift &gt;{ish}% &nbsp;'
         '<font color="#9C0006">&#9632;</font> Economy &gt;{ec}% &nbsp;'
         '<font color="#9C0006">&#9632;</font> Coasting &lt;{co}% &nbsp;'
         '<font color="#9C0006">&#9632;</font> l/h above fleet avg &nbsp;|&nbsp; '
         'Sorted: Standstill % descending &nbsp;|&nbsp; '
         'Rows &lt;00:30 or distance &lt;{km:g} km excluded'
        ).format(ss=thr["STANDSTILL_FLAG"],el=thr["ENGINELOAD_FLAG"],ish=thr["ISHIFT_FLAG"],
                 ec=thr["ECONOMY_FLAG"],co=thr["COASTING_FLAG"],
                 km=thr["MIN_DRIVING_KM"])

    # Keep the two side-by-side summary tables within portrait page height.
    MAXROWS = 30
    def perf_block(label, key, flagv):
        hits=sorted([r for r in rows if (_n(r[key]) or 0)>flagv], key=lambda r:-(_n(r[key]) or 0))
        hdr=Paragraph(f"{label} | &gt; {flagv}% | Vehicles: {len(hits)}",
                      ParagraphStyle("pb",parent=small,fontName="Helvetica-Bold"))
        shown=hits[:MAXROWS]
        if shown:
            d=[["Vehicle","Driver","Value (%)"]] + [[r["name"], (r.get("drivers") or "Unknown"), f"{_n(r[key]):.2f}"] for r in shown]
            if len(hits) > MAXROWS:
                d.append([f"...and {len(hits)-MAXROWS} more","",""])
        else:
            d=[["Vehicle","Driver","Value (%)"], ["-","No vehicles exceeded threshold",""]]
        t=Table(d, colWidths=[48,150,50])
        t.setStyle(TableStyle([("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),0.3,colors.grey),
                   ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E7EEF6")),
                   ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                   ("ALIGN",(2,0),(2,-1),"RIGHT"),
                   ("TOPPADDING",(0,0),(-1,-1),1.5),("BOTTOMPADDING",(0,0),(-1,-1),1.5)]))
        return [hdr, Spacer(1,3), t]

    # ===== PAGE 1 : SUMMARY / OVERVIEW =====
    # Keep the metadata in fixed columns. A single flowing Paragraph allowed
    # Date, Period and Total vehicles to run together beneath the title.
    meta = Table([[
        Paragraph(f"<b>Date:</b> {date_str}", sub_st),
        Paragraph(f"<b>Period:</b> {period}", sub_st),
        Paragraph(f"<b>Total vehicles:</b> {N}", sub_st),
    ]], colWidths=[115, 250, 160])
    meta.setStyle(TableStyle([
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    elems=[Paragraph(f"Driverwise Standstill Tracking | {site} | Shift {shift}", title_st),
           Spacer(1,3), meta, Spacer(1,10)]
    kpi=(f'<b>Idling (Standstill &gt;{thr["STANDSTILL_FLAG"]}%):</b> {len(idle)} of {N} '
         f'&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp; <b>Above economy &gt;{thr["ECONOMY_FLAG"]}%:</b> {len(econ_hits)}')
    elems.append(Paragraph(kpi, kpi_st))
    elems.append(Spacer(1,10))
    elems.append(Paragraph("Performance Summary", h2))
    elems.append(Spacer(1,5))
    left  = perf_block("Idling (Standstill)","standstill_pct",thr["STANDSTILL_FLAG"])
    right = perf_block("Above Economy","economy_pct",thr["ECONOMY_FLAG"])
    two=Table([[left,right]], colWidths=[272,272])
    two.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),
                             ("LEFTPADDING",(0,0),(0,0),0),("RIGHTPADDING",(0,0),(0,0),8),
                             ("LEFTPADDING",(1,0),(1,0),8),("RIGHTPADDING",(1,0),(1,0),0)]))
    elems.append(two)

    # ===== VEHICLE-WISE DETAIL =====
    # Continue directly after the page-1 summary. ReportLab splits the table
    # naturally across pages only when the remaining space is exhausted.
    elems.append(Spacer(1,10))
    elems.append(Paragraph(f"Vehicle-wise Detail | {site} | Shift {shift}", h2))
    elems.append(Spacer(1,6))
    elems.append(table)
    elems.append(Spacer(1,6))
    elems.append(Paragraph(leg, ParagraphStyle("l",parent=styles["Normal"],fontSize=6.5,leading=8)))

    # --- render with footer page numbers ---
    stub=report_filename(site, shift, start_dt, end_dt)
    path=os.path.join(out_dir, stub)
    def footer(canvas, doc):
        canvas.saveState(); canvas.setFont("Helvetica",6.5)
        canvas.drawRightString(A4[0]-10*mm, 6*mm,
            f"Page {doc.page} | Generated: {dt.datetime.now():%d-%b-%Y %H:%M}")
        canvas.restoreState()
    doc=SimpleDocTemplate(path, pagesize=A4,
        leftMargin=8*mm, rightMargin=8*mm, topMargin=8*mm, bottomMargin=10*mm)
    doc.build(elems, onFirstPage=footer, onLaterPages=footer)
    return {"pdf":path, "vehicles":N, "avg_kmpl":round(avg_kmpl,2), "avg_lph":round(avg_lph,2),
            "idling":len(idle)}
