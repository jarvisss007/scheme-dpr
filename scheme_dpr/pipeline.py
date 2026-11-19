"""End-to-end run: project YAML -> scheme check -> model -> workbook -> DPR."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .eligibility import Evaluation, evaluate, load_rules
from .excel import WorkbookReport, write_workbook
from .finance import ModelResult, build_model, project_cost
from .i18n import Strings
from .models import Project
from .narrative import Narrative, make_narrative
from .report import Report, build_report, to_docx, to_markdown


@dataclass
class RunResult:
    project: Project
    evaluation: Evaluation
    model: ModelResult
    narrative: Narrative
    report: Report
    outputs: dict[str, Path] = field(default_factory=dict)
    workbook: WorkbookReport | None = None
    warnings: list[str] = field(default_factory=list)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "project"


def analyse(p: Project, rules_path: str | Path | None = None, run_date: date | None = None) -> tuple[Evaluation, ModelResult]:
    rules = load_rules(rules_path)
    cost = project_cost(p)[3]
    ev = evaluate(p, cost, rules, run_date)
    sel = ev.selected
    own = p.finance.own_contribution_pct
    if own is None:
        own = sel.own_pct if sel and sel.own_pct is not None else 0.10
    if sel and own < (sel.own_pct or 0) - 1e-9:
        raise ValueError(f"own contribution {own:.0%} is below the {sel.name} minimum of {sel.own_pct:.0%}")
    m = build_model(p, own, sel.subsidy_pct if sel else 0.0, sel.subsidy_cap if sel else None)
    return ev, m


def run(p: Project, out_dir: Path, *, use_llm: bool = True, recalc: bool = True, docx: bool = True,
        rules_path: str | Path | None = None, run_date: date | None = None) -> RunResult:
    s = Strings(p.language)
    ev, m = analyse(p, rules_path, run_date)
    narr = make_narrative(p, m, ev, s, use_llm=use_llm)
    rep = build_report(p, m, ev, narr, s, run_date)
    res = RunResult(p, ev, m, narr, rep, warnings=list(narr.warnings))

    out_dir.mkdir(parents=True, exist_ok=True)
    base = slug(p.name)

    xlsx = out_dir / f"{base}_model.xlsx"
    res.workbook = write_workbook(xlsx, p, m, ev, s, recalc=recalc)
    res.outputs["xlsx"] = xlsx

    md = out_dir / f"{base}_DPR.md"
    md.write_text(to_markdown(rep), encoding="utf-8")
    res.outputs["md"] = md

    if docx:
        res.outputs["docx"] = to_docx(rep, out_dir / f"{base}_DPR.docx", out_dir / f"{base}_DPR.blocks.json")

    schemes = out_dir / f"{base}_schemes.json"
    schemes.write_text(json.dumps(schemes_json(ev, m), ensure_ascii=False, indent=2), encoding="utf-8")
    res.outputs["schemes"] = schemes
    return res


def schemes_json(ev: Evaluation, m: ModelResult) -> dict:
    return {
        "notice": ev.notice,
        "human_review_required": True,
        "rules_version": ev.rules_version,
        "rules_as_of": ev.rules_as_of,
        "project_cost_lakh": round(m.project_cost, 4),
        "selected_scheme": ev.selected.id if ev.selected else None,
        "schemes": [
            {
                "id": r.id, "name": r.name, "kind": r.kind, "verdict": r.verdict.value, "selected": r.selected,
                "benefit": r.benefit_text, "subsidy_pct": r.subsidy_pct if r.kind == "capital_subsidy" else None,
                "subsidy_cap_lakh": r.subsidy_cap, "subsidy_amount_lakh": round(r.subsidy_amount, 4) if r.kind == "capital_subsidy" else None,
                "own_contribution_pct": r.own_pct, "verify_with": "DIC / implementing agency / bank",
                "source": r.source, "as_of": r.as_of,
                "rules": [{"id": x.id, "text": x.text, "status": x.status.value, "detail": x.detail,
                           "source": x.source, "as_of": x.as_of, "confidence": x.confidence} for x in r.rules],
            }
            for r in ev.results
        ],
    }
