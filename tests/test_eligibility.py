"""Scheme rules: each check, the selection logic, and that terms come from the data file."""

from __future__ import annotations

import copy
from datetime import date

import pytest
import yaml

from scheme_dpr.eligibility import Status, Verdict, evaluate, load_rules, validate_rules
from scheme_dpr.finance import project_cost
from scheme_dpr.pipeline import analyse

from .conftest import RUN_DATE


def run(p, rules=None, when=RUN_DATE, cost=None):
    return evaluate(p, cost if cost is not None else project_cost(p)[3], rules, when)


def by_id(ev):
    return {r.id: r for r in ev.results}


def rule(ev, scheme, rule_id):
    return next(x for x in by_id(ev)[scheme].rules if x.id == rule_id)


# ---------------------------------------------------------------- worked example


def test_worked_example_selects_pmegp_25pct_rural_general(example):
    ev = run(example)
    assert ev.selected.id == "pmegp"
    assert ev.selected.subsidy_pct == 0.25
    assert ev.selected.own_pct == 0.10
    assert ev.selected.verdict is Verdict.VERIFY  # age/education unknown, continuation pending
    assert ev.selected.status_state == "pending_continuation"
    assert ev.selected.subsidy_amount == pytest.approx(19.8875 * 0.25)


def test_worked_example_rules_out_the_right_schemes(example):
    ev = by_id(run(example))
    assert ev["pmfme"].verdict is Verdict.NOT_ELIGIBLE  # food service is not food processing
    assert ev["cmegp"].verdict is Verdict.NOT_ELIGIBLE  # ₹19.89 lakh > ₹10 lakh
    assert ev["smam"].verdict is Verdict.NOT_ELIGIBLE
    assert ev["mudra"].verdict is Verdict.NOT_ELIGIBLE  # Tarun Plus needs a repaid Tarun loan
    assert ev["ka_tourism_wayside"].verdict is Verdict.NOT_ELIGIBLE  # minimum eligible cost ₹2 crore
    assert ev["cgtmse"].verdict is Verdict.VERIFY  # Udyam to do


def test_every_result_carries_source_and_date(example):
    for r in run(example).results:
        assert r.source.startswith("http") and r.as_of
        for x in r.rules:
            assert x.source.startswith("http") and x.as_of and x.confidence


# ---------------------------------------------------------------- PMEGP rates


@pytest.mark.parametrize("area,cats,special_area,pct,own", [
    ("rural", ["general"], False, 0.25, 0.10),
    ("urban", ["general"], False, 0.15, 0.10),
    ("rural", ["sc"], False, 0.35, 0.05),
    ("urban", ["women"], False, 0.25, 0.05),
    ("urban", ["ex_serviceman"], False, 0.25, 0.05),
    ("rural", ["general"], True, 0.35, 0.05),  # aspirational / hill / border district
])
def test_pmegp_rate_table(make_project, area, cats, special_area, pct, own):
    p = make_project(**{"location.area": area, "applicant.categories": cats, "location.special_area": special_area})
    r = by_id(run(p))["pmegp"]
    assert (r.subsidy_pct, r.own_pct) == (pct, own)


def test_pmegp_service_cap(make_project):
    p = make_project(**{"setup_cost.working_capital": 1.25 + 0.2})  # pushes cost over ₹20 lakh
    ev = run(p)
    assert rule(ev, "pmegp", "project_cap").status is Status.FAIL
    assert ev.selected is None  # nothing else fits; plain loan


def test_pmegp_manufacturing_cap_is_higher(make_project):
    p = make_project(**{"activity.sector": "manufacturing"})
    assert rule(run(p, cost=45.0), "pmegp", "project_cap").status is Status.PASS
    assert rule(run(p, cost=51.0), "pmegp", "project_cap").status is Status.FAIL


def test_pmegp_excludes_trading(make_project):
    ev = run(make_project(**{"activity.sector": "trading"}))
    assert rule(ev, "pmegp", "sector").status is Status.FAIL
    assert rule(ev, "mudra", "sector").status is Status.PASS  # Mudra and CGTMSE allow trading
    assert rule(ev, "cgtmse", "sector").status is Status.PASS


