"""src/targets.py — JSON-backed monthly target persistence.

Targets are stored as `data/targets.json` keyed by '{salesman}__{yyyy-MM}'
so the file is committed with the repo and survives Streamlit Cloud's
ephemeral filesystem.

Schema per entry:
    "Aabid__2026-05": {
        "wod_pct":         75,
        "target_outlets":  88,
        "updated_at":      "2026-05-17"
    }
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

TARGETS_FILE = Path(__file__).resolve().parent.parent / "data" / "targets.json"
TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_targets() -> dict[str, dict[str, Any]]:
    """Return the full targets dict.  Empty if the file is missing or invalid."""
    if not TARGETS_FILE.exists():
        return {}
    try:
        return json.loads(TARGETS_FILE.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def save_targets(targets: dict[str, dict[str, Any]]) -> None:
    TARGETS_FILE.write_text(
        json.dumps(targets, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _key(salesman: str, month_str: str) -> str:
    return f"{salesman}__{month_str}"


def get_target(
    salesman: str,
    month_str: str,
    default_wod: float = 70.0,
    default_outlets: int | None = None,
) -> dict[str, Any]:
    """Return the stored target, or sensible defaults if not yet set."""
    entry = load_targets().get(_key(salesman, month_str))
    if entry:
        return entry
    return {
        "wod_pct":        default_wod,
        "target_outlets": default_outlets,
        "updated_at":     None,
    }


def set_target(
    salesman: str,
    month_str: str,
    wod_pct: float,
    target_outlets: int,
) -> None:
    """Persist a single salesman/month target."""
    targets = load_targets()
    targets[_key(salesman, month_str)] = {
        "wod_pct":        float(wod_pct),
        "target_outlets": int(target_outlets),
        "updated_at":     date.today().isoformat(),
    }
    save_targets(targets)
