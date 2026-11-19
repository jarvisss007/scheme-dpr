from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from scheme_dpr.eligibility import load_rules
from scheme_dpr.models import Project

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = date(2026, 11, 18)


@pytest.fixture
def example_data() -> dict:
    return yaml.safe_load((ROOT / "example.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def example(example_data) -> Project:
    return Project.model_validate(example_data)


@pytest.fixture
def reference() -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "ranebennur_option_a.json").read_text())


@pytest.fixture
def rules() -> dict:
    return load_rules()


@pytest.fixture
def make_project(example_data):
    """Build a variant of the example: make_project(**{"location.area": "urban"})."""

    def _make(**changes) -> Project:
        d = copy.deepcopy(example_data)
        for dotted, value in changes.items():
            node = d
            *path, last = dotted.split(".")
            for part in path:
                node = node[int(part)] if isinstance(node, list) else node[part]
            if isinstance(node, list):
                node[int(last)] = value
            else:
                node[last] = value
        return Project.model_validate(d)

    return _make