def test_negative_list_tag_fails(make_project):
    ev = run(make_project(**{"activity.tags": ["food_service", "liquor"]}))
    assert rule(ev, "pmegp", "negative_list").status is Status.FAIL


def test_negative_list_keyword_only_asks_to_verify(make_project):
    clean = run(make_project(**{"idea": "A highway meals kiosk."}))
    assert rule(clean, "pmegp", "negative_keywords").status is Status.PASS
    flagged = run(make_project(**{"idea": "A highway kiosk that also sells cigarette packs."}))
    assert rule(flagged, "pmegp", "negative_keywords").status is Status.VERIFY


def test_education_threshold(make_project):
    ev = run(make_project(**{"applicant.education": "SSLC"}))
    assert rule(ev, "pmegp", "education").status is Status.PASS
    small = run(make_project(), cost=4.0)  # service project under ₹5 lakh: not required
    assert rule(small, "pmegp", "education").status is Status.PASS
    unknown = run(make_project())
    assert rule(unknown, "pmegp", "education").status is Status.VERIFY


def test_age(make_project):
    assert rule(run(make_project(**{"applicant.age": 17})), "pmegp", "age").status is Status.FAIL
    assert rule(run(make_project(**{"applicant.age": 30})), "pmegp", "age").status is Status.PASS


def test_existing_unit_is_not_eligible(make_project):
    assert rule(run(make_project(**{"activity.new_unit": False})), "pmegp", "new_unit").status is Status.FAIL


def test_scheme_window_check(make_project, rules):
    """The date-based check stays available for schemes with a known end date."""
    changed = copy.deepcopy(rules)
    pm = next(s for s in changed["schemes"] if s["id"] == "pmegp")
    pm["rules"].append({"id": "window", "check": "scheme_window", "params": {"approved_until": "2026-03-31"},
                        "text": "Approved period", "source": "https://example.org", "as_of": "2026-01-01",
                        "confidence": "confirmed"})
    p = make_project()
    assert rule(run(p, rules=changed, when=date(2026, 1, 15)), "pmegp", "window").status is Status.PASS
    assert rule(run(p, rules=changed, when=date(2026, 11, 18)), "pmegp", "window").status is Status.VERIFY


def with_status(rules, scheme_id, state, confidence="confirmed"):
    changed = copy.deepcopy(rules)
    s = next(x for x in changed["schemes"] if x["id"] == scheme_id)
    s["status"] = {**s["status"], "state": state, "confidence": confidence}
    return changed


@pytest.mark.parametrize("state,status,verdict", [
    ("active", Status.PASS, None),
    ("pending_continuation", Status.VERIFY, Verdict.VERIFY),
    ("lapsed", Status.VERIFY, Verdict.VERIFY),
    ("discontinued", Status.FAIL, Verdict.NOT_ELIGIBLE),
])
def test_scheme_status_is_the_first_check(make_project, rules, state, status, verdict):
    ev = run(make_project(**{"applicant.age": 30, "applicant.education": "SSLC", "applicant.udyam_registered": True,
                             "idea": "A highway meals kiosk."}),
             rules=with_status(rules, "pmegp", state))
    r = by_id(ev)["pmegp"]
    assert r.rules[0].id == "status" and r.rules[0].status is status
    assert r.verdict is (verdict or Verdict.ELIGIBLE)


def test_active_but_unconfirmed_status_is_verify(make_project, rules):
    ev = run(make_project(), rules=with_status(rules, "pmegp", "active", "unconfirmed"))
    assert by_id(ev)["pmegp"].rules[0].status is Status.VERIFY


def test_discontinued_cmegp_is_never_selected(make_project):
    ev = run(make_project(**{"applicant.categories": ["women"], "applicant.age": 30}), cost=8.0)
    assert by_id(ev)["cmegp"].verdict is Verdict.NOT_ELIGIBLE
    assert ev.selected.id == "pmegp"


# ---------------------------------------------------------------- CMEGP / PMFME / SMAM


