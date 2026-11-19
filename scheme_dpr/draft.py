"""Idea -> project YAML, using Claude to structure free text.

Claude turns the promoter's description, rough items and volumes into the input
schema. Anything the promoter did not state is marked as an AI assumption in the
YAML so the reviewer sees exactly which inputs to check. The draft is validated
against ``models.Project`` before it is written. Claude never computes outputs:
the model, the scheme check and the report all run from the reviewed YAML.
"""

from __future__ import annotations

from typing import Any, Callable

import yaml

from .models import CATEGORIES, Project
from .narrative import call_claude_json

KNOWN_TAGS = [
    "food_service", "food_processing", "wayside_amenity", "highway", "tourism", "farm_machinery",
    "custom_hiring_centre", "retail", "repair_service", "tailoring", "beauty_wellness", "dairy",
    "bakery", "transport", "liquor", "tobacco", "meat_slaughter",
]

_num = {"type": "number"}
_str = {"type": "string"}
_bool = {"type": "boolean"}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _obj({
    "name": _str,
    "idea_summary": _str,
    "sector": {"type": "string", "enum": ["manufacturing", "service", "trading"]},
    "tags": {"type": "array", "items": {"type": "string", "enum": KNOWN_TAGS}},
    "nic_description": _str,
    "site_description": _str,
    "setup_items": {"type": "array", "items": _obj({"name": _str, "amount_lakh": _num, "stated_by_promoter": _bool, "basis": _str})},
    "contingency_pct": _num,
    "working_capital_lakh": _num,
    "working_capital_stated": _bool,
    "revenue": {"type": "array", "items": _obj({
        "name": _str, "kind": {"type": "string", "enum": ["per_day", "annual_fixed"]},
        "per_day": _num, "price_rs": _num, "annual_lakh": _num, "cogs_pct": _num,
        "primary": _bool, "stated_by_promoter": _bool, "basis": _str})},
    "staff_count": {"type": "integer"},
    "staff_monthly_lakh": _num,
    "rent_annual_lakh": _num,
    "revenue_share_pct": _num,
    "kwh_per_day": _num,
    "power_rate_rs": _num,
    "other_costs": {"type": "array", "items": _obj({"name": _str, "amount_lakh": _num, "stated_by_promoter": _bool})},
    "market_notes": _str,
    "open_questions": {"type": "array", "items": _str},
})

SYSTEM = """You convert a small-business idea in Karnataka, India into structured inputs \
for a project-report tool. Money is in rupees lakh (1 lakh = 100,000 rupees) except price_rs, \
which is rupees per unit.

- Use the promoter's own numbers wherever given and set stated_by_promoter true for them.
- Where a needed input is missing, give a conservative, typical value for a small unit of this \
kind in a Karnataka district, set stated_by_promoter false, and explain the basis in a few words.
- Do not inflate volumes. Prefer the low end of a plausible range.
- Pick tags only from the allowed list; tag food_processing only for processing units (not \
restaurants), and add liquor/tobacco/meat_slaughter only if the idea involves them.
- market_notes: only what the promoter said about customers and location, restated plainly.
- open_questions: what the consultant must ask the promoter before the report is final."""


def _user_prompt(idea: str, district: str, area: str, categories: list[str], items: str, volumes: str, taluk: str | None) -> str:
    return (f"Idea: {idea}\nDistrict: {district}\nTaluk: {taluk or 'not given'}\nArea: {area}\n"
            f"Applicant categories: {', '.join(categories)}\n"
            f"Rough set-up items and costs (promoter): {items or 'not given'}\n"
            f"Expected daily volumes and prices (promoter): {volumes or 'not given'}")


def to_project_dict(d: dict[str, Any], district: str, area: str, categories: list[str], taluk: str | None) -> tuple[dict[str, Any], list[str]]:
    """Map the model's draft to the Project schema; return (data, list of AI assumptions)."""
    assumed: list[str] = []
    items = []
    for it in d["setup_items"]:
        items.append({"name": it["name"], "amount": round(float(it["amount_lakh"]), 4)})
        if not it["stated_by_promoter"]:
            assumed.append(f"set-up '{it['name']}' ₹{it['amount_lakh']:g} lakh ({it['basis']})")
    if not d["working_capital_stated"]:
        assumed.append(f"working capital ₹{d['working_capital_lakh']:g} lakh")
    revenue = []
    for r in d["revenue"]:
        if r["kind"] == "per_day":
            row = {"name": r["name"], "per_day": r["per_day"], "price_rs": r["price_rs"], "cogs_pct": r["cogs_pct"]}
        else:
            row = {"name": r["name"], "annual_lakh": r["annual_lakh"], "cogs_pct": r["cogs_pct"]}
        if r["primary"] and r["kind"] == "per_day":
            row["primary"] = True
        revenue.append(row)
        if not r["stated_by_promoter"]:
            assumed.append(f"sales line '{r['name']}' ({r['basis']})")
    if sum(1 for r in revenue if r.get("primary")) > 1:
        first = True
        for r in revenue:
            if r.get("primary"):
                r["primary"] = first
                first = False
    other = [{"name": o["name"], "amount": o["amount_lakh"]} for o in d["other_costs"]]
    assumed += [f"running cost '{o['name']}' ₹{o['amount_lakh']:g} lakh/yr" for o in d["other_costs"] if not o["stated_by_promoter"]]
    cats = [c for c in categories if c in CATEGORIES] or ["general"]
    data = {
        "name": d["name"],
        "idea": d["idea_summary"],
        "activity": {"sector": d["sector"], "tags": sorted(set(d["tags"])), "new_unit": True, "nic_description": d["nic_description"]},
        "location": {"state": "Karnataka", "district": district, "taluk": taluk, "area": area, "site_description": d["site_description"]},
        "applicant": {"categories": cats},
        "setup_cost": {"items": items, "contingency_pct": d["contingency_pct"], "working_capital": d["working_capital_lakh"]},
        "revenue": revenue,
        "costs": {
            "staff": {"count": d["staff_count"], "monthly_lakh": d["staff_monthly_lakh"]},
            "rent_annual_lakh": d["rent_annual_lakh"], "revenue_share_pct": d["revenue_share_pct"],
            "electricity": {"kwh_per_day": d["kwh_per_day"], "rate_rs": d["power_rate_rs"]},
            "other": other,
        },
        "market_notes": d["market_notes"],
    }
    return data, assumed


def draft_project(idea: str, district: str, area: str, categories: list[str], items: str = "", volumes: str = "",
                  taluk: str | None = None, call: Callable[..., dict[str, Any]] = call_claude_json) -> tuple[str, list[str]]:
    raw = call(SYSTEM, _user_prompt(idea, district, area, categories, items, volumes, taluk), SCHEMA)
    data, assumed = to_project_dict(raw, district, area, categories, taluk)
    Project.model_validate(data)  # fail here, not later, if the draft is malformed
    header = ["# DRAFT generated from a free-text idea by Claude. REVIEW BEFORE USE.",
              "# AI ASSUMPTIONS (not stated by the promoter) - check each one:"]
    header += [f"#   - {a}" for a in assumed] or ["#   (none)"]
    header += ["# Open questions for the promoter:"] + [f"#   - {q}" for q in raw.get("open_questions", [])]
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(header) + "\n\n" + body, assumed + [f"ask: {q}" for q in raw.get("open_questions", [])]
