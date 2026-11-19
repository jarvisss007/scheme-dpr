"""Narrative sections of the DPR (description, market, operations, business risks).

Claude writes prose only. It is given the computed facts and told not to state
any figure that is not in them; ``check_numbers`` enforces that afterwards and
falls back to the template text for any section that breaks the rule. With no
API access the whole narrative comes from templates.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .eligibility import Evaluation
from .finance import ModelResult
from .i18n import Strings
from .models import Project

MODEL = "claude-opus-5-5"
SECTIONS = ("description", "market", "operations")


@dataclass
class RiskItem:
    risk: str
    mitigation: str


@dataclass
class Narrative:
    description: str
    market: str
    operations: str
    risks: list[RiskItem]
    source: str  # "claude" | "template" | "mixed"
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- facts


def facts(p: Project, m: ModelResult, ev: Evaluation) -> dict[str, Any]:
    """The only information the model may use. Numbers are pre-formatted strings."""
    loc = p.location
    y3 = m.years[2]
    return {
        "project_name": p.name,
        "idea": p.idea.strip(),
        "activity_sector": p.activity.sector.value,
        "activity_tags": p.activity.tags,
        "nic": p.activity.nic_description,
        "location": {"state": loc.state, "district": loc.district, "taluk": loc.taluk,
                     "village_or_town": loc.village_or_town, "area": loc.area.value, "site": loc.site_description},
        "market_notes_from_promoter": (p.market_notes or "").strip(),
        "setup_items": [n for n, v in m.setup_items if v > 0],
        "products_and_daily_volumes": [
            {"name": s.name, "per_day": f"{s.per_day:g}", "price_rs": f"{s.price_rs:g}"} if s.is_volume
            else {"name": s.name, "annual_lakh": f"{s.annual_lakh:g}"} for s in p.revenue
        ],
        "staff_count": str(p.costs.staff.count),
        "employment_total": str(p.employment_count),
        "project_cost_lakh": f"{m.project_cost:.2f}",
        "year3_sales_lakh": f"{y3.revenue:.2f}",
        "scheme_used": ev.selected.name if ev.selected else None,
    }


# ---------------------------------------------------------------- number guard

_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for tok in _NUM.findall(text):
        t = tok.replace(",", "").rstrip(".")
        if "." in t:
            t = t.rstrip("0").rstrip(".")
        out.add(t)
    return out


def check_numbers(text: str, allowed_source: str) -> list[str]:
    """Numbers in ``text`` that do not appear in ``allowed_source``. Integers 1-12 are
    allowed (counts like 'two shifts', 'year 3')."""
    allowed = _numbers(allowed_source) | {str(i) for i in range(1, 13)}
    return sorted(n for n in _numbers(text) if n not in allowed)


# ---------------------------------------------------------------- templates


def template_narrative(p: Project, m: ModelResult, ev: Evaluation, s: Strings) -> Narrative:
    loc = p.location
    place = ", ".join(x for x in [loc.village_or_town, loc.taluk, loc.district] if x)
    vol = [x for x in p.revenue if x.is_volume]
    offer = ", ".join(x.name.lower() for x in p.revenue)
    desc = s("tmpl.description", name=p.name, idea=p.idea.strip(), place=place, area=s("area." + loc.area.value),
             sector=s("sector." + p.activity.sector.value))
    market = s("tmpl.market", notes=(p.market_notes or s("tmpl.market_no_notes")).strip(), place=place,
               volumes=", ".join(f"{x.per_day:g} {x.name.lower()}" for x in vol) or "-")
    ops = s("tmpl.operations", offer=offer, staff=p.costs.staff.count,
            items=", ".join(n for n, v in m.setup_items if v > 0))
    risks = [RiskItem(r["risk"], r["mitigation"]) for r in s.get("tmpl.risks")]
    return Narrative(desc, market, ops, risks, "template")


# ---------------------------------------------------------------- Claude

SYSTEM = """You write the narrative sections of a Detailed Project Report (DPR) for a small \
business in India that is applying for a bank loan under a government scheme. The reader is a \
bank credit officer or a District Industries Centre official.

