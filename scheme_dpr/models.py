"""Input schema for a project.

All money is in ₹ lakh (1 lakh = ₹100,000) unless a field name says ``_rs``
(rupees). Rates are fractions (0.25 = 25%).

The schema is deliberately explicit: every number that drives the financial
model must be visible here (or in ``schemes.yaml``) so a reviewer can trace it.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sector(str, Enum):
    manufacturing = "manufacturing"
    service = "service"
    trading = "trading"


class Area(str, Enum):
    rural = "rural"
    urban = "urban"


# Categories the schemes care about. "general" means none of the others.
CATEGORIES = {
    "general",
    "sc",
    "st",
    "obc",
    "minority",
    "women",
    "ex_serviceman",
    "transgender",
    "divyang",
    "small_marginal_farmer",
}


class Activity(Strict):
    sector: Sector
    # Free tags the scheme rules match on, e.g. food_service, food_processing,
    # wayside_amenity, farm_machinery, custom_hiring_centre, liquor.
    tags: list[str] = Field(default_factory=list)
    new_unit: bool = True
    nic_description: str | None = None


class Location(Strict):
    state: str = "Karnataka"
    district: str
    taluk: str | None = None
    village_or_town: str | None = None
    area: Area
    site_description: str | None = None
    # Aspirational / hill / border districts get special-category rates under PMEGP.
    special_area: bool = False


class Applicant(Strict):
    name: str | None = None  # placeholder in the DPR when missing
    categories: list[str] = Field(default_factory=lambda: ["general"])
    age: int | None = None
    education: str | None = None  # e.g. "VIII pass", "Graduate"
    udyam_registered: bool | None = None
    prior_mudra_tarun_repaid: bool | None = None
    prior_govt_subsidy_same_family: bool | None = None

    @field_validator("categories")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        v = [c.strip().lower() for c in v] or ["general"]
        unknown = set(v) - CATEGORIES
        if unknown:
            raise ValueError(f"unknown applicant categories {sorted(unknown)}; use {sorted(CATEGORIES)}")
        if "general" in v and len(v) > 1:
            v = [c for c in v if c != "general"]
        return v


class SetupItem(Strict):
    name: str
    amount: float | None = None  # ₹ lakh
    qty: float | None = None
    unit: str | None = None
    rate: float | None = None  # ₹ lakh per unit
    note: str | None = None

    @model_validator(mode="after")
    def _amount_or_qty(self) -> "SetupItem":
        if self.amount is None and (self.qty is None or self.rate is None):
            raise ValueError(f"setup item '{self.name}': give amount, or qty and rate")
        if self.amount is not None and self.qty is not None:
            raise ValueError(f"setup item '{self.name}': give amount OR qty×rate, not both")
        return self

    @property
    def cost(self) -> float:
        return self.amount if self.amount is not None else self.qty * self.rate  # type: ignore[operator]


class SetupCost(Strict):
    items: list[SetupItem]
    contingency_pct: float = Field(0.05, ge=0, le=0.5)
    working_capital: float = Field(0.0, ge=0)  # opening stock and cash, ₹ lakh


class RevenueStream(Strict):
    """Either a volume stream (per_day × price) or a fixed annual amount."""

    name: str
    per_day: float | None = None
    price_rs: float | None = None
    cogs_pct: float = Field(0.0, ge=0, lt=1)
    annual_lakh: float | None = None  # fixed annual income, grows with inflation only
    primary: bool = False  # break-even is expressed in units of the primary stream
    revenue_share: bool = False  # included in the base for a % revenue share to a landlord/partner

    @model_validator(mode="after")
    def _kind(self) -> "RevenueStream":
        volume = self.per_day is not None or self.price_rs is not None
        if volume and self.annual_lakh is not None:
            raise ValueError(f"stream '{self.name}': per_day/price_rs OR annual_lakh, not both")
        if volume and (self.per_day is None or self.price_rs is None):
            raise ValueError(f"stream '{self.name}': needs both per_day and price_rs")
        if not volume and self.annual_lakh is None:
            raise ValueError(f"stream '{self.name}': needs per_day+price_rs or annual_lakh")
        if self.primary and not volume:
            raise ValueError(f"stream '{self.name}': the primary stream must be a volume stream")
        return self

    @property
    def is_volume(self) -> bool:
        return self.annual_lakh is None


class Staff(Strict):
    count: int = Field(ge=0)
    monthly_lakh: float = Field(ge=0)  # cost per person per month
    manager_monthly_lakh: float = 0.0


class Electricity(Strict):
    kwh_per_day: float = 0.0
    rate_rs: float = 0.0


class FixedCost(Strict):
    name: str
    amount: float  # ₹ lakh per year, grows with inflation


class RunningCosts(Strict):
    staff: Staff
    rent_annual_lakh: float = 0.0
    revenue_share_pct: float = Field(0.0, ge=0, lt=1)
    electricity: Electricity = Field(default_factory=Electricity)
    other: list[FixedCost] = Field(default_factory=list)


class Finance(Strict):
    # "auto" picks the eligible capital-subsidy scheme with the largest subsidy;
    # a scheme id forces it; "none" models a plain bank loan.
    scheme: str = "auto"
    own_contribution_pct: float | None = None  # default: the scheme's minimum
    interest_rate: float = Field(0.11, ge=0, lt=1)
    tenor_years: int = Field(5, ge=1, le=15)  # total, including any moratorium
    moratorium_months: int = Field(0, ge=0, le=36)  # no principal repaid (interest still paid)
    tax_rate: float = Field(0.15, ge=0, lt=1)
    depreciation_years: int = Field(10, ge=1)


    @model_validator(mode="after")
    def _moratorium_fits(self) -> "Finance":
        if self.moratorium_months >= self.tenor_years * 12:
            raise ValueError("moratorium must be shorter than the loan tenor")
        return self


class Assumptions(Strict):
    ramp_year1: float = Field(0.6, gt=0)
    ramp_year2: float = Field(0.85, gt=0)
    growth_after_year3: float = 0.05
    inflation: float = 0.05
    days_per_year: int = 365
    min_dscr: float = 1.3  # what banks look for


class Project(Strict):
    name: str
    idea: str
    language: Literal["en", "kn"] = "en"
    activity: Activity
    location: Location
    applicant: Applicant = Field(default_factory=Applicant)
    setup_cost: SetupCost
    revenue: list[RevenueStream]
    costs: RunningCosts
    finance: Finance = Field(default_factory=Finance)
    assumptions: Assumptions = Field(default_factory=Assumptions)
    employment: int | None = None  # default: staff + promoter
    market_notes: str | None = None  # facts the user knows about the market

    @model_validator(mode="after")
    def _one_primary(self) -> "Project":
        primaries = [s for s in self.revenue if s.primary]
        if len(primaries) > 1:
            raise ValueError("only one revenue stream can be primary")
        if not primaries:
            vol = [s for s in self.revenue if s.is_volume]
            if vol:
                vol[0].primary = True
        return self

    @property
    def primary_stream(self) -> RevenueStream | None:
        return next((s for s in self.revenue if s.primary), None)

    @property
    def employment_count(self) -> int:
        if self.employment is not None:
            return self.employment
        return self.costs.staff.count + 1  # + the promoter


def load_project(path: str | Path) -> Project:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Project.model_validate(data)
