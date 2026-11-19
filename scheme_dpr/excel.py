"""Write the 5-year model as an .xlsx with live formulas, recalculate it with
LibreOffice headless, and check it against the Python model.

Layout
* Inputs   - every assumption, blue font = editable input
* Model    - set-up cost, means of finance, 5-year P&L, loan, DSCR, payback, break-even
* Schemes  - eligibility results (values, with sources)
* Read me  - how to use it, and the human-review notice
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

from .eligibility import Evaluation
from .finance import YEARS, ModelResult
from .i18n import Strings
from .models import Project

BLUE = Font(color="0000FF")
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
TOTAL_FILL = PatternFill("solid", fgColor="F2F2F2")
INPUT_FILL = PatternFill("solid", fgColor="FFF9E5")
THIN = Side(style="thin", color="BFBFBF")
TOP = Border(top=THIN)
LAKH_FMT = '#,##0.00;[Red]-#,##0.00'
PCT_FMT = "0.0%"
X_FMT = '0.00"x"'
YEAR_COLS = [get_column_letter(2 + i) for i in range(YEARS)]  # B..F

ERROR_MARKERS = ("#REF!", "#NAME?", "#DIV/0!", "#VALUE!", "#N/A", "#NUM!", "#NULL!", "Err:")


@dataclass
class CellMap:
    """Where each modelled quantity lives, so the recalculated file can be checked."""

    scalars: dict[str, str] = field(default_factory=dict)  # key -> "Model!B14"
    rows: dict[str, int] = field(default_factory=dict)  # key -> row on Model (years in B..F)
    monthly: dict[str, int] = field(default_factory=dict)  # key -> row on "Year 1 monthly" (months in B..M)


class _Inputs:
    """Helper that appends labelled input rows and defines a workbook name for each."""

    def __init__(self, wb: Workbook, ws):
        self.wb, self.ws, self.row = wb, ws, 1

    def section(self, title: str) -> None:
        self.row += 1
        c = self.ws.cell(self.row, 1, title)
        c.font = BOLD
        c.fill = HEAD_FILL
        for col in range(2, 5):
            self.ws.cell(self.row, col).fill = HEAD_FILL
        self.row += 1

    def scalar(self, name: str, label: str, value, unit: str = "", note: str = "", fmt: str | None = None) -> str:
        r = self.row
        self.ws.cell(r, 1, label)
        c = self.ws.cell(r, 2, value)
        c.font, c.fill = BLUE, INPUT_FILL
        if fmt:
            c.number_format = fmt
        self.ws.cell(r, 3, unit)
        self.ws.cell(r, 4, note)
        ref = f"Inputs!$B${r}"
        self.wb.defined_names[name] = DefinedName(name, attr_text=ref)
        self.row += 1
        return ref


def build_workbook(p: Project, m: ModelResult, ev: Evaluation, s: Strings) -> tuple[Workbook, CellMap]:
    wb = Workbook()
    readme = wb.active
    readme.title = s("xlsx.sheet_readme")
    # "Inputs" and "Model" are referenced inside formulas, so they are not translated.
    wi = wb.create_sheet("Inputs")
    wm = wb.create_sheet("Model")
    wmo = wb.create_sheet(MONTHLY_SHEET)
    wsch = wb.create_sheet(s("xlsx.sheet_schemes"))

    cm = CellMap()
    rows = _write_inputs(wb, wi, p, m, ev, s)
    _write_model(wm, p, m, s, cm, rows)
    _write_monthly(wmo, p, m, s, cm, rows)
    _write_schemes(wsch, ev, s)
    _write_readme(readme, p, ev, s)
    for ws in wb.worksheets:  # print one page wide, landscape - how bank officers read it
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.active = 2  # open on the Model sheet
    return wb, cm


# ---------------------------------------------------------------- Inputs


@dataclass
class InputRows:
    setup: list[int] = field(default_factory=list)
    revenue: list[int] = field(default_factory=list)
    other: list[int] = field(default_factory=list)


def _write_inputs(wb, ws, p: Project, m: ModelResult, ev: Evaluation, s: Strings) -> InputRows:
    ws.column_dimensions["A"].width = 44
    for col, w in zip("BCDEFG", (14, 14, 14, 14, 14, 14)):
        ws.column_dimensions[col].width = w
    ws.cell(1, 1, f"{p.name} - {s('xlsx.inputs_title')}").font = TITLE
    ws.cell(2, 1, s("xlsx.inputs_hint"))
    ins = _Inputs(wb, ws)
    ins.row = 3

    # Set-up items: a small table. Amount is an input, or qty × rate.
    ins.section(s("xlsx.setup_items"))
    hdr = [s("xlsx.item"), s("xlsx.qty"), s("xlsx.unit"), s("xlsx.rate_lakh"), s("xlsx.amount_lakh")]
    for i, h in enumerate(hdr, 1):
        ws.cell(ins.row, i, h).font = BOLD
    ins.row += 1
    rows = InputRows()
    for it in p.setup_cost.items:
        r = ins.row
        ws.cell(r, 1, it.name)
        if it.amount is not None:
            c = ws.cell(r, 5, it.amount)
            c.font, c.fill = BLUE, INPUT_FILL
        else:
            for col, v in ((2, it.qty), (4, it.rate)):
                c = ws.cell(r, col, v)
                c.font, c.fill = BLUE, INPUT_FILL
            ws.cell(r, 3, it.unit or "")
            ws.cell(r, 5, f"=B{r}*D{r}")
        ws.cell(r, 5).number_format = LAKH_FMT
        rows.setup.append(r)
        ins.row += 1
    ins.scalar("contingency_pct", s("xlsx.contingency_pct"), p.setup_cost.contingency_pct, "%", s("xlsx.contingency_note"), PCT_FMT)
    ins.scalar("working_capital", s("xlsx.working_capital"), p.setup_cost.working_capital, "₹ lakh", "", LAKH_FMT)

    ins.section(s("xlsx.revenue_streams"))
    hdr = [s("xlsx.stream"), s("xlsx.per_day"), s("xlsx.price_rs"), s("xlsx.cogs_pct"), s("xlsx.annual_lakh"), s("xlsx.rev_share_flag"), s("xlsx.primary_flag")]
    for i, h in enumerate(hdr, 1):
        ws.cell(ins.row, i, h).font = BOLD
    ins.row += 1
    for st in p.revenue:
        r = ins.row
        ws.cell(r, 1, st.name)
        vals = {2: st.per_day, 3: st.price_rs, 4: st.cogs_pct, 5: st.annual_lakh}
        for col, v in vals.items():
            if v is None:
                continue
            c = ws.cell(r, col, v)
            c.font, c.fill = BLUE, INPUT_FILL
        ws.cell(r, 4).number_format = PCT_FMT
        ws.cell(r, 6, "yes" if st.revenue_share else "")
        ws.cell(r, 7, "yes" if st.primary else "")
        rows.revenue.append(r)
        ins.row += 1

    c = p.costs
    ins.section(s("xlsx.running_costs"))
    ins.scalar("staff_count", s("xlsx.staff_count"), c.staff.count, s("xlsx.people"))
    ins.scalar("staff_monthly", s("xlsx.staff_monthly"), c.staff.monthly_lakh, "₹ lakh / month")
    ins.scalar("manager_monthly", s("xlsx.manager_monthly"), c.staff.manager_monthly_lakh, "₹ lakh / month")
    ins.scalar("rent_annual", s("xlsx.rent_annual"), c.rent_annual_lakh, "₹ lakh / yr")
    ins.scalar("revenue_share_pct", s("xlsx.revenue_share_pct"), c.revenue_share_pct, "%", s("xlsx.revenue_share_note"), PCT_FMT)
    ins.scalar("power_kwh", s("xlsx.power_kwh"), c.electricity.kwh_per_day, "kWh / day")
    ins.scalar("power_rate", s("xlsx.power_rate"), c.electricity.rate_rs, "₹ / kWh")
    for o in c.other:
        r = ins.row
        ws.cell(r, 1, o.name)
        cell = ws.cell(r, 2, o.amount)
        cell.font, cell.fill = BLUE, INPUT_FILL
        ws.cell(r, 3, "₹ lakh / yr")
        rows.other.append(r)
        ins.row += 1

    f = p.finance
    sel = ev.selected
    ins.section(s("xlsx.funding"))
    scheme_note = f"{sel.name}" if sel else s("xlsx.no_scheme")
    ins.scalar("own_pct", s("xlsx.own_pct"), m.finance.own_pct, "%", scheme_note, PCT_FMT)
    ins.scalar("subsidy_pct", s("xlsx.subsidy_pct"), m.finance.subsidy_pct, "%", s("xlsx.subsidy_note"), PCT_FMT)
    ins.scalar("subsidy_cap", s("xlsx.subsidy_cap"), m.finance.subsidy_cap if m.finance.subsidy_cap is not None else "",
               "₹ lakh", s("xlsx.subsidy_cap_note"), LAKH_FMT)
    ins.scalar("interest_rate", s("xlsx.interest_rate"), f.interest_rate, "% / yr", "", PCT_FMT)
    ins.scalar("tenor", s("xlsx.tenor"), f.tenor_years, s("xlsx.years"), s("xlsx.tenor_note"))
    ins.scalar("moratorium", s("xlsx.moratorium"), f.moratorium_months, s("xlsx.months"))
    ins.scalar("tax_rate", s("xlsx.tax_rate"), f.tax_rate, "%", s("xlsx.tax_note"), PCT_FMT)
    ins.scalar("dep_years", s("xlsx.dep_years"), f.depreciation_years, s("xlsx.years"))

    a = p.assumptions
    ins.section(s("xlsx.growth"))
    ins.scalar("ramp1", s("xlsx.ramp1"), a.ramp_year1, "x")
    ins.scalar("ramp2", s("xlsx.ramp2"), a.ramp_year2, "x")
    ins.scalar("growth", s("xlsx.growth_rate"), a.growth_after_year3, "% / yr", "", PCT_FMT)
    ins.scalar("inflation", s("xlsx.inflation"), a.inflation, "% / yr", "", PCT_FMT)
    ins.scalar("days", s("xlsx.days"), a.days_per_year, s("xlsx.days_unit"))
    ins.scalar("min_dscr", s("xlsx.min_dscr"), a.min_dscr, "x")
    ws.freeze_panes = "B4"
    return rows


# ---------------------------------------------------------------- Model


def _write_model(ws, p: Project, m: ModelResult, s: Strings, cm: CellMap, rows: InputRows) -> None:
    setup_rows, rev_rows, other_rows = rows.setup, rows.revenue, rows.other
    ws.column_dimensions["A"].width = 48
    for col in YEAR_COLS:
        ws.column_dimensions[col].width = 13
    ws.cell(1, 1, f"{p.name} - {s('xlsx.model_title')}").font = TITLE
    r = 3

    def label(row, text, bold=False, fill=None):
        c = ws.cell(row, 1, text)
        if bold:
            c.font = BOLD
        if fill:
            for col in range(1, 2 + YEARS):
                ws.cell(row, col).fill = fill

    def header(row, text):
        label(row, text, True, HEAD_FILL)
        ws.cell(row, 2, "₹ lakh").font = BOLD

    # --- set-up cost
    header(r, s("dpr.cost_table_title")); r += 1
    first = r
    for it, ir in zip(p.setup_cost.items, setup_rows):
        label(r, it.name)
        ws.cell(r, 2, f"=Inputs!$E${ir}").number_format = LAKH_FMT
        r += 1
    label(r, s("xlsx.contingency"))
    ws.cell(r, 2, f"=SUM(B{first}:B{r-1})*contingency_pct").number_format = LAKH_FMT
    cm.scalars["contingency"] = f"Model!B{r}"; r += 1
    label(r, s("xlsx.working_capital"))
    ws.cell(r, 2, "=working_capital").number_format = LAKH_FMT; r += 1
    label(r, s("xlsx.total_cost"), True, TOTAL_FILL)
    ws.cell(r, 2, f"=SUM(B{first}:B{r-1})").number_format = LAKH_FMT
    ws.cell(r, 2).font = BOLD
    total = f"$B${r}"
    cm.scalars["project_cost"] = f"Model!B{r}"; r += 2

    # --- means of finance
    header(r, s("dpr.mof_title")); r += 1
    label(r, s("xlsx.own"))
    ws.cell(r, 2, f"={total}*own_pct").number_format = LAKH_FMT
    cm.scalars["own"] = f"Model!B{r}"; r += 1
    label(r, s("xlsx.subsidy"))
    ws.cell(r, 2, f'=IF(subsidy_cap="",{total}*subsidy_pct,MIN({total}*subsidy_pct,subsidy_cap))').number_format = LAKH_FMT
    sub = f"$B${r}"
    cm.scalars["subsidy"] = f"Model!B{r}"; r += 1
    label(r, s("xlsx.gross_loan"))
    ws.cell(r, 2, f"={total}-B{r-2}").number_format = LAKH_FMT
    cm.scalars["gross_loan"] = f"Model!B{r}"; r += 1
    label(r, s("xlsx.net_loan"), True)
    ws.cell(r, 2, f"=IF({total}-B{r-3}-{sub}<0.000000001,0,{total}-B{r-3}-{sub})").number_format = LAKH_FMT
    net_loan = f"$B${r}"
    cm.scalars["net_loan"] = f"Model!B{r}"; r += 2

    # --- years
    yr_row = r
    label(r, s("xlsx.year"), True, HEAD_FILL)
    for i, col in enumerate(YEAR_COLS):
        ws[f"{col}{r}"] = i + 1
        ws[f"{col}{r}"].font = BOLD
    r += 1
    vf = r
    label(r, s("xlsx.volume_factor"))
    for col in YEAR_COLS:
        ws[f"{col}{r}"] = f"=IF({col}${yr_row}=1,ramp1,IF({col}${yr_row}=2,ramp2,1))*(1+growth)^MAX(0,{col}${yr_row}-3)"
        ws[f"{col}{r}"].number_format = "0.000"
    cm.rows["volume_factor"] = r; r += 1
    pf = r
    label(r, s("xlsx.price_factor"))
    for col in YEAR_COLS:
        ws[f"{col}{r}"] = f"=(1+inflation)^({col}${yr_row}-1)"
        ws[f"{col}{r}"].number_format = "0.000"
    cm.rows["price_factor"] = r; r += 1

    def year_row(key, text, formula_for_col, bold=False, fill=None, fmt=LAKH_FMT):
        nonlocal r
        label(r, text, bold, fill)
        for col in YEAR_COLS:
            ws[f"{col}{r}"] = formula_for_col(col)
            ws[f"{col}{r}"].number_format = fmt
            if bold:
                ws[f"{col}{r}"].font = BOLD
        cm.rows[key] = r
        r += 1
        return r - 1

    stream_rows = {}
    for st, ir in zip(p.revenue, rev_rows):
        if st.is_volume:
            fx = lambda col, ir=ir: f"=Inputs!$B${ir}*days*{col}${vf}*Inputs!$C${ir}*{col}${pf}/100000"
        else:
            fx = lambda col, ir=ir: f"=Inputs!$E${ir}*{col}${pf}"
        stream_rows[st.name] = (year_row(f"rev:{st.name}", st.name, fx), ir)
    rs, re_ = min(v[0] for v in stream_rows.values()), max(v[0] for v in stream_rows.values())
    rev = year_row("revenue", s("xlsx.total_revenue"), lambda col: f"=SUM({col}{rs}:{col}{re_})", True, TOTAL_FILL)

    def cogs_fx(col):
        parts = [f"{col}{row}*Inputs!$D${ir}" for (row, ir) in stream_rows.values()]
        return "=" + "+".join(parts)
    cogs = year_row("cogs", s("xlsx.cogs"), cogs_fx)
    gp = year_row("gross_profit", s("xlsx.gross_profit"), lambda col: f"={col}{rev}-{col}{cogs}", True)

    staff = year_row("staff", s("xlsx.staff"), lambda col: f"=(staff_count*staff_monthly+manager_monthly)*12*{col}${pf}")
    share_rows = [stream_rows[x.name][0] for x in p.revenue if x.revenue_share]

    def rent_fx(col):
        base = "+".join(f"{col}{row}" for row in share_rows) or "0"
        return f"=rent_annual*{col}${pf}+revenue_share_pct*({base})"
    rent = year_row("rent_share", s("xlsx.rent_share"), rent_fx)
    elec = year_row("electricity", s("xlsx.electricity"), lambda col: f"=power_kwh*power_rate*days*{col}${pf}/100000")
    first_cost = staff
    for o, ir in zip(p.costs.other, other_rows):
        year_row(f"other:{o.name}", o.name, lambda col, ir=ir: f"=Inputs!$B${ir}*{col}${pf}")
    last_cost = r - 1
    run = year_row("running_costs", s("xlsx.running_total"), lambda col: f"=SUM({col}{first_cost}:{col}{last_cost})", True, TOTAL_FILL)
    ebitda = year_row("ebitda", "EBITDA", lambda col: f"={col}{gp}-{col}{run}", True)
    dep = year_row("depreciation", s("xlsx.depreciation"), lambda col: f"=({total}-working_capital)/dep_years")

    lo = r
    label(r, s("xlsx.loan_open"))
    for i, col in enumerate(YEAR_COLS):
        ws[f"{col}{r}"] = f"={net_loan}" if i == 0 else f"={YEAR_COLS[i-1]}{r+3}"
        ws[f"{col}{r}"].number_format = LAKH_FMT
    cm.rows["loan_open"] = r; r += 1
    intr = year_row("interest", s("xlsx.interest"), lambda col: f"={col}{lo}*interest_rate")
    # Equal monthly principal after the moratorium; months repaid in this year:
    # MAX(0, y*12-mor) - MAX(0, (y-1)*12-mor)
    prin = year_row("principal", s("xlsx.principal"), lambda col: (
        f"=MIN({col}{lo},{net_loan}/(tenor*12-moratorium)"
        f"*(MAX(0,{col}${yr_row}*12-moratorium)-MAX(0,({col}${yr_row}-1)*12-moratorium)))"))
    year_row("loan_close", s("xlsx.loan_close"), lambda col: f"={col}{lo}-{col}{prin}")
    pbt = year_row("pbt", s("xlsx.pbt"), lambda col: f"={col}{ebitda}-{col}{dep}-{col}{intr}", True)
    tax = year_row("tax", s("xlsx.tax"), lambda col: f"=MAX(0,{col}{pbt})*tax_rate")
    year_row("net_profit", s("xlsx.net_profit"), lambda col: f"={col}{pbt}-{col}{tax}", True, TOTAL_FILL)
    cash = year_row("cash_after_debt", s("xlsx.cash_after_debt"), lambda col: f"={col}{ebitda}-{col}{tax}-{col}{intr}-{col}{prin}")

    cum = r
    label(r, s("xlsx.cumulative"))
    for i, col in enumerate(YEAR_COLS):
        ws[f"{col}{r}"] = f"=-{cm.scalars['own'].split('!')[1]}+{col}{cash}" if i == 0 else f"={YEAR_COLS[i-1]}{r}+{col}{cash}"
        ws[f"{col}{r}"].number_format = LAKH_FMT
    cm.rows["cumulative_cash"] = r; r += 1
    dscr = year_row("dscr", s("xlsx.dscr"),
                    lambda col: f'=IF({col}{intr}+{col}{prin}<0.000000001,"",({col}{ebitda}-{col}{tax})/({col}{intr}+{col}{prin}))',
                    True, None, X_FMT)
    r += 1

    # --- summary
    header(r, s("xlsx.summary")); ws.cell(r, 2, ""); r += 1
    y0, y1 = YEAR_COLS[0], YEAR_COLS[-1]

    def summary(key, text, formula, fmt):
        nonlocal r
        label(r, text)
        ws.cell(r, 2, formula).number_format = fmt
        ws.cell(r, 2).font = BOLD
        cm.scalars[key] = f"Model!B{r}"
        r += 1

    summary("min_dscr", s("xlsx.min_dscr_out"), f"=MIN({y0}{dscr}:{y1}{dscr})", X_FMT)
    summary("avg_dscr", s("xlsx.avg_dscr_out"), f"=AVERAGE({y0}{dscr}:{y1}{dscr})", X_FMT)
    label(r, s("xlsx.dscr_test"))
    ws.cell(r, 2, f'=IF(B{r-2}>=min_dscr,"OK","BELOW BANK NORM")').font = BOLD
    r += 1
    summary("payback_year", s("xlsx.payback"),
            f'=IFERROR(INDEX({y0}{yr_row}:{y1}{yr_row},MATCH(TRUE(),INDEX({y0}{cum}:{y1}{cum}>=0,0),0)),"> 5")', "0")
    prim = p.primary_stream
    if prim is not None:
        prow, pir = stream_rows[prim.name]
        y3 = YEAR_COLS[2]
        others = [(row, ir) for name, (row, ir) in stream_rows.items() if name != prim.name]
        other_contrib = "+".join(f"{y3}{row}*(1-Inputs!$D${ir})" for row, ir in others) or "0"
        summary("breakeven", s("xlsx.breakeven").format(unit=prim.name.lower()),
                f"=({y3}{run}+{y3}{intr}+{y3}{prin}-({other_contrib}))"
                f"/(Inputs!$C${pir}*{y3}${pf}*(1-Inputs!$D${pir})*days/100000)", "0.0")
    ws.freeze_panes = "B3"


# ---------------------------------------------------------------- Year 1 monthly

MONTHLY_SHEET = "Year 1 monthly"  # referenced by the checks below; not translated
MONTH_COLS = [get_column_letter(2 + i) for i in range(12)]  # B..M


def _write_monthly(ws, p: Project, m: ModelResult, s: Strings, cm: CellMap, rows: InputRows) -> None:
    ws.column_dimensions["A"].width = 40
    for col in MONTH_COLS + ["N"]:
        ws.column_dimensions[col].width = 10
    ws.cell(1, 1, f"{p.name} - {s('xlsx.monthly_title')}").font = TITLE
    ws.cell(2, 1, s("xlsx.monthly_hint"))
    r = 4
    ws.cell(r, 1, s("xlsx.month")).font = BOLD
    for i, col in enumerate(MONTH_COLS):
        ws[f"{col}{r}"] = i + 1
        ws[f"{col}{r}"].font = BOLD
        ws[f"{col}{r}"].fill = HEAD_FILL
    ws.cell(r, 14, s("xlsx.year1_total")).font = BOLD
    mrow = r
    r += 1
    raw = r
    ws.cell(r, 1, s("xlsx.profile_raw"))
    weights_in = p.assumptions.year1_monthly_profile or [0.6 + 0.8 * i / 11 for i in range(12)]
    for col, v in zip(MONTH_COLS, weights_in):
        c = ws[f"{col}{r}"]
        c.value = v
        c.font, c.fill, c.number_format = BLUE, INPUT_FILL, "0.000"
    r += 1
    wrow = r
    ws.cell(r, 1, s("xlsx.profile"))
    for col in MONTH_COLS:
        ws[f"{col}{r}"] = f"={col}{raw}/AVERAGE($B${raw}:$M${raw})"
        ws[f"{col}{r}"].number_format = "0.000"
    cm.monthly["weight"] = r
    r += 1

    def mrow_(key, text, fx, bold=False, total=True, fmt=LAKH_FMT):
        nonlocal r
        ws.cell(r, 1, text).font = BOLD if bold else Font()
        for col in MONTH_COLS:
            ws[f"{col}{r}"] = fx(col)
            ws[f"{col}{r}"].number_format = fmt
            if bold:
                ws[f"{col}{r}"].font = BOLD
        if total:
            ws.cell(r, 14, f"=SUM(B{r}:M{r})").number_format = fmt
        cm.monthly[key] = r
        r += 1
        return r - 1

    srow = {}
    for st, ir in zip(p.revenue, rows.revenue):
        yrow = cm.rows[f"rev:{st.name}"]
        if st.is_volume:
            fx = lambda col, yrow=yrow: f"=Model!$B${yrow}*{col}${wrow}/12"
        else:
            fx = lambda col, yrow=yrow: f"=Model!$B${yrow}/12"
        srow[st.name] = (mrow_(f"rev:{st.name}", st.name, fx), ir)
    first, last = min(v[0] for v in srow.values()), max(v[0] for v in srow.values())
    sales = mrow_("revenue", s("xlsx.total_revenue"), lambda col: f"=SUM({col}{first}:{col}{last})", True)
    cogs = mrow_("cogs", s("xlsx.cogs"), lambda col: "=" + "+".join(f"{col}{row}*Inputs!$D${ir}" for row, ir in srow.values()))
    share = [srow[x.name][0] for x in p.revenue if x.revenue_share]
    run_y1 = f"Model!$B${cm.rows['running_costs']}"
    if share:
        base_y1 = "+".join(f"Model!$B${cm.rows['rev:' + x.name]}" for x in p.revenue if x.revenue_share)
        fx_run = lambda col: (f"=({run_y1}-revenue_share_pct*({base_y1}))/12"
                              f"+revenue_share_pct*({'+'.join(f'{col}{row}' for row in share)})")
    else:
        fx_run = lambda col: f"={run_y1}/12"
    run = mrow_("running_costs", s("xlsx.running_total"), fx_run)
    ebitda = mrow_("ebitda", "EBITDA", lambda col: f"={col}{sales}-{col}{cogs}-{col}{run}", True)

    net_loan = "Model!" + cm.scalars["net_loan"].split("!")[1].replace("B", "$B$")
    lo = r
    mrow_("loan_open", s("xlsx.loan_open_m"),
          lambda col: f"={net_loan}" if col == "B" else f"={MONTH_COLS[MONTH_COLS.index(col) - 1]}{lo + 3}", total=False)
    intr = mrow_("interest", s("xlsx.interest"), lambda col: f"={col}{lo}*interest_rate/12")
    prin = mrow_("principal", s("xlsx.principal"),
                 lambda col: f"=IF({col}${mrow}>moratorium,MIN({col}{lo},{net_loan}/(tenor*12-moratorium)),0)")
    mrow_("loan_close", s("xlsx.loan_close_m"), lambda col: f"={col}{lo}-{col}{prin}", total=False)
    net = mrow_("net_cash", s("xlsx.net_cash"), lambda col: f"={col}{ebitda}-{col}{intr}-{col}{prin}", True)
    co = r
    mrow_("cash_open", s("xlsx.cash_open"),
          lambda col: "=working_capital" if col == "B" else f"={MONTH_COLS[MONTH_COLS.index(col) - 1]}{co + 1}", total=False)
    close = mrow_("cash_close", s("xlsx.cash_close"), lambda col: f"={col}{co}+{col}{net}", True, total=False)
    r += 1
    ws.cell(r, 1, s("xlsx.lowest_cash")).font = BOLD
    ws.cell(r, 2, f"=MIN(B{close}:M{close})").number_format = LAKH_FMT
    ws.cell(r, 2).font = BOLD
    cm.scalars["lowest_cash"] = f"'{MONTHLY_SHEET}'!B{r}"
    r += 1
    ws.cell(r, 1, s("xlsx.lowest_cash_month")).font = BOLD
    ws.cell(r, 2, f"=INDEX(B{mrow}:M{mrow},MATCH(B{r-1},B{close}:M{close},0))")
    cm.scalars["lowest_cash_month"] = f"'{MONTHLY_SHEET}'!B{r}"
    ws.freeze_panes = "B5"


# ---------------------------------------------------------------- Schemes / Read me


def _write_schemes(ws, ev: Evaluation, s: Strings) -> None:
    ws.cell(1, 1, s("xlsx.schemes_title")).font = TITLE
    ws.cell(2, 1, ev.notice)
    ws.cell(3, 1, f"{s('xlsx.rules_version')}: {ev.rules_version} ({s('xlsx.as_of')} {ev.rules_as_of})")
    heads = [s("xlsx.scheme"), s("xlsx.verdict"), s("xlsx.benefit"), s("xlsx.subsidy_amt"), s("xlsx.open_points"), s("xlsx.source")]
    for i, h in enumerate(heads, 1):
        c = ws.cell(5, i, h)
        c.font, c.fill = BOLD, HEAD_FILL
    r = 6
    for x in ev.results:
        ws.cell(r, 1, x.name + (f"  [{s('xlsx.used_in_model')}]" if x.selected else ""))
        ws.cell(r, 2, s(f"verdict.{x.verdict.value}"))
        ws.cell(r, 3, x.benefit_text)
        if x.kind == "capital_subsidy":
            ws.cell(r, 4, round(x.subsidy_amount, 2)).number_format = LAKH_FMT
        pts = [f"✗ {y.text}: {y.detail}" for y in x.failed] + [f"? {y.text}: {y.detail}" for y in x.to_verify]
        ws.cell(r, 5, "\n".join(pts) or "-")
        ws.cell(r, 6, f"{x.source} ({x.as_of})")
        for col in range(1, 7):
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    for col, w in zip("ABCDEF", (40, 18, 42, 12, 70, 40)):
        ws.column_dimensions[col].width = w


def _write_readme(ws, p: Project, ev: Evaluation, s: Strings) -> None:
    ws.column_dimensions["A"].width = 110
    lines = [
        (f"{p.name}", TITLE),
        (s("disclaimer.review_required"), Font(bold=True, color="C00000")),
        (s("disclaimer.verify_schemes"), None),
        (s("disclaimer.numbers"), None),
        ("", None),
        (s("xlsx.readme_inputs"), None),
        (s("xlsx.readme_model"), None),
        (s("xlsx.readme_monthly"), None),
        (s("xlsx.readme_schemes"), None),
        (s("xlsx.readme_units"), None),
        ("", None),
        (f"{s('xlsx.rules_version')}: {ev.rules_version} ({s('xlsx.as_of')} {ev.rules_as_of})", None),
    ]
    for i, (text, font) in enumerate(lines, 1):
        c = ws.cell(i, 1, text)
        c.alignment = Alignment(wrap_text=True)
        if font:
            c.font = font


# ---------------------------------------------------------------- recalc + verify

SOFFICE_CANDIDATES = ["/opt/homebrew/bin/soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice", "soffice", "libreoffice"]


def find_soffice() -> str | None:
    env = os.environ.get("SOFFICE")
    for cand in ([env] if env else []) + SOFFICE_CANDIDATES:
        path = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if path:
            return path
    return None


def recalculate(path: Path, soffice: str | None = None, timeout: int = 180) -> Path:
    """Open the workbook in LibreOffice headless, recalculate, save back as .xlsx (in place)."""
    soffice = soffice or find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) not found; set SOFFICE=/path/to/soffice or use --no-recalc")
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        outdir = Path(tmp) / "out"
        outdir.mkdir()
        cmd = [soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
               "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(outdir), str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        produced = outdir / path.name
        if proc.returncode != 0 or not produced.exists():
            raise RuntimeError(f"LibreOffice recalculation failed: {proc.stderr or proc.stdout}")
        shutil.copyfile(produced, path)
    return path


def scan_errors(path: Path) -> list[str]:
    wb = load_workbook(path, data_only=True)
    bad = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith(ERROR_MARKERS):
                    bad.append(f"{ws.title}!{c.coordinate}: {c.value}")
    return bad


def missing_values(path: Path) -> list[str]:
    """Formula cells that have no cached value (i.e. were never calculated)."""
    wf = load_workbook(path)
    wv = load_workbook(path, data_only=True)
    out = []
    for ws in wf.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    v = wv[ws.title][c.coordinate].value
                    if v is None:
                        out.append(f"{ws.title}!{c.coordinate}")
    return out


def verify_against_model(path: Path, m: ModelResult, cm: CellMap, tol: float = 1e-6) -> list[str]:
    """Compare recalculated workbook values with the Python model. Returns mismatches."""
    wb = load_workbook(path, data_only=True)
    ws = wb["Model"]

    def val(ref):
        sheet, cell = ref.rsplit("!", 1)
        return wb[sheet.strip("'")][cell].value

    expect_scalar = {
        "project_cost": m.project_cost, "contingency": m.contingency, "own": m.finance.own,
        "subsidy": m.finance.subsidy, "gross_loan": m.finance.gross_loan, "net_loan": m.finance.net_loan,
        "min_dscr": m.min_dscr, "avg_dscr": m.avg_dscr,
        "payback_year": m.payback_year if m.payback_year is not None else "> 5",
        "breakeven": m.breakeven_units_per_day,
        "lowest_cash": m.lowest_cash[0] if m.lowest_cash else None,
        "lowest_cash_month": m.lowest_cash[1] if m.lowest_cash else None,
    }
    problems = []
    for k, exp in expect_scalar.items():
        if k not in cm.scalars or exp is None:
            continue
        got = val(cm.scalars[k])
        if not _close(got, exp, tol):
            problems.append(f"{k}: workbook {got!r} vs model {exp!r}")

    attr = {
        "volume_factor": "volume_factor", "price_factor": "price_factor", "revenue": "revenue", "cogs": "cogs",
        "gross_profit": "gross_profit", "staff": "staff", "rent_share": "rent_share", "electricity": "electricity",
        "running_costs": "running_costs", "ebitda": "ebitda", "depreciation": "depreciation", "loan_open": "loan_open",
        "interest": "interest", "principal": "principal", "loan_close": "loan_close", "pbt": "pbt", "tax": "tax",
        "net_profit": "net_profit", "cash_after_debt": "cash_after_debt", "cumulative_cash": "cumulative_cash",
        "dscr": "dscr",
    }
    for key, row in cm.rows.items():
        for i, col in enumerate(YEAR_COLS):
            y = m.years[i]
            if key.startswith("rev:"):
                exp = y.revenue_by_stream[key[4:]]
            elif key.startswith("other:"):
                exp = y.other_by_name[key[6:]]
            else:
                exp = getattr(y, attr[key])
            got = ws[f"{col}{row}"].value
            if exp is None:
                if got not in (None, ""):
                    problems.append(f"{key} Y{i+1}: workbook {got!r} vs model blank")
            elif not _close(got, exp, tol):
                problems.append(f"{key} Y{i+1}: workbook {got!r} vs model {exp!r}")
    wmo = wb[MONTHLY_SHEET]
    for key, row in cm.monthly.items():
        for i, col in enumerate(MONTH_COLS):
            mo = m.monthly[i]
            exp = mo.revenue_by_stream[key[4:]] if key.startswith("rev:") else getattr(mo, key)
            got = wmo[f"{col}{row}"].value
            if not _close(got, exp, tol):
                problems.append(f"monthly {key} M{i+1}: workbook {got!r} vs model {exp!r}")
    return problems


def _close(got, exp, tol) -> bool:
    if isinstance(exp, str):
        return str(got).strip() == exp
    try:
        return abs(float(got) - float(exp)) <= tol * max(1.0, abs(float(exp)))
    except (TypeError, ValueError):
        return False


@dataclass
class WorkbookReport:
    path: Path
    recalculated: bool
    errors: list[str]
    uncalculated: list[str]
    mismatches: list[str]

    @property
    def ok(self) -> bool:
        return self.recalculated and not self.errors and not self.uncalculated and not self.mismatches


def write_workbook(path: Path, p: Project, m: ModelResult, ev: Evaluation, s: Strings, recalc: bool = True) -> WorkbookReport:
    wb, cm = build_workbook(p, m, ev, s)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    if not recalc:
        return WorkbookReport(path, False, [], [], [])
    recalculate(path)
    return WorkbookReport(path, True, scan_errors(path), missing_values(path), verify_against_model(path, m, cm))
