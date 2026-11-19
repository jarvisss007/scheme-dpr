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
class MonthRow:
    month: int
    weight: float
    revenue_by_stream: dict[str, float]
    revenue: float
    cogs: float
    running_costs: float
    ebitda: float
    loan_open: float
    interest: float
    principal: float
    loan_close: float
    net_cash: float
    cash_open: float
    cash_close: float


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
    monthly: list[MonthRow] = field(default_factory=list)  # Year 1, month by month

    @property
    def lowest_cash(self) -> tuple[float, int] | None:
        if not self.monthly:
            return None
        low = min(self.monthly, key=lambda r: r.cash_close)
        return low.cash_close, low.month

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
    res.monthly = monthly_year1(p, res)
    return res


def monthly_year1(p: Project, res: ModelResult) -> list[MonthRow]:
    """Year 1 month by month, the way a bank's cash-budget (CMA) schedule looks at it.

    Volume lines follow ``assumptions.monthly_weights``; fixed income and fixed costs are
    spread evenly, so the twelve months add up to the annual Year-1 figures. Interest is
    charged monthly on the falling balance, so it is slightly below the annual model's
    (opening-balance) figure. Opening cash is the working-capital provision. Tax is
    left to the annual model.
    """
    y1 = res.years[0]
    f, c = p.finance, p.costs
    w = p.assumptions.monthly_weights
    share_streams = [s.name for s in p.revenue if s.revenue_share]
    share_base_y1 = sum(y1.revenue_by_stream[n] for n in share_streams)
    fixed_costs_month = (y1.running_costs - c.revenue_share_pct * share_base_y1) / 12
    monthly_principal = res.finance.net_loan / (f.tenor_years * 12 - f.moratorium_months)

    rows: list[MonthRow] = []
    loan = res.finance.net_loan
    cash = p.setup_cost.working_capital
    for i in range(12):
        month = i + 1
        rev = {s.name: y1.revenue_by_stream[s.name] * (w[i] if s.is_volume else 1.0) / 12 for s in p.revenue}
        revenue = sum(rev.values())
        cogs = sum(rev[s.name] * s.cogs_pct for s in p.revenue)
        running = fixed_costs_month + c.revenue_share_pct * sum(rev[n] for n in share_streams)
        ebitda = revenue - cogs - running
        interest = loan * f.interest_rate / 12
        principal = min(loan, monthly_principal) if month > f.moratorium_months else 0.0
        close = loan - principal
        net = ebitda - interest - principal
        rows.append(MonthRow(month, w[i], rev, revenue, cogs, running, ebitda, loan, interest, principal, close,
                             net, cash, cash + net))
        loan, cash = close, cash + net
    return rows


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


# ---------------------------------------------------------------- sensitivity

SCENARIOS = [
    ("base", 1.0, 1.0, 1.0),
    ("volume_-10", 0.9, 1.0, 1.0),
    ("volume_-20", 0.8, 1.0, 1.0),
    ("price_-10", 1.0, 0.9, 1.0),
    ("costs_+10", 1.0, 1.0, 1.1),
]


@dataclass
class Sensitivity:
    key: str
    year3_ebitda: float
    min_dscr: float | None
    avg_dscr: float | None
    payback_year: int | None


def sensitivity(p: Project, own_pct: float, subsidy_pct: float, subsidy_cap: float | None = None) -> list[Sensitivity]:
    """Re-run the model with volumes, prices or running costs shocked. Material cost
    stays a % of sales; fixed and staff costs scale with the cost shock."""
    out = []
    for key, vol, price, cost in SCENARIOS:
        q = p.model_copy(deep=True)
        for st in q.revenue:
            if st.is_volume:
                st.per_day *= vol  # type: ignore[operator]
                st.price_rs *= price  # type: ignore[operator]
            else:
                st.annual_lakh *= price  # type: ignore[operator]
        c = q.costs
        c.staff.monthly_lakh *= cost
        c.staff.manager_monthly_lakh *= cost
        c.rent_annual_lakh *= cost
        c.electricity.rate_rs *= cost
        for o in c.other:
            o.amount *= cost
        m = build_model(q, own_pct, subsidy_pct, subsidy_cap)
        out.append(Sensitivity(key, m.years[2].ebitda, m.min_dscr, m.avg_dscr, m.payback_year))
    return out