Rules:
- Use only the facts in the user message. Do not invent facts about the promoter, the site, \
competitors, traffic counts, prices, or government policy.
- Do not write any number, percentage, amount or date that does not appear in the facts. Prefer \
words over figures; the report's tables carry the numbers.
- Do not state that the project is eligible for any scheme; eligibility is checked separately.
- Plain, formal, specific. No marketing language, no exclamation marks, no headings or bullet \
characters inside the text.
- description: what the unit does, for whom, where, and why this site (90-150 words).
- market: who the customers are and why demand exists, using the promoter's market notes; say \
plainly what is assumed rather than surveyed (90-150 words).
- operations: daily working, staffing, purchasing, hygiene and upkeep (80-130 words).
- risks: four to six business risks specific to this project, each with a practical mitigation \
(one sentence each). Do not repeat financial-ratio risks; those are computed separately."""

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "market": {"type": "string"},
        "operations": {"type": "string"},
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"risk": {"type": "string"}, "mitigation": {"type": "string"}},
                "required": ["risk", "mitigation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["description", "market", "operations", "risks"],
    "additionalProperties": False,
}


def api_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))


def call_claude_json(system: str, user: str, schema: dict[str, Any], effort: str = "medium") -> dict[str, Any]:
    """One structured-output call. Raises RuntimeError with a readable reason on failure."""
    import anthropic

    try:
        client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # re-route on a policy decline instead of failing
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError(f"Claude API authentication failed: {e}") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"Claude API rate limit: {e}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"Claude API unreachable: {e}") from e
    except anthropic.AnthropicError as e:  # e.g. no credentials configured
        raise RuntimeError(f"Claude API not configured: {e}") from e

    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude response was cut off (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("Claude returned no text")
    return json.loads(text)


def claude_narrative(p: Project, m: ModelResult, ev: Evaluation, s: Strings, call=call_claude_json) -> Narrative:
    """Ask Claude for the narrative; keep the template for any section that fails the number check."""
    fallback = template_narrative(p, m, ev, s)
    fx = facts(p, m, ev)
    fx_text = json.dumps(fx, ensure_ascii=False, indent=1)
    user = (f"Write the narrative in {s.language_name}.\n\nFacts (JSON):\n{fx_text}")
    try:
        out = call(SYSTEM, user, SCHEMA)
    except (RuntimeError, json.JSONDecodeError) as e:
        fallback.warnings.append(f"Narrative from templates: {e}")
        return fallback

    warnings: list[str] = []
    used_template = False
    result: dict[str, Any] = {}
    for sec in SECTIONS:
        text = (out.get(sec) or "").strip()
        bad = check_numbers(text, fx_text)
        if not text or bad:
            warnings.append(f"'{sec}' replaced with template text: Claude wrote figures not in the facts ({', '.join(bad) or 'empty'})")
            result[sec] = getattr(fallback, sec)
            used_template = True
        else:
            result[sec] = text
    risks = []
    for r in out.get("risks", []):
        joined = f"{r.get('risk', '')} {r.get('mitigation', '')}"
        if r.get("risk") and r.get("mitigation") and not check_numbers(joined, fx_text):
            risks.append(RiskItem(r["risk"].strip(), r["mitigation"].strip()))
        else:
            warnings.append("a risk item was dropped: it contained figures not in the facts")
    if not risks:
        risks = fallback.risks
        used_template = True
    return Narrative(result["description"], result["market"], result["operations"], risks,
                     "mixed" if used_template else "claude", warnings)


def make_narrative(p: Project, m: ModelResult, ev: Evaluation, s: Strings, use_llm: bool = True) -> Narrative:
    if not use_llm:
        return template_narrative(p, m, ev, s)
    if not api_available():
        n = template_narrative(p, m, ev, s)
        n.warnings.append("No Claude API credentials found (ANTHROPIC_API_KEY); narrative from templates.")
        return n
    return claude_narrative(p, m, ev, s)
