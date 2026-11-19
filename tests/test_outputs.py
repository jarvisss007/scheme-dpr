"""Workbook, report, narrative guard, draft step, i18n and CLI."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

from scheme_dpr import cli
from scheme_dpr.draft import to_project_dict
from scheme_dpr.excel import build_workbook, find_soffice, write_workbook
from scheme_dpr.i18n import Strings
from scheme_dpr.models import Project
from scheme_dpr.narrative import check_numbers, claude_narrative, facts, make_narrative, template_narrative
from scheme_dpr.pipeline import analyse, run
from scheme_dpr.report import build_report, find_node, rupees, to_markdown

from .conftest import ROOT, RUN_DATE

needs_soffice = pytest.mark.skipif(find_soffice() is None, reason="LibreOffice not installed")
needs_node = pytest.mark.skipif(find_node() is None or shutil.which("npm") is None, reason="node/npm not installed")

EN = Strings("en")


@pytest.fixture
def analysed(example):
    ev, m = analyse(example, run_date=RUN_DATE)
    return example, ev, m


# ---------------------------------------------------------------- workbook


def test_workbook_has_live_formulas_and_blue_inputs(analysed):
    p, ev, m = analysed
    wb, cm = build_workbook(p, m, ev, EN)
    assert wb.sheetnames == ["Read me", "Inputs", "Model", "Year 1 monthly", "Schemes"]
    assert str(wb["Year 1 monthly"][f"F{cm.monthly['cash_close']}"].value).startswith("=")
    model = wb["Model"]
    for key in ("project_cost", "own", "subsidy", "net_loan", "min_dscr", "payback_year", "breakeven"):
        assert str(model[cm.scalars[key].split("!")[1]].value).startswith("="), key
    for key in ("revenue", "ebitda", "interest", "principal", "dscr", "cumulative_cash"):
        assert str(model[f"C{cm.rows[key]}"].value).startswith("="), key
    inputs = wb["Inputs"]
    blue = [c for row in inputs.iter_rows() for c in row if c.font and c.font.color and c.font.color.rgb == "000000FF"]
    assert len(blue) >= 30
    assert all(not str(c.value).startswith("=") for c in blue)  # inputs are values, never formulas
    assert "subsidy_pct" in wb.defined_names


@needs_soffice
@pytest.mark.libreoffice
@pytest.mark.parametrize("variant", [{}, {"finance": {"moratorium_months": 6, "tenor_years": 7}}])
def test_recalculated_workbook_matches_model(tmp_path, example_data, variant):
    data = {**example_data, **{k: {**example_data[k], **v} for k, v in variant.items()}}
    p = Project.model_validate(data)
    ev, m = analyse(p, run_date=RUN_DATE)
    rep = write_workbook(tmp_path / "m.xlsx", p, m, ev, EN, recalc=True)
    assert rep.errors == []
    assert rep.uncalculated == []
    assert rep.mismatches == []
    assert rep.ok
    v = load_workbook(tmp_path / "m.xlsx", data_only=True)["Model"]
    assert any(c.value == pytest.approx(19.8875) for row in v.iter_rows() for c in row if isinstance(c.value, float))


# ---------------------------------------------------------------- report


SECTIONS = ["1. Project at a glance", "2. Promoter details", "3. Project description and location", "4. Market potential",
            "5. Project cost and means of finance", "6. Operations", "7. Five-year financial projections", "8. Employment",
            "9. Implementation schedule", "10. Risks and mitigation", "11. Documents checklist", "12. Declaration",
            "Annex A: Government scheme eligibility check"]


def test_markdown_sections_in_pmegp_order(analysed):
    p, ev, m = analysed
    md = to_markdown(build_report(p, m, ev, template_narrative(p, m, ev, EN), EN, RUN_DATE))
    positions = [md.index(f" {h}\n") for h in SECTIONS]
    assert positions == sorted(positions)
    assert "DRAFT FOR HUMAN REVIEW" in md
    assert "19.89" in md and "1.28x" in md and "below the usual bank norm" in md
    assert "Year 1 month by month" in md and "Cash runs short from month 2 to month 8" in md
    assert "[ to be filled ]" in md  # promoter placeholders


def test_report_numbers_come_from_model(analysed):
    p, ev, m = analysed
    md = to_markdown(build_report(p, m, ev, template_narrative(p, m, ev, EN), EN, RUN_DATE))
    assert f"{m.years[2].revenue:,.2f}" in md
    assert f"{m.finance.net_loan:,.2f}" in md


def test_indian_rupee_grouping():
    assert rupees(1989875) == "₹19,89,875"
    assert rupees(15000) == "₹15,000"
    assert rupees(999) == "₹999"
    assert rupees(-1234567) == "-₹12,34,567"


@needs_node
def test_docx_is_built(tmp_path, example):
    res = run(example, tmp_path, use_llm=False, recalc=False, run_date=RUN_DATE)
    docx = res.outputs["docx"]
    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "Project at a glance" in xml and "Declaration" in xml
    assert "DRAFT FOR HUMAN REVIEW" in xml


# ---------------------------------------------------------------- narrative guard


def test_check_numbers_flags_invented_figures():
    src = json.dumps({"per_day": "120", "cost": "19.89", "road": "NH-48"})
    assert check_numbers("Serves 120 meals a day on NH-48.", src) == []
    assert check_numbers("About 5,000 trucks pass daily.", src) == ["5000"]
    assert check_numbers("Year 3 and two shifts.", src) == []  # small counts allowed


def test_claude_section_with_invented_number_is_replaced(analysed):
    p, ev, m = analysed

    def fake(system, user, schema):
        assert "Facts (JSON)" in user and "19.89" in user
        return {"description": "A kiosk on NH-48 near Ranebennur serving truckers.",
                "market": "Around 8,000 vehicles pass every day.",  # not in the facts
                "operations": "Run daily by the promoter with hired staff.",
                "risks": [{"risk": "Demand may be lower.", "mitigation": "Track daily sales."},
                          {"risk": "Fuel prices rise 40%.", "mitigation": "Reprice."}]}

    n = claude_narrative(p, m, ev, EN, call=fake)
    assert n.description.startswith("A kiosk")
    assert n.market == template_narrative(p, m, ev, EN).market
    assert len(n.risks) == 1
    assert n.source == "mixed"
    assert any("market" in w for w in n.warnings)


def test_claude_failure_falls_back_to_templates(analysed):
    p, ev, m = analysed

    def boom(*a):
        raise RuntimeError("Claude API unreachable")

    n = claude_narrative(p, m, ev, EN, call=boom)
    assert n.source == "template"
    assert "unreachable" in n.warnings[0]


def test_no_llm_never_calls_api(analysed, monkeypatch):
    p, ev, m = analysed
    monkeypatch.setattr("scheme_dpr.narrative.call_claude_json", lambda *a: pytest.fail("API called"))
    assert make_narrative(p, m, ev, EN, use_llm=False).source == "template"


def test_facts_contain_no_scheme_terms(analysed):
    """Scheme rules must not leak into prompts; only the chosen scheme's name is passed."""
    p, ev, m = analysed
    text = json.dumps(facts(p, m, ev))
    assert "25%" not in text and "subsidy_pct" not in text