def test_cmegp_age_limits_depend_on_category(make_project):
    gen = run(make_project(**{"applicant.age": 40}), cost=8.0)
    assert rule(gen, "cmegp", "age").status is Status.FAIL  # general: 21-35
    women = run(make_project(**{"applicant.age": 40, "applicant.categories": ["women"]}), cost=8.0)
    assert rule(women, "cmegp", "age").status is Status.PASS  # special: up to 45


def test_cmegp_only_in_karnataka(make_project):
    ev = run(make_project(**{"location.state": "Goa"}), cost=8.0)
    assert rule(ev, "cmegp", "state").status is Status.FAIL


def test_pmfme_selected_for_food_processing_when_live_and_larger(make_project, rules):
    p = make_project(**{"activity.tags": ["food_processing"], "activity.sector": "manufacturing"})
    live = with_status(rules, "pmfme", "pending_continuation")  # same footing as PMEGP
    ev = run(p, rules=live, cost=15.0)
    assert ev.selected.id == "pmfme"  # 35% of 15 = 5.25 beats PMEGP 25% of 15 = 3.75
    big = run(p, rules=live, cost=45.0)
    assert by_id(big)["pmfme"].subsidy_amount == 10  # cap
    assert big.selected.id == "pmegp"  # 25% of 45 = 11.25 beats the ₹10 lakh cap


def test_lapsed_pmfme_loses_to_pmegp_awaiting_continuation(make_project):
    p = make_project(**{"activity.tags": ["food_processing"], "activity.sector": "manufacturing"})
    ev = run(p, cost=15.0)
    assert by_id(ev)["pmfme"].subsidy_amount > by_id(ev)["pmegp"].subsidy_amount
    assert ev.selected.id == "pmegp"  # a lapsed scheme is ranked below one awaiting continuation


def test_pmegp_working_capital_limit(make_project):
    ok = run(make_project())
    assert rule(ok, "pmegp", "working_capital").status is Status.PASS
    heavy = run(make_project(**{"setup_cost.working_capital": 12.0}))  # ~39% of cost: within 60% for service
    assert rule(heavy, "pmegp", "working_capital").status is Status.PASS
    too_much = run(make_project(**{"setup_cost.working_capital": 30.0}))  # ~62% of cost
    assert rule(too_much, "pmegp", "working_capital").status is Status.FAIL


def test_tourism_rules(make_project):
    ev = run(make_project(), cost=250.0)
    assert rule(ev, "ka_tourism_wayside", "min_epc").status is Status.PASS
    assert by_id(ev)["ka_tourism_wayside"].subsidy_amount == pytest.approx(62.5)
    women = run(make_project(**{"applicant.categories": ["women"]}), cost=250.0)
    assert by_id(women)["ka_tourism_wayside"].subsidy_pct == 0.30  # +5% for women
    huge = run(make_project(), cost=1000.0)
    assert by_id(huge)["ka_tourism_wayside"].subsidy_amount == 200  # cap
    blr = run(make_project(**{"location.district": "Bengaluru Urban"}), cost=250.0)
    assert rule(blr, "ka_tourism_wayside", "not_bengaluru").status is Status.FAIL


def test_smam_chc_rate_is_40pct_for_every_category(make_project):
    ev = run(make_project(**{"activity.tags": ["custom_hiring_centre"], "applicant.categories": ["women"]}))
    assert by_id(ev)["smam"].subsidy_pct == 0.40
    ev = run(make_project(**{"activity.tags": ["farm_machinery"], "applicant.categories": ["women"]}))
    assert by_id(ev)["smam"].subsidy_pct == 0.50


def test_cmegp_cap_depends_on_group(make_project):
    gen = by_id(run(make_project(), cost=10.0))["cmegp"]
    sp = by_id(run(make_project(**{"applicant.categories": ["sc"]}), cost=10.0))["cmegp"]
    assert (gen.subsidy_amount, sp.subsidy_amount) == (2.5, 3.5)


def test_smam_needs_farm_machinery(make_project):
    ev = run(make_project(**{"activity.tags": ["custom_hiring_centre"], "activity.sector": "service"}))
    assert rule(ev, "smam", "farm_machinery").status is Status.PASS


# ---------------------------------------------------------------- loans and guarantees


