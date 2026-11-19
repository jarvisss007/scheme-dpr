"""Scheme eligibility engine.

Reads declarative rules from ``data/schemes.yaml`` and checks a project against
them. Each rule returns PASS, FAIL, VERIFY (information missing or terms not
confirmed) or CONFIRM (a declaration the applicant must make). No scheme term is
written in this file: the numbers all come from the YAML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml

from .models import Project


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    VERIFY = "verify"
    CONFIRM = "confirm"


class Verdict(str, Enum):
    ELIGIBLE = "eligible"
    VERIFY = "eligible_if_verified"
    NOT_ELIGIBLE = "not_eligible"


@dataclass
class RuleResult:
    id: str
    text: str
    status: Status
    detail: str
    source: str
    as_of: str
    confidence: str


@dataclass
class SchemeResult:
    id: str
    name: str
    kind: str
    level: str
    agency: str
    portal: str
    source: str
    as_of: str
    verdict: Verdict
    rules: list[RuleResult]
    group: str = "general"  # general | special
    subsidy_pct: float = 0.0
    subsidy_cap: float | None = None
    subsidy_amount: float = 0.0
    own_pct: float | None = None
    benefit_text: str = ""
    notes: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    selected: bool = False

    @property
    def failed(self) -> list[RuleResult]:
        return [r for r in self.rules if r.status is Status.FAIL]

    @property
    def to_verify(self) -> list[RuleResult]:
        return [r for r in self.rules if r.status is Status.VERIFY]

    @property
    def declarations(self) -> list[RuleResult]:
        return [r for r in self.rules if r.status is Status.CONFIRM]


@dataclass
class Context:
    project: Project
    project_cost: float
    loan_amount: float | None  # None while capital-subsidy schemes are being chosen
    run_date: date


# ---------------------------------------------------------------- data loading


def default_rules_path() -> Path:
    return Path(str(resources.files("scheme_dpr") / "data" / "schemes.yaml"))


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or default_rules_path(), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    validate_rules(data)
    return data


REQUIRED_SCHEME_KEYS = {"id", "name", "level", "kind", "agency", "source", "as_of", "benefit", "rules"}
REQUIRED_RULE_KEYS = {"id", "check", "text", "source", "as_of", "confidence"}
ALLOWED_RULE_KEYS = REQUIRED_RULE_KEYS | {"params"}
CONFIDENCE = {"confirmed", "single_source", "unconfirmed"}


def validate_rules(data: dict[str, Any]) -> None:
    """Fail loudly if the rules file is missing provenance or uses unknown checks."""
    ids = set()
    for s in data["schemes"]:
        missing = REQUIRED_SCHEME_KEYS - s.keys()
        if missing:
            raise ValueError(f"scheme {s.get('id')}: missing {sorted(missing)}")
        if s["id"] in ids:
            raise ValueError(f"duplicate scheme id {s['id']}")
        ids.add(s["id"])
        for r in s["rules"]:
            missing = REQUIRED_RULE_KEYS - r.keys()
            if missing:
                raise ValueError(f"{s['id']}.{r.get('id')}: missing {sorted(missing)}")
            extra = r.keys() - ALLOWED_RULE_KEYS
            if extra:  # usually an unquoted comma inside a {flow: mapping}
                raise ValueError(f"{s['id']}.{r['id']}: unexpected keys {sorted(map(str, extra))} - quote text containing commas")
            if r["check"] not in CHECKS:
                raise ValueError(f"{s['id']}.{r['id']}: unknown check '{r['check']}'")
            if r["confidence"] not in CONFIDENCE:
                raise ValueError(f"{s['id']}.{r['id']}: confidence must be one of {sorted(CONFIDENCE)}")


# ---------------------------------------------------------------- checks
# Each check returns (status, detail).

Check = Callable[[Context, dict[str, Any], dict[str, Any]], tuple[Status, str]]


def _group(ctx: Context, scheme: dict[str, Any]) -> str:
    b = scheme.get("benefit", {})
    special = set(b.get("special_categories") or [])
    if special & set(ctx.project.applicant.categories):
        return "special"
    if b.get("special_if_special_area") and ctx.project.location.special_area:
        return "special"
    return "general"


def _by_sector(params: dict[str, Any], sector: str) -> float | None:
    if "all" in params:
        return params["all"]
    return params.get("by_sector", {}).get(sector)


def c_state_in(ctx, p, s):
    st = ctx.project.location.state
    ok = st.lower() in [x.lower() for x in p["states"]]
    return (Status.PASS if ok else Status.FAIL), f"unit is in {st}"


def c_sector_in(ctx, p, s):
    sec = ctx.project.activity.sector.value
    return (Status.PASS if sec in p["allowed"] else Status.FAIL), f"activity is {sec}"


def c_requires_tag(ctx, p, s):
    tags = set(ctx.project.activity.tags)
    hit = tags & set(p["any_of"])
    if hit:
        return Status.PASS, f"activity tagged {', '.join(sorted(hit))}"
    return Status.FAIL, f"activity is not tagged {' / '.join(p['any_of'])}"


def c_excluded_tags(ctx, p, s):
    hit = set(ctx.project.activity.tags) & set(p["tags"])
    if hit:
        return Status.FAIL, f"activity tagged {', '.join(sorted(hit))}"
    return Status.PASS, "no excluded activity tagged"


def c_keyword_watch(ctx, p, s):
    text = f"{ctx.project.name} {ctx.project.idea}".lower()
    hits = [w for w in p["words"] if re.search(rf"\b{re.escape(w.lower())}\b", text)]
    if hits:
        return Status.VERIFY, f"idea mentions {', '.join(hits)} - confirm none of these are sold or made"
    return Status.PASS, "no negative-list words in the idea"


def c_project_cost_max(ctx, p, s):
    cap = _by_sector(p, ctx.project.activity.sector.value)
    if cap is None:
        return Status.FAIL, f"no cap defined for {ctx.project.activity.sector.value}"
    ok = ctx.project_cost <= cap + 1e-9
    return (Status.PASS if ok else Status.FAIL), f"project cost ₹{ctx.project_cost:.2f} lakh vs cap ₹{cap:g} lakh"


def c_project_cost_min(ctx, p, s):
    floor = p["lakh"]
    ok = ctx.project_cost >= floor - 1e-9
    return (Status.PASS if ok else Status.FAIL), f"project cost ₹{ctx.project_cost:.2f} lakh vs minimum ₹{floor:g} lakh"


def c_loan_amount_max(ctx, p, s):
    if ctx.loan_amount is None:
        return Status.VERIFY, "loan amount not known yet"
    ok = ctx.loan_amount <= p["lakh"] + 1e-9
    return (Status.PASS if ok else Status.FAIL), f"loan ₹{ctx.loan_amount:.2f} lakh vs cap ₹{p['lakh']:g} lakh"


def c_age_range(ctx, p, s):
    age = ctx.project.applicant.age
    lo = p.get("min")
    hi = p.get("special_max") if _group(ctx, s) == "special" and p.get("special_max") else p.get("max")
    rng = f"{lo or 0}-{hi}" if hi else f"{lo}+"
    if age is None:
        return Status.VERIFY, f"applicant age not given (must be {rng})"
    ok = (lo is None or age >= lo) and (hi is None or age <= hi)
    return (Status.PASS if ok else Status.FAIL), f"age {age} vs {rng}"


def c_education_threshold(ctx, p, s):
    threshold = _by_sector(p, ctx.project.activity.sector.value) or 0
    if ctx.project_cost <= threshold:
        return Status.PASS, f"not required below ₹{threshold:g} lakh"
    edu = ctx.project.applicant.education
    if not edu:
        return Status.VERIFY, f"education not given ({p['level']} needed for this project size)"
    return Status.PASS, f"stated '{edu}' - certificate must show {p['level']} or above"


def c_new_unit_only(ctx, p, s):
    ok = ctx.project.activity.new_unit
    return (Status.PASS if ok else Status.FAIL), "new unit" if ok else "existing unit"


def c_scheme_window(ctx, p, s):
    until = date.fromisoformat(p["approved_until"])
    if ctx.run_date > until:
        return Status.VERIFY, f"approved period ended {until:%d %b %Y}; confirm the scheme is accepting applications"
    return Status.PASS, f"approved until {until:%d %b %Y}"


def c_requires_flag(ctx, p, s):
    above = p.get("when_loan_above")
    if above is not None:
        if ctx.loan_amount is None:
            return Status.VERIFY, "loan amount not known yet"
        if ctx.loan_amount <= above:
            return Status.PASS, f"loan ₹{ctx.loan_amount:.2f} lakh is within ₹{above:g} lakh"
    flag = p["flag"]
    val = getattr(ctx.project.applicant, flag)
    label = flag.replace("_", " ")
    if val is True:
        return Status.PASS, f"{label}: yes"
    if val is None:
        return Status.VERIFY, f"{label}: not stated"
    # A "false" is blocking unless the rule says it is just a to-do.
    if p.get("blocking", True):
        return Status.FAIL, f"{label}: no"
    return Status.VERIFY, f"{label}: no - do this before applying"


def c_terms_unconfirmed(ctx, p, s):
    return Status.VERIFY, "terms not confirmed from an official source"


def c_declaration(ctx, p, s):
    return Status.CONFIRM, "applicant to confirm"


CHECKS: dict[str, Check] = {
    "state_in": c_state_in,
    "sector_in": c_sector_in,
    "requires_tag": c_requires_tag,
    "excluded_tags": c_excluded_tags,
    "keyword_watch": c_keyword_watch,
    "project_cost_max": c_project_cost_max,
    "project_cost_min": c_project_cost_min,
    "loan_amount_max": c_loan_amount_max,
    "age_range": c_age_range,
    "education_threshold": c_education_threshold,
    "new_unit_only": c_new_unit_only,
    "scheme_window": c_scheme_window,
    "requires_flag": c_requires_flag,
    "terms_unconfirmed": c_terms_unconfirmed,
    "declaration": c_declaration,
}


# ---------------------------------------------------------------- evaluation


def evaluate_scheme(scheme: dict[str, Any], ctx: Context) -> SchemeResult:
    results = []
    for r in scheme["rules"]:
        status, detail = CHECKS[r["check"]](ctx, r.get("params", {}), scheme)
        # A rule we have not confirmed can't give a clean PASS.
        if status is Status.PASS and r["confidence"] == "unconfirmed":
            status, detail = Status.VERIFY, f"{detail} (rule not confirmed)"
        results.append(RuleResult(r["id"], r["text"], status, detail, r["source"], str(r["as_of"]), r["confidence"]))

    if any(x.status is Status.FAIL for x in results):
        verdict = Verdict.NOT_ELIGIBLE
    elif any(x.status is Status.VERIFY for x in results):
        verdict = Verdict.VERIFY
    else:
        verdict = Verdict.ELIGIBLE

    b = scheme["benefit"]
    res = SchemeResult(
        id=scheme["id"], name=scheme["name"], kind=scheme["kind"], level=scheme["level"],
        agency=scheme["agency"], portal=scheme.get("portal", scheme["source"]), source=scheme["source"],
        as_of=str(scheme["as_of"]), verdict=verdict, rules=results,
        notes=list(b.get("notes", [])), documents=list(scheme.get("documents", [])),
    )

    if scheme["kind"] == "capital_subsidy":
        g = _group(ctx, scheme)
        area = ctx.project.location.area.value
        res.group = g
        res.subsidy_pct = float(b["rates"][g][area])
        res.subsidy_cap = b.get("cap_lakh")
        res.own_pct = float(b["own_contribution"][g])
        amt = ctx.project_cost * res.subsidy_pct
        if res.subsidy_cap is not None:
            amt = min(amt, res.subsidy_cap)
        res.subsidy_amount = amt
        cap = f", capped at ₹{res.subsidy_cap:g} lakh" if res.subsidy_cap is not None else ""
        if b.get("reported_rate_text") and res.subsidy_pct == 0:
            res.benefit_text = f"{b['reported_rate_text']}; planned at 0%"
        else:
            res.benefit_text = f"{res.subsidy_pct:.0%} capital subsidy ({g}, {area}){cap}; own contribution {res.own_pct:.0%}"
    elif scheme["kind"] == "loan":
        res.benefit_text = _loan_tier_text(b, ctx)
    elif scheme["kind"] == "guarantee":
        res.benefit_text = f"{b['coverage_text']}. {b['fee_text']}."
    return res


def _loan_tier_text(b: dict[str, Any], ctx: Context) -> str:
    tiers = b["tiers"]
    ladder = ", ".join(f"{t['name']} up to ₹{t['max_lakh']:g} lakh" for t in tiers)
    if ctx.loan_amount is None:
        return f"Collateral-free loan: {ladder}"
    tier = next((t for t in tiers if ctx.loan_amount <= t["max_lakh"] + 1e-9), None)
    which = f"; a ₹{ctx.loan_amount:.2f} lakh loan falls in {tier['name']}" if tier else "; loan exceeds the largest tier"
    return f"Collateral-free loan, no subsidy ({ladder}){which}"


@dataclass
class Evaluation:
    results: list[SchemeResult]
    selected: SchemeResult | None
    rules_version: str
    rules_as_of: str
    notice: str


def pick_subsidy_scheme(results: list[SchemeResult], forced: str = "auto") -> SchemeResult | None:
    caps = [r for r in results if r.kind == "capital_subsidy"]
    if forced == "none":
        return None
    if forced != "auto":
        chosen = next((r for r in caps if r.id == forced), None)
        if chosen is None:
            raise ValueError(f"finance.scheme '{forced}' is not a capital-subsidy scheme in schemes.yaml")
        return chosen
    usable = [r for r in caps if r.verdict is not Verdict.NOT_ELIGIBLE and r.subsidy_amount > 0]
    if not usable:
        return None
    order = {r.id: i for i, r in enumerate(caps)}
    # Largest subsidy; then fewer open questions; then file order.
    return sorted(usable, key=lambda r: (-round(r.subsidy_amount, 6), len(r.to_verify), order[r.id]))[0]


def evaluate(project: Project, project_cost: float, rules: dict[str, Any] | None = None,
             run_date: date | None = None) -> Evaluation:
    """Two passes: choose the capital-subsidy scheme, then check loan/guarantee schemes
    against the loan that choice implies."""
    rules = rules or load_rules()
    run_date = run_date or date.today()
    ctx = Context(project, project_cost, None, run_date)

    first = [evaluate_scheme(s, ctx) for s in rules["schemes"] if s["kind"] == "capital_subsidy"]
    chosen = pick_subsidy_scheme(first, project.finance.scheme)

    own_pct = project.finance.own_contribution_pct
    if own_pct is None:
        own_pct = chosen.own_pct if chosen else 0.10
    ctx.loan_amount = project_cost * (1 - own_pct)

    results = []
    for s in rules["schemes"]:
        r = evaluate_scheme(s, ctx)
        if chosen and r.id == chosen.id:
            r.selected = True
        results.append(r)
    selected = next((r for r in results if r.selected), None)
    return Evaluation(results, selected, str(rules["version"]), str(rules["as_of"]), rules["global_notice"].strip())
