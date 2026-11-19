"""User-facing text lives in ``locales/<lang>.yaml``. Missing keys fall back to English,
so a partial Kannada file is usable while translation is in progress."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

SUPPORTED = ("en", "kn")


@lru_cache(maxsize=None)
def _load(lang: str) -> dict[str, Any]:
    path = resources.files("scheme_dpr") / "locales" / f"{lang}.yaml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _lookup(tree: dict[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class Strings:
    def __init__(self, lang: str = "en"):
        if lang not in SUPPORTED:
            raise ValueError(f"language must be one of {SUPPORTED}")
        self.lang = lang
        self.language_name = {"en": "English", "kn": "Kannada"}[lang]

    def get(self, key: str) -> Any:
        v = _lookup(_load(self.lang), key)
        if v is None and self.lang != "en":
            v = _lookup(_load("en"), key)
        if v is None:
            raise KeyError(f"missing string '{key}'")
        return v

    def __call__(self, key: str, **kw: Any) -> str:
        v = self.get(key)
        if not isinstance(v, str):
            raise TypeError(f"string '{key}' is not text")
        return v.format(**kw) if kw else v

    def missing_keys(self) -> list[str]:
        """Keys present in English but not yet in this language (for translators)."""
        out: list[str] = []

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif _lookup(_load(self.lang), prefix) is None:
                out.append(prefix)

        walk(_load("en"), "")
        return out