@pytest.mark.parametrize("repaid,status", [(True, Status.PASS), (None, Status.VERIFY), (False, Status.FAIL)])
def test_mudra_tarun_plus_condition(make_project, repaid, status):
    ev = run(make_project(**{"applicant.prior_mudra_tarun_repaid": repaid}))
    assert rule(ev, "mudra", "tarun_plus").status is status


def test_mudra_tier_for_smaller_loan(make_project):
    ev = run(make_project(), cost=8.0)  # loan 7.2 -> Tarun, no prior-loan condition
    assert rule(ev, "mudra", "tarun_plus").status is Status.PASS
    assert "falls in Tarun" in by_id(ev)["mudra"].benefit_text


def test_udyam_missing_is_a_todo_not_a_fail(make_project):
    ev = run(make_project(**{"applicant.udyam_registered": False}))
    assert rule(ev, "cgtmse", "udyam").status is Status.VERIFY
    ev = run(make_project(**{"applicant.udyam_registered": True}))
    assert rule(ev, "cgtmse", "udyam").status is Status.PASS


# ---------------------------------------------------------------- selection and overrides


def test_force_no_scheme(make_project):
    ev, m = analyse(make_project(**{"finance.scheme": "none"}), run_date=RUN_DATE)
    assert ev.selected is None
    assert m.finance.subsidy == 0 and m.finance.own_pct == 0.10


def test_force_unknown_scheme_errors(make_project):
    with pytest.raises(ValueError):
        analyse(make_project(**{"finance.scheme": "mudra"}), run_date=RUN_DATE)


def test_own_contribution_below_scheme_minimum_errors(make_project):
    with pytest.raises(ValueError):
        analyse(make_project(**{"finance.own_contribution_pct": 0.05}), run_date=RUN_DATE)


# ---------------------------------------------------------------- terms live in the data file


def test_rates_come_from_rules_file(example, rules):
    changed = copy.deepcopy(rules)
    pm = next(s for s in changed["schemes"] if s["id"] == "pmegp")
    pm["benefit"]["rates"]["general"]["rural"] = 0.30
    ev = run(example, rules=changed)
    assert ev.selected.subsidy_pct == 0.30


def test_no_scheme_numbers_in_code():
    """Guard against terms creeping into the engine: no subsidy rates or caps in eligibility.py."""
    import inspect

    import scheme_dpr.eligibility as e

    src = inspect.getsource(e)
    for literal in ("0.25", "0.35", "0.15", "20)", "50)", "10)"):
        assert literal not in src, literal


def test_rules_file_validation(rules):
    validate_rules(rules)
    bad = copy.deepcopy(rules)
    bad["schemes"][0]["rules"][0]["check"] = "made_up"
    with pytest.raises(ValueError, match="unknown check"):
        validate_rules(bad)
    bad = copy.deepcopy(rules)
    del bad["schemes"][0]["rules"][0]["source"]
    with pytest.raises(ValueError, match="missing"):
        validate_rules(bad)
    bad = copy.deepcopy(rules)
    del bad["schemes"][0]["status"]["source"]
    with pytest.raises(ValueError, match="status missing"):
        validate_rules(bad)
    bad = copy.deepcopy(rules)
    bad["schemes"][0]["status"]["state"] = "maybe"
    with pytest.raises(ValueError, match="status.state"):
        validate_rules(bad)
    bad = copy.deepcopy(rules)
    bad["schemes"][0]["rules"][0]["online) is needed"] = None  # the unquoted-comma trap
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_rules(bad)


def test_rules_file_on_disk_is_valid():
    data = load_rules()
    assert data["version"] and data["as_of"]
    assert {s["id"] for s in data["schemes"]} == {"pmegp", "pmfme", "cmegp", "smam", "ka_tourism_wayside", "mudra", "cgtmse"}


def test_examples_all_run():
    from pathlib import Path

    from scheme_dpr.models import load_project

    root = Path(__file__).resolve().parents[1]
    for f in [root / "example.yaml", *sorted((root / "examples").glob("*.yaml"))]:
        ev, m = analyse(load_project(f), run_date=RUN_DATE)
        assert m.project_cost > 0, f
