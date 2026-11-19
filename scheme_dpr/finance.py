"""5-year financial model, computed in plain Python.

Every formula here has a twin in ``excel.py``; after LibreOffice recalculates the
workbook, ``excel.verify_against_model`` checks the two agree cell by cell. The
Python side is the source of truth for the report text; the workbook is what a
bank officer or consultant edits.

Conventions (match the Ranebennur worked example):
* money in ₹ lakh; per-unit prices in ₹, divided by 1e5;
* volume factor: Y1 = ramp1, Y2 = ramp2, Y3 = 1, then (1+growth)^(year-3);
* price factor: (1+inflation)^(year-1), applied to prices, wages, rent and fixed costs;
* the interest-bearing loan is project cost − own contribution − capital subsidy
  (PMEGP charges no interest on the subsidy portion), repaid in equal monthly
  principal instalments over the tenor after any moratorium; interest each year is
  the opening balance × rate (slightly conservative);
* depreciation straight-line on fixed assets (project cost − working capital).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Project

YEARS = 5
LAKH = 100_000
EPS = 1e-9  # ₹0.0001 - below this a loan balance is treated as zero


@dataclass
class MeansOfFinance:
    project_cost: float
    own_pct: float
    subsidy_pct: float
    subsidy_cap: float | None
    own: float
    subsidy: float
    gross_loan: float  # what the bank sanctions: cost − own
    net_loan: float  # interest-bearing after the subsidy is adjusted


@dataclass
class YearRow:
    year: int
    volume_factor: float
    price_factor: float
    revenue_by_stream: dict[str, float]
    revenue: float
    cogs: float
    gross_profit: float
    staff: float
    rent_share: float
    electricity: float
    other_by_name: dict[str, float]
    running_costs: float
    ebitda: float
    depreciation: float
    loan_open: float
    interest: float
    principal: float
    loan_close: float
    pbt: float
    tax: float
    net_profit: float
    cash_after_debt: float
    cumulative_cash: float
    dscr: float | None


@dataclass
class ModelResult:
    setup_items: list[tuple[str, float]]
    capex_subtotal: float
    contingency: float
    working_capital: float
    project_cost: float
    finance: MeansOfFinance
    years: list[YearRow] = field(default_factory=list)
    payback_year: int | None = None
    breakeven_units_per_day: float | None = None
    breakeven_unit_name: str | None = None

    @property
    def dscr_values(self) -> list[float]:
        return [y.dscr for y in self.years if y.dscr is not None]

    @property
    def min_dscr(self) -> float | None:
        return min(self.dscr_values) if self.dscr_values else None

    @property
    def avg_dscr(self) -> float | None:
        v = self.dscr_values
        return sum(v) / len(v) if v else None


def repayment_months_in_year(year: int, moratorium_months: int) -> int:
    """Months of principal repayment that fall in model year ``year`` (1-based)."""
    return max(0, year * 12 - moratorium_months) - max(0, (year - 1) * 12 - moratorium_months)


def volume_factor(year: int, a) -> float:
    base = a.ramp_year1 if year == 1 else a.ramp_year2 if year == 2 else 1.0
    return base * (1 + a.growth_after_year3) ** max(0, year - 3)


def price_factor(year: int, a) -> float:
    return (1 + a.inflation) ** (year - 1)


def project_cost(p: Project) -> tuple[list[tuple[str, float]], float, float, float]:
    items = [(i.name, i.cost) for i in p.setup_cost.items]
    capex = sum(c for _, c in items)
    contingency = capex * p.setup_cost.contingency_pct
    total = capex + contingency + p.setup_cost.working_capital
    return items, capex, contingency, total


def means_of_finance(cost: float, own_pct: float, subsidy_pct: float, subsidy_cap: float | None) -> MeansOfFinance:
    if not 0 <= own_pct < 1:
        raise ValueError("own contribution must be between 0 and 100%")
    own = cost * own_pct
    subsidy = cost * subsidy_pct
    if subsidy_cap is not None:
        subsidy = min(subsidy, subsidy_cap)
    gross = cost - own
    net = cost - own - subsidy
    if net < -1e-9:
        raise ValueError("own contribution + subsidy exceed project cost")
    return MeansOfFinance(cost, own_pct, subsidy_pct, subsidy_cap, own, subsidy, gross, net if net > EPS else 0.0)


def build_model(p: Project, own_pct: float, subsidy_pct: float, subsidy_cap: float | None = None) -> ModelResult:
    a = p.assumptions
    c = p.costs
    f = p.finance
    days = a.days_per_year

    items, capex, contingency, cost = project_cost(p)
    mof = means_of_finance(cost, own_pct, subsidy_pct, subsidy_cap)
    res = ModelResult(items, capex, contingency, p.setup_cost.working_capital, cost, mof)

    depreciation = (cost - p.setup_cost.working_capital) / f.depreciation_years
    repay_months = f.tenor_years * 12 - f.moratorium_months
    monthly = mof.net_loan / repay_months
    loan = mof.net_loan
    cumulative = -mof.own

    for yr in range(1, YEARS + 1):
        vf = volume_factor(yr, a)
        pf = price_factor(yr, a)
        rev: dict[str, float] = {}
        cogs = 0.0
        share_base = 0.0
        for s in p.revenue:
            if s.is_volume:
                r = s.per_day * days * vf * s.price_rs * pf / LAKH  # type: ignore[operator]
            else:
                r = s.annual_lakh * pf  # type: ignore[operator]
            rev[s.name] = r
            cogs += r * s.cogs_pct
            if s.revenue_share:
                share_base += r
        revenue = sum(rev.values())
        gross = revenue - cogs

        staff = (c.staff.count * c.staff.monthly_lakh + c.staff.manager_monthly_lakh) * 12 * pf
        rent_share = c.rent_annual_lakh * pf + c.revenue_share_pct * share_base
        elec = c.electricity.kwh_per_day * c.electricity.rate_rs * days * pf / LAKH
        other = {o.name: o.amount * pf for o in c.other}
        running = staff + rent_share + elec + sum(other.values())
        ebitda = gross - running

        interest = loan * f.interest_rate
        principal = min(loan, monthly * repayment_months_in_year(yr, f.moratorium_months))
        close = loan - principal
        pbt = ebitda - depreciation - interest
        tax = max(0.0, pbt) * f.tax_rate
        net = pbt - tax
        cash = ebitda - tax - interest - principal
        cumulative += cash
        debt_service = interest + principal
        dscr = (ebitda - tax) / debt_service if debt_service > EPS else None

        res.years.append(
            YearRow(yr, vf, pf, rev, revenue, cogs, gross, staff, rent_share, elec, other, running, ebitda,
                    depreciation, loan, interest, principal, close, pbt, tax, net, cash, cumulative, dscr)
        )
        loan = close

    res.payback_year = next((y.year for y in res.years if y.cumulative_cash >= 0), None)
    res.breakeven_units_per_day, res.breakeven_unit_name = breakeven(p, res)
    return res


def breakeven(p: Project, res: ModelResult) -> tuple[float | None, str | None]:
    """Primary-stream units per day needed in Year 3 to cover running costs and debt service.

    Other streams' contribution (revenue − their own COGS) is netted off first;
    running costs are taken at their Year-3 level. Same as the worked example.
    """
    s = p.primary_stream
    if s is None or len(res.years) < 3:
        return None, None
    y3 = res.years[2]
    other_contrib = sum(
        y3.revenue_by_stream[o.name] * (1 - o.cogs_pct) for o in p.revenue if o.name != s.name
    )
    need = y3.running_costs + y3.interest + y3.principal - other_contrib
    per_unit_year = s.price_rs * y3.price_factor * (1 - s.cogs_pct) * p.assumptions.days_per_year / LAKH  # type: ignore[operator]
    if per_unit_year <= 0:
        return None, s.name
    return need / per_unit_year, s.name
