"""Financial math: the worked example must be reproduced exactly, and the
loan / break-even / payback logic must behave at the edges."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scheme_dpr.finance import (
    build_model,
    means_of_finance,
    price_factor,
    project_cost,
    repayment_months_in_year,
    volume_factor,
)
from scheme_dpr.models import Assumptions

TOL = 1e-9


def test_project_cost_matches_worked_example(example, reference):
    items, capex, contingency, total = project_cost(example)
    assert total == pytest.approx(reference["project_cost"], abs=TOL)  # ₹19.8875 lakh
    assert contingency == pytest.approx(reference["contingency"], abs=TOL)
    assert dict(items)["Kitchen and seating shed (prefab)"] == pytest.approx(9.75)


def test_means_of_finance_pmegp_rural_general(example, reference):
    m = build_model(example, own_pct=0.10, subsidy_pct=0.25)
    assert m.finance.own == pytest.approx(reference["own"], abs=TOL)
    assert m.finance.subsidy == pytest.approx(reference["subsidy"], abs=TOL)
    assert m.finance.net_loan == pytest.approx(reference["net_loan"], abs=TOL)
    assert m.finance.gross_loan == pytest.approx(reference["project_cost"] * 0.9, abs=TOL)


MAP = {
    "volume_factor": lambda y: y.volume_factor,
    "price_factor": lambda y: y.price_factor,
    "meals": lambda y: y.revenue_by_stream["Meals"],
    "tea": lambda y: y.revenue_by_stream["Tea and snacks"],
    "pump_fee": lambda y: y.revenue_by_stream["Fee from fuel station for running its toilets"],
    "revenue": lambda y: y.revenue,
    "cogs": lambda y: y.cogs,
    "gross_profit": lambda y: y.gross_profit,
    "staff": lambda y: y.staff,
    "rent_share": lambda y: y.rent_share,
    "electricity": lambda y: y.electricity,
    "repairs": lambda y: y.other_by_name["Repairs, supplies, licences"],
    "marketing": lambda y: y.other_by_name["Marketing"],
    "running_costs": lambda y: y.running_costs,
    "ebitda": lambda y: y.ebitda,
    "depreciation": lambda y: y.depreciation,
    "loan_open": lambda y: y.loan_open,
    "interest": lambda y: y.interest,
    "principal": lambda y: y.principal,
    "loan_close": lambda y: y.loan_close,
    "pbt": lambda y: y.pbt,
    "tax": lambda y: y.tax,
    "net_profit": lambda y: y.net_profit,
    "cash_after_debt": lambda y: y.cash_after_debt,
    "cumulative_cash": lambda y: y.cumulative_cash,
    "dscr": lambda y: y.dscr,
}


@pytest.mark.parametrize("line", sorted(MAP))
def test_every_line_matches_worked_example(example, reference, line):
    m = build_model(example, own_pct=0.10, subsidy_pct=0.25)
    got = [MAP[line](y) for y in m.years]
    assert got == pytest.approx(reference["years"][line], abs=1e-9), line


def test_payback_and_breakeven_match_worked_example(example, reference):
    m = build_model(example, own_pct=0.10, subsidy_pct=0.25)
    assert m.payback_year == reference["payback_year"] == 2
    assert m.breakeven_units_per_day == pytest.approx(reference["breakeven_meals_per_day"], abs=1e-9)
    assert m.breakeven_unit_name == "Meals"


def test_year1_dscr_is_below_bank_norm(example):
    """A real finding of the worked example: year-1 cover is 1.28x, under 1.3x."""
    m = build_model(example, own_pct=0.10, subsidy_pct=0.25)
    assert m.years[0].dscr == pytest.approx(1.2834, abs=1e-4)
    assert m.min_dscr < example.assumptions.min_dscr


def test_volume_and_price_factors():
    a = Assumptions(ramp_year1=0.6, ramp_year2=0.85, growth_after_year3=0.05, inflation=0.05)
    assert [volume_factor(y, a) for y in range(1, 6)] == pytest.approx([0.6, 0.85, 1.0, 1.05, 1.1025])
    assert [price_factor(y, a) for y in range(1, 6)] == pytest.approx([1, 1.05, 1.1025, 1.157625, 1.21550625])


@pytest.mark.parametrize("mor,expected", [
    (0, [12, 12, 12, 12, 12]),
    (6, [6, 12, 12, 12, 12]),
    (12, [0, 12, 12, 12, 12]),
    (18, [0, 6, 12, 12, 12]),
])
def test_repayment_months(mor, expected):
    assert [repayment_months_in_year(y, mor) for y in range(1, 6)] == expected


def test_moratorium_repays_full_loan_and_lifts_year1_cover(make_project):
    base = build_model(make_project(), 0.10, 0.25)
    mor = build_model(make_project(**{"finance.moratorium_months": 6}), 0.10, 0.25)
    assert sum(y.principal for y in mor.years) == pytest.approx(mor.finance.net_loan)
    assert mor.years[-1].loan_close == pytest.approx(0, abs=1e-9)
    assert mor.years[0].principal == pytest.approx(mor.finance.net_loan / 54 * 6)
    assert mor.years[0].dscr > base.years[0].dscr


def test_loan_longer_than_horizon_leaves_balance(make_project):
    m = build_model(make_project(**{"finance.tenor_years": 7}), 0.10, 0.25)
    assert m.years[-1].loan_close == pytest.approx(m.finance.net_loan * 2 / 7)


def test_subsidy_cap_applies():
    mof = means_of_finance(40.0, 0.10, 0.35, subsidy_cap=10.0)
    assert mof.subsidy == 10.0
    assert mof.net_loan == pytest.approx(40 - 4 - 10)


def test_own_plus_subsidy_cannot_exceed_cost():
    with pytest.raises(ValueError):
        means_of_finance(10.0, 0.80, 0.35, None)


def test_no_loan_means_no_dscr(make_project):
    m = build_model(make_project(), own_pct=0.75, subsidy_pct=0.25)
    assert m.finance.net_loan == pytest.approx(0)
    assert all(y.dscr is None for y in m.years)
    assert m.min_dscr is None


def test_tax_is_zero_on_losses(make_project):
    m = build_model(make_project(**{"revenue.0.per_day": 10}), 0.10, 0.25)
    assert m.years[0].pbt < 0
    assert m.years[0].tax == 0


def test_no_payback_within_five_years(make_project):
    m = build_model(make_project(**{"revenue.0.per_day": 20, "revenue.1.per_day": 40}), 0.10, 0.25)
    assert m.payback_year is None


def test_revenue_share_only_on_flagged_streams(make_project):
    m = build_model(make_project(**{"revenue.1.revenue_share": False}), 0.10, 0.25)
    y = m.years[0]
    assert y.rent_share == pytest.approx(0.10 * y.revenue_by_stream["Meals"])


@pytest.mark.parametrize("bad_item", [
    {"name": "x"},
    {"name": "x", "amount": 1, "qty": 2, "rate": 0.1},
    {"name": "x", "qty": 2},
])
def test_setup_item_validation(example_data, bad_item):
    from scheme_dpr.models import Project

    example_data["setup_cost"]["items"].append(bad_item)
    with pytest.raises(ValidationError):
        Project.model_validate(example_data)


def test_moratorium_must_be_inside_tenor(example_data):
    from scheme_dpr.models import Project

    example_data["finance"]["moratorium_months"] = 60
    with pytest.raises(ValidationError):
        Project.model_validate(example_data)
