"""Command line: ``python -m scheme_dpr run example.yaml``."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from .eligibility import Status, Verdict, load_rules
from .i18n import Strings
from .models import load_project

MARK = {Verdict.ELIGIBLE: "✔", Verdict.VERIFY: "?", Verdict.NOT_ELIGIBLE: "✗"}


def _date(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


def print_schemes(ev, s: Strings) -> None:
    print(s("cli.schemes_header", v=ev.rules_version, d=ev.rules_as_of))
    for r in ev.results:
        tag = f"  <- {s('cli.selected')}" if r.selected else ""
        print(f"  {MARK[r.verdict]} {r.name}{tag}")
        print(f"      {s('verdict.' + r.verdict.value)}: {r.benefit_text}")
        for x in r.rules:
            if x.status in (Status.FAIL, Status.VERIFY):
                sym = "✗" if x.status is Status.FAIL else "?"
                print(f"      {sym} {x.text} - {x.detail}")
    print(f"  ! {ev.notice}")


def cmd_run(a) -> int:
    from .pipeline import run

    p = load_project(a.project)
    if a.lang:
        p.language = a.lang
    s = Strings(p.language)
    res = run(p, Path(a.out), use_llm=not a.no_llm, recalc=not a.no_recalc, docx=not a.no_docx,
              rules_path=a.rules, run_date=_date(a.date))
    m = res.model
    print("=" * 78)
    print(s("cli.review_banner"))
    print("=" * 78)
    print(f"{p.name}")
    print(f"  Project cost ₹{m.project_cost:.2f} lakh | own ₹{m.finance.own:.2f} | subsidy ₹{m.finance.subsidy:.2f} "
          f"| bank loan ₹{m.finance.gross_loan:.2f} (net ₹{m.finance.net_loan:.2f})")
    dscr = " ".join(f"{y.dscr:.2f}" for y in m.years if y.dscr is not None)
    print(f"  DSCR by year: {dscr} (min {m.min_dscr:.2f}, avg {m.avg_dscr:.2f}) | payback year "
          f"{m.payback_year or '>5'} | break-even {m.breakeven_units_per_day:.1f} {m.breakeven_unit_name}/day")
    print()
    print_schemes(res.evaluation, s)
    print()
    wb = res.workbook
    if wb and wb.recalculated:
        if wb.ok:
            print(s("cli.workbook_ok"))
        else:
            print(s("cli.workbook_bad"))
            for line in (wb.errors + wb.uncalculated + wb.mismatches)[:20]:
                print("   ", line)
    else:
        print(s("cli.workbook_skipped"))
    print(f"Narrative: {res.narrative.source}")
    for w in res.warnings:
        print(f"  note: {w}")
    print(s("cli.outputs") + ":")
    for k, v in res.outputs.items():
        print(f"  {k:8s} {v}")
    print(s("cli.review_banner"))
    return 0 if (wb is None or not wb.recalculated or wb.ok) else 1


def cmd_schemes(a) -> int:
    from .pipeline import analyse

    p = load_project(a.project)
    ev, _ = analyse(p, a.rules, _date(a.date))
    print_schemes(ev, Strings(p.language))
    return 0


def cmd_draft(a) -> int:
    from .draft import draft_project

    text, notes = draft_project(
        idea=a.idea, district=a.district, area=a.area, categories=a.category, items=a.items, volumes=a.volumes,
        taluk=a.taluk,
    )
    Path(a.output).write_text(text, encoding="utf-8")
    print(f"Draft written to {a.output}. Review every value marked 'AI ASSUMPTION' before running it.")
    for n in notes:
        print(f"  - {n}")
    return 0


def cmd_validate_rules(a) -> int:
    data = load_rules(a.rules)
    n = sum(len(s["rules"]) for s in data["schemes"])
    unconfirmed = [f"{s['id']}.{r['id']}" for s in data["schemes"] for r in s["rules"] if r["confidence"] != "confirmed"]
    print(f"schemes.yaml v{data['version']} (as of {data['as_of']}): {len(data['schemes'])} schemes, {n} rules - OK")
    print(f"  not fully confirmed ({len(unconfirmed)}): {', '.join(unconfirmed)}")
    return 0


def cmd_i18n_missing(a) -> int:
    missing = Strings(a.lang).missing_keys()
    print(f"{len(missing)} keys untranslated in {a.lang}:")
    for k in missing:
        print("  " + k)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scheme_dpr", description="Scheme-matched DPR and 5-year model for small businesses in Karnataka.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scheme check + .xlsx model + DPR (.docx and .md)")
    r.add_argument("project", help="project YAML (see example.yaml)")
    r.add_argument("--out", default="out", help="output folder (default: out/)")
    r.add_argument("--no-llm", action="store_true", help="template narrative only; no Claude API call")
    r.add_argument("--no-recalc", action="store_true", help="skip LibreOffice recalculation and checks")
    r.add_argument("--no-docx", action="store_true", help="skip the .docx (Node) step")
    r.add_argument("--lang", choices=["en", "kn"], help="report language (overrides the YAML)")
    r.add_argument("--rules", help="alternative schemes.yaml")
    r.add_argument("--date", help="evaluate scheme windows as of this date (YYYY-MM-DD)")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("schemes", help="scheme eligibility only")
    c.add_argument("project")
    c.add_argument("--rules")
    c.add_argument("--date")
    c.set_defaults(fn=cmd_schemes)

    d = sub.add_parser("draft", help="turn a free-text idea into a project YAML with Claude (review before use)")
    d.add_argument("--idea", required=True)
    d.add_argument("--district", required=True)
    d.add_argument("--taluk")
    d.add_argument("--area", choices=["rural", "urban"], required=True)
    d.add_argument("--category", nargs="+", default=["general"])
    d.add_argument("--items", default="", help="rough set-up items and costs, free text")
    d.add_argument("--volumes", default="", help="expected daily volumes and prices, free text")
    d.add_argument("-o", "--output", default="draft.yaml")
    d.set_defaults(fn=cmd_draft)

    v = sub.add_parser("validate-rules", help="check schemes.yaml structure and provenance")
    v.add_argument("--rules")
    v.set_defaults(fn=cmd_validate_rules)

    i = sub.add_parser("i18n-missing", help="list strings not yet translated")
    i.add_argument("lang", choices=["kn"])
    i.set_defaults(fn=cmd_i18n_missing)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except ValidationError as e:
        print(f"Input error:\n{e}", file=sys.stderr)
        if "extra_forbidden" in str(e):
            print("Hint: in YAML {flow: mappings}, put text containing commas in quotes.", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