# ---------------------------------------------------------------- draft


def test_draft_mapping_marks_ai_assumptions():
    raw = {
        "name": "Bakery", "idea_summary": "Small bakery.", "sector": "manufacturing", "tags": ["bakery", "food_processing"],
        "nic_description": "10711", "site_description": "Rented shop",
        "setup_items": [{"name": "Oven", "amount_lakh": 3.0, "stated_by_promoter": True, "basis": "quote"},
                        {"name": "Mixer", "amount_lakh": 1.0, "stated_by_promoter": False, "basis": "typical"}],
        "contingency_pct": 0.05, "working_capital_lakh": 1.0, "working_capital_stated": False,
        "revenue": [{"name": "Bread (loaves)", "kind": "per_day", "per_day": 200, "price_rs": 40, "annual_lakh": 0,
                     "cogs_pct": 0.55, "primary": True, "stated_by_promoter": True, "basis": ""}],
        "staff_count": 3, "staff_monthly_lakh": 0.12, "rent_annual_lakh": 1.2, "revenue_share_pct": 0,
        "kwh_per_day": 40, "power_rate_rs": 9, "other_costs": [], "market_notes": "", "open_questions": ["Own site?"],
    }
    data, assumed = to_project_dict(raw, "Dharwad", "urban", ["women"], None)
    p = Project.model_validate(data)
    assert p.applicant.categories == ["women"]
    assert any("Mixer" in a for a in assumed) and any("working capital" in a for a in assumed)
    assert not any("Oven" in a for a in assumed)


# ---------------------------------------------------------------- i18n


def test_kannada_falls_back_to_english():
    kn = Strings("kn")
    assert kn("dpr.doc_type") != EN("dpr.doc_type")
    assert kn("dpr.s1") == EN("dpr.s1")
    assert "dpr.s1" in kn.missing_keys()
    assert "dpr.doc_type" not in kn.missing_keys()


def test_kannada_report_renders(analysed):
    p, ev, m = analysed
    kn = Strings("kn")
    md = to_markdown(build_report(p, m, ev, template_narrative(p, m, ev, kn), kn, RUN_DATE))
    assert "ವಿವರವಾದ ಯೋಜನಾ ವರದಿ" in md


# ---------------------------------------------------------------- CLI


def test_cli_run(tmp_path, capsys):
    code = cli.main(["run", str(ROOT / "example.yaml"), "--out", str(tmp_path), "--no-llm", "--no-recalc", "--no-docx",
                     "--date", "2026-11-18"])
    out = capsys.readouterr().out
    assert code == 0
    assert "HUMAN REVIEW REQUIRED" in out
    assert "PMEGP" in out and "used in the model" in out
    files = {f.suffix for f in tmp_path.iterdir()}
    assert {".xlsx", ".md", ".json"} <= files
    sj = json.loads(next(tmp_path.glob("*_schemes.json")).read_text())
    assert sj["selected_scheme"] == "pmegp" and sj["human_review_required"] is True
    assert all(s["verify_with"] for s in sj["schemes"])


def test_cli_reports_bad_input(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text((ROOT / "example.yaml").read_text().replace("area: rural", "area: suburban"))
    assert cli.main(["schemes", str(bad)]) == 2
    assert "Input error" in capsys.readouterr().err
