"""Build the Detailed Project Report (DPR) as a neutral list of blocks, then render
it to Markdown (here) or .docx (``render/build_docx.js``).

Section order follows the usual PMEGP / bank DPR layout. Every number in the
report is formatted here from the Python model; narrative text (from Claude or
from templates) is slotted in but never supplies figures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from .eligibility import Evaluation, Status
from .finance import ModelResult, sensitivity
from .i18n import Strings
from .models import Project
from .narrative import Narrative

Block = dict[str, Any]


# ---------------------------------------------------------------- formatting


def lakh(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "-"
    return f"{x:,.{nd}f}"


def pct(x: float | None, nd: int = 0) -> str:
    return "-" if x is None else f"{x * 100:.{nd}f}%"


def times(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}x"


def rupees(x: float) -> str:
    """Indian digit grouping: 1989875 -> 19,89,875."""
    neg = x < 0
    s = f"{abs(round(x)):d}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return ("-" if neg else "") + "₹" + s


# ---------------------------------------------------------------- blocks


def H(text: str, level: int = 1) -> Block:
    return {"type": "heading", "level": level, "text": text}


def P(text: str, style: str = "normal") -> Block:
    return {"type": "para", "text": text, "style": style}


def T(header: list[str], rows: list[list[str]], align: list[str] | None = None, bold_rows: list[int] | None = None) -> Block:
    return {"type": "table", "header": header, "rows": rows, "align": align or ["left"] + ["right"] * (len(header) - 1),
            "bold_rows": bold_rows or []}


def KV(rows: list[tuple[str, str]]) -> Block:
    return {"type": "table", "header": [], "rows": [list(r) for r in rows], "align": ["left", "left"], "bold_rows": [],
            "key_value": True}


def BUL(items: list[str]) -> Block:
    return {"type": "bullets", "items": items}


def CHECK(items: list[str]) -> Block:
    return {"type": "checklist", "items": items}


def BREAK() -> Block:
    return {"type": "pagebreak"}


@dataclass
class Report:
    title: str
    blocks: list[Block]
    lang: str


# ---------------------------------------------------------------- builder


def build_report(p: Project, m: ModelResult, ev: Evaluation, narr: Narrative, s: Strings,
                 run_date: date | None = None) -> Report:
    run_date = run_date or date.today()
    sel = ev.selected
    a, f, c = p.assumptions, p.finance, p.costs
    loc = p.location
    place = ", ".join(x for x in [loc.village_or_town, loc.taluk, loc.district, loc.state] if x)
    applicant = p.applicant.name or s("dpr.placeholder")
    cats = ", ".join(s(f"category.{x}") for x in p.applicant.categories)
    prim = p.primary_stream
    y = m.years
    blocks: list[Block] = []

    # --- cover
    blocks += [
        P(s("dpr.doc_type"), "subtitle"),
        H(p.name, 0),
        P(s("dpr.cover_line", place=place, date=run_date.strftime("%d %B %Y"))),
        P(s("disclaimer.review_required"), "warning"),
        P(s("disclaimer.verify_schemes"), "warning"),
        P(s("disclaimer.numbers"), "note"),
    ]
    if narr.source != "claude":
        blocks.append(P(s("disclaimer.template_narrative"), "note"))
    blocks.append(BREAK())

    # --- 1. at a glance
    scheme_line = (f"{sel.name} - {pct(sel.subsidy_pct)} ({s('dpr.group.' + sel.group)}, {s('area.' + loc.area.value)})"
                   if sel else s("dpr.no_scheme"))
    blocks += [H(s("dpr.s1")), KV([
        (s("dpr.f.project"), p.name),
        (s("dpr.f.promoter"), applicant),
        (s("dpr.f.category"), cats),
        (s("dpr.f.activity"), f"{s('sector.' + p.activity.sector.value).capitalize()}" + (f" - {p.activity.nic_description}" if p.activity.nic_description else "")),
        (s("dpr.f.location"), f"{place} ({s('area.' + loc.area.value)})"),
        (s("dpr.f.project_cost"), f"₹{lakh(m.project_cost)} lakh"),
        (s("dpr.f.scheme"), scheme_line),
        (s("dpr.f.own"), f"₹{lakh(m.finance.own)} lakh ({pct(m.finance.own_pct)})"),
        (s("dpr.f.subsidy"), f"₹{lakh(m.finance.subsidy)} lakh"),
        (s("dpr.f.loan"), f"₹{lakh(m.finance.gross_loan)} lakh " + s("dpr.f.loan_terms", rate=pct(f.interest_rate, 1), tenor=f.tenor_years)),
        (s("dpr.f.employment"), str(p.employment_count)),
        (s("dpr.f.sales_y3"), f"₹{lakh(y[2].revenue)} lakh"),
        (s("dpr.f.dscr"), s("dpr.f.dscr_val", avg=times(m.avg_dscr), min=times(m.min_dscr))),
        (s("dpr.f.payback"), s("dpr.year_n", n=m.payback_year) if m.payback_year else s("dpr.beyond5")),
        (s("dpr.f.breakeven"), s("dpr.f.breakeven_val", n=f"{m.breakeven_units_per_day:.0f}", unit=prim.name.lower(),
                                  planned=f"{prim.per_day:g}") if prim and m.breakeven_units_per_day else "-"),
    ])]

    # --- 2. promoter
    ph = s("dpr.placeholder")
    blocks += [H(s("dpr.s2")), P(s("dpr.s2_intro"), "note"), KV([
        (s("dpr.p.name"), applicant),
        (s("dpr.p.relation"), ph),
        (s("dpr.p.dob"), f"{p.applicant.age} {s('dpr.p.years')}" if p.applicant.age else ph),
        (s("dpr.p.address"), ph),
        (s("dpr.p.phone"), ph),
        (s("dpr.p.category"), cats),
        (s("dpr.p.education"), p.applicant.education or ph),
        (s("dpr.p.experience"), ph),
        (s("dpr.p.constitution"), s("dpr.p.proprietorship")),
        (s("dpr.p.udyam"), s("yes_word") if p.applicant.udyam_registered else s("dpr.p.udyam_todo")),
        (s("dpr.p.bank"), ph),
    ])]

    # --- 3. description and location
    blocks += [H(s("dpr.s3")), P(narr.description)]
    if loc.site_description:
        blocks.append(P(f"{s('dpr.site')}: {loc.site_description}"))

    # --- 4. market
    blocks += [H(s("dpr.s4")), P(narr.market)]

    # --- 5. cost and means of finance
    rows = [[n, lakh(v)] for n, v in m.setup_items if abs(v) > 1e-12]
    rows.append([f"{s('xlsx.contingency')} ({pct(p.setup_cost.contingency_pct)})", lakh(m.contingency)])
    rows.append([s("xlsx.working_capital"), lakh(m.working_capital)])
    rows.append([s("xlsx.total_cost"), lakh(m.project_cost)])
    blocks += [H(s("dpr.s5")), H(s("dpr.cost_table_title"), 2), T([s("dpr.item"), "₹ lakh"], rows, bold_rows=[len(rows) - 1])]
    zero_items = [n for n, v in m.setup_items if abs(v) <= 1e-12]
    if zero_items:
        blocks.append(P(s("dpr.zero_items", items=", ".join(zero_items)), "note"))
    mof = m.finance
    mrows = [
        [s("xlsx.own"), lakh(mof.own), pct(mof.own / m.project_cost, 1)],
        [s("dpr.bank_loan_gross"), lakh(mof.gross_loan), pct(mof.gross_loan / m.project_cost, 1)],
        [s("xlsx.total_cost"), lakh(m.project_cost), "100.0%"],
    ]
    blocks += [H(s("dpr.mof_title"), 2), T([s("dpr.source"), "₹ lakh", s("dpr.share")], mrows, bold_rows=[2])]
    if sel:
        blocks.append(P(s("dpr.subsidy_line", scheme=sel.name, pct=pct(mof.subsidy_pct), amt=lakh(mof.subsidy),
                          net=lakh(mof.net_loan))))
        blocks.append(BUL(sel.notes))
    else:
        blocks.append(P(s("dpr.no_scheme_line")))

    # --- 6. operations
    vol_rows = []
    for st in p.revenue:
        if st.is_volume:
            vol_rows.append([st.name, f"{st.per_day:g}", f"₹{st.price_rs:g}", pct(st.cogs_pct)])
        else:
            vol_rows.append([st.name, "-", f"₹{lakh(st.annual_lakh)} lakh / {s('dpr.yr')}", "-"])
    util = [
        [s("dpr.ops.staff"), s("dpr.ops.staff_val", n=c.staff.count, cost=rupees(c.staff.monthly_lakh * 1e5))],
        [s("dpr.ops.power"), s("dpr.ops.power_val", kwh=f"{c.electricity.kwh_per_day:g}", rate=f"{c.electricity.rate_rs:g}")],
    ]
    if c.revenue_share_pct:
        util.append([s("dpr.ops.revshare"), pct(c.revenue_share_pct)])
    if c.rent_annual_lakh:
        util.append([s("xlsx.rent_annual"), f"₹{lakh(c.rent_annual_lakh)} lakh"])
    blocks += [H(s("dpr.s6")), P(narr.operations), H(s("dpr.capacity"), 2),
               T([s("dpr.stream"), s("dpr.per_day"), s("dpr.price"), s("dpr.cogs")], vol_rows),
               P(s("dpr.ramp_line", r1=pct(a.ramp_year1), r2=pct(a.ramp_year2), g=pct(a.growth_after_year3), i=pct(a.inflation))),
               H(s("dpr.utilities"), 2), KV([tuple(r) for r in util])]  # type: ignore[misc]

    # --- 7. projections
    yh = [s("dpr.particulars")] + [s("dpr.year_n", n=i + 1) for i in range(len(y))]

    def row(lbl, attr, fmt=lakh):
        return [lbl] + [fmt(getattr(r, attr)) for r in y]

    prow = [[st.name] + [lakh(r.revenue_by_stream[st.name]) for r in y] for st in p.revenue]
    pl = prow + [
        row(s("xlsx.total_revenue"), "revenue"),
        row(s("xlsx.cogs"), "cogs"),
        row(s("xlsx.gross_profit"), "gross_profit"),
        row(s("xlsx.staff"), "staff"),
        row(s("xlsx.rent_share"), "rent_share"),
        row(s("xlsx.electricity"), "electricity"),
    ] + [[o.name] + [lakh(r.other_by_name[o.name]) for r in y] for o in c.other] + [
        row(s("xlsx.running_total"), "running_costs"),
        row("EBITDA", "ebitda"),
        row(s("xlsx.depreciation"), "depreciation"),
        row(s("xlsx.interest"), "interest"),
        row(s("xlsx.pbt"), "pbt"),
        row(s("xlsx.tax"), "tax"),
        row(s("xlsx.net_profit"), "net_profit"),
    ]
    n0 = len(prow)
    bold = [n0, n0 + 2, n0 + 6 + len(c.other), n0 + 7 + len(c.other), n0 + 12 + len(c.other)]
    loan = [
        row(s("xlsx.loan_open"), "loan_open"), row(s("xlsx.interest"), "interest"),
        row(s("xlsx.principal"), "principal"), row(s("xlsx.loan_close"), "loan_close"),
    ]
    cover = [
        row(s("xlsx.cash_after_debt"), "cash_after_debt"),
        row(s("xlsx.cumulative"), "cumulative_cash"),
        row(s("dpr.dscr_row"), "dscr", times),
    ]
    blocks += [H(s("dpr.s7")), P(s("dpr.s7_intro")),
               H(s("dpr.pl_title"), 2), T(yh, pl, bold_rows=bold),
               H(s("dpr.loan_title"), 2), P(s("dpr.loan_basis", net=lakh(mof.net_loan), rate=pct(f.interest_rate, 1), tenor=f.tenor_years, mor=f.moratorium_months)),
               T(yh, loan),
               H(s("dpr.cover_title"), 2), T(yh, cover, bold_rows=[2]),
               KV([
                   (s("dpr.f.dscr"), s("dpr.f.dscr_val", avg=times(m.avg_dscr), min=times(m.min_dscr))),
                   (s("dpr.bank_norm"), times(a.min_dscr)),
                   (s("dpr.f.payback"), s("dpr.year_n", n=m.payback_year) if m.payback_year else s("dpr.beyond5")),
                   (s("dpr.f.breakeven"), s("dpr.f.breakeven_val", n=f"{m.breakeven_units_per_day:.0f}", unit=prim.name.lower(),
                                             planned=f"{prim.per_day:g}") if prim and m.breakeven_units_per_day else "-"),
               ]),
               P(s("dpr.breakeven_basis"), "note")]
    sens = sensitivity(p, m.finance.own_pct, m.finance.subsidy_pct, m.finance.subsidy_cap)
    blocks += [H(s("dpr.sens_title"), 2), P(s("dpr.sens_intro"), "note"),
               T([s("dpr.scenario"), s("dpr.sens_ebitda"), s("dpr.sens_min_dscr"), s("dpr.sens_avg_dscr"), s("dpr.f.payback")],
                 [[s(f"dpr.sens.{x.key}"), lakh(x.year3_ebitda), times(x.min_dscr), times(x.avg_dscr),
                   s("dpr.year_n", n=x.payback_year) if x.payback_year else s("dpr.beyond5")] for x in sens],
                 bold_rows=[0])]
    mo = m.monthly
    blocks += [H(s("dpr.monthly_title"), 2), P(s("dpr.monthly_intro"), "note"),
               T([s("dpr.month"), s("dpr.m.sales"), s("dpr.m.materials"), s("dpr.m.running"), s("xlsx.interest"),
                  s("xlsx.principal"), s("dpr.m.net"), s("dpr.m.cash")],
                 [[str(r.month), lakh(r.revenue), lakh(r.cogs), lakh(r.running_costs), lakh(r.interest),
                   lakh(r.principal), lakh(r.net_cash), lakh(r.cash_close)] for r in mo])]
    short = [r.month for r in mo if r.cash_close < 0]
    if short:
        low, when = m.lowest_cash  # type: ignore[misc]
        blocks.append(P(s("dpr.cash_gap", first=short[0], last=short[-1], low=lakh(-low), month=when), "warning"))
    below = [r.year for r in y if r.dscr is not None and r.dscr < a.min_dscr]
    if below:
        blocks.append(P(s("dpr.dscr_warning", years=", ".join(str(x) for x in below), norm=times(a.min_dscr)), "warning"))

    # --- 8. employment
    blocks += [H(s("dpr.s8")), T([s("dpr.role"), s("dpr.number")], [
        [s("dpr.emp.promoter"), "1"],
        [s("dpr.emp.staff"), str(c.staff.count)],
        [s("dpr.emp.total"), str(p.employment_count)],
    ], bold_rows=[2])]

    # --- 9. implementation schedule
    sched = s.get("schedule")
    blocks += [H(s("dpr.s9")), T([s("dpr.activity"), s("dpr.month")], [[x["task"], x["month"]] for x in sched], align=["left", "left"])]

    # --- 10. risks
    blocks += [H(s("dpr.s10")), H(s("dpr.risks_computed"), 2), BUL(computed_risks(p, m, ev, s)),
               H(s("dpr.risks_business"), 2),
               T([s("dpr.risk"), s("dpr.mitigation")], [[r.risk, r.mitigation] for r in narr.risks], align=["left", "left"])]

    # --- 11. documents
    docs = list(s.get("documents_common"))
    if sel:
        docs += [d for d in sel.documents if d not in docs]
    blocks += [H(s("dpr.s11")), CHECK(docs)]

    # --- 12. declaration
    blocks += [H(s("dpr.s12")), P(s("dpr.declaration", name=applicant)),
               KV([(s("dpr.place"), ph), (s("dpr.date"), ph), (s("dpr.signature"), ph)])]

    # --- annex: scheme check
    blocks += [BREAK(), H(s("dpr.annex_a")), P(ev.notice, "warning"),
               P(s("dpr.rules_line", v=ev.rules_version, d=ev.rules_as_of), "note")]
    blocks.append(T([s("xlsx.scheme"), s("xlsx.verdict"), s("xlsx.benefit")],
                    [[x.name + (" ★" if x.selected else ""), s(f"verdict.{x.verdict.value}"), x.benefit_text] for x in ev.results],
                    align=["left", "left", "left"]))
    for x in ev.results:
        blocks.append(H(x.name, 3))
        blocks.append(T([s("dpr.rule"), s("dpr.result"), s("dpr.detail"), s("dpr.source")],
                        [[r.text, s(f"status.{r.status.value}"), r.detail, f"{r.source} ({r.as_of}, {s('confidence.' + r.confidence)})"]
                         for r in x.rules], align=["left"] * 4))
    blocks.append(P(s("dpr.annex_footer"), "note"))
    return Report(p.name, blocks, s.lang)


def computed_risks(p: Project, m: ModelResult, ev: Evaluation, s: Strings) -> list[str]:
    """Risks that follow from the numbers and the scheme check - written by code, not the model."""
    out = []
    a = p.assumptions
    for r in m.years:
        if r.dscr is not None and r.dscr < a.min_dscr:
            out.append(s("risk.dscr_low", year=r.year, dscr=times(r.dscr), norm=times(a.min_dscr)))
    prim = p.primary_stream
    if prim and m.breakeven_units_per_day:
        cushion = 1 - m.breakeven_units_per_day / prim.per_day  # type: ignore[operator]
        out.append(s("risk.breakeven", n=f"{m.breakeven_units_per_day:.0f}", unit=prim.name.lower(),
                     planned=f"{prim.per_day:g}", cushion=pct(cushion)))
    y3 = m.years[2]
    top = max(p.revenue, key=lambda st: y3.revenue_by_stream[st.name])
    share = y3.revenue_by_stream[top.name] / y3.revenue if y3.revenue else 0
    if share > 0.6:
        out.append(s("risk.concentration", stream=top.name, share=pct(share)))
    if p.costs.revenue_share_pct > 0:
        out.append(s("risk.landlord", pct=pct(p.costs.revenue_share_pct)))
    if m.payback_year is None:
        out.append(s("risk.no_payback"))
    if m.lowest_cash and m.lowest_cash[0] < 0:
        short = [r.month for r in m.monthly if r.cash_close < 0]
        out.append(s("risk.cash_gap", first=short[0], last=short[-1], low=lakh(-m.lowest_cash[0]), month=m.lowest_cash[1]))
    if ev.selected:
        for r in ev.selected.to_verify:
            out.append(s("risk.scheme_open", scheme=ev.selected.name, text=r.text, detail=r.detail))
    else:
        out.append(s("risk.no_scheme"))
    return out


# ---------------------------------------------------------------- renderers


def to_markdown(rep: Report) -> str:
    out: list[str] = []
    for b in rep.blocks:
        t = b["type"]
        if t == "heading":
            out.append("#" * max(1, b["level"] + (1 if b["level"] else 0)) + " " + b["text"])
        elif t == "para":
            text = b["text"]
            if b["style"] == "warning":
                text = f"> **{text}**"
            elif b["style"] == "note":
                text = f"> {text}"
            elif b["style"] == "subtitle":
                text = f"*{text}*"
            out.append(text)
        elif t == "bullets":
            out.append("\n".join(f"- {i}" for i in b["items"]))
        elif t == "checklist":
            out.append("\n".join(f"- [ ] {i}" for i in b["items"]))
        elif t == "table":
            out.append(_md_table(b))
        elif t == "pagebreak":
            out.append("---")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _md_table(b: Block) -> str:
    def esc(x: str) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ")

    header = b["header"] or ["", ""]
    rows = b["rows"]
    bold = set(b.get("bold_rows", []))
    sep = ["---:" if a == "right" else "---" for a in b["align"]]
    lines = ["| " + " | ".join(esc(h) for h in header) + " |", "| " + " | ".join(sep) + " |"]
    for i, r in enumerate(rows):
        cells = [esc(c) for c in r]
        if i in bold:
            cells = [f"**{c}**" if c else c for c in cells]
        if b.get("key_value"):
            cells[0] = f"**{cells[0]}**"
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def find_node() -> str | None:
    return os.environ.get("NODE") or shutil.which("node") or ("/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else None)


def node_module_path() -> str | None:
    if os.environ.get("NODE_PATH"):
        return os.environ["NODE_PATH"]
    npm = shutil.which("npm")
    if npm:
        try:
            return subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def to_docx(rep: Report, out_path: Path, json_path: Path | None = None) -> Path:
    node = find_node()
    if not node:
        raise RuntimeError("Node.js not found; install node and the 'docx' package (npm i -g docx)")
    script = Path(str(resources.files("scheme_dpr") / "render" / "build_docx.js"))
    json_path = json_path or out_path.with_suffix(".blocks.json")
    json_path.write_text(json.dumps({"title": rep.title, "lang": rep.lang, "blocks": rep.blocks}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    env = dict(os.environ)
    mp = node_module_path()
    if mp:
        env["NODE_PATH"] = mp
    proc = subprocess.run([node, str(script), str(json_path), str(out_path)], capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"docx build failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return out_path
