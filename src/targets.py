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

_DATA_DIR              = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

TARGETS_FILE             = _DATA_DIR / "targets.json"              # salesman WOD targets
PRINCIPAL_TARGETS_FILE   = _DATA_DIR / "principal_targets.json"    # principal monthly case targets
FOCUS_BRAND_FILE         = _DATA_DIR / "focus_brand.json"          # current focus brand
PARTY_TARGETS_FILE       = _DATA_DIR / "party_targets.json"        # manual per-outlet overrides
SALESMAN_OVERRIDES_FILE  = _DATA_DIR / "salesman_overrides.json"   # manual salesman case targets
TARGET_CONFIG_FILE       = _DATA_DIR / "target_config.json"        # smart-target params
BRAND_TARGETS_FILE       = _DATA_DIR / "brand_targets.json"        # principal × month × {brand: cases}


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


# ═══════════════════════════════════════════════════════════════════════════════
# PRINCIPAL-LEVEL MONTHLY TARGETS
# ═══════════════════════════════════════════════════════════════════════════════

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def load_principal_target(principal: str, month_str: str) -> dict | None:
    """Return {'total_cases': int, 'breakdown': dict, 'updated_at': str} or None."""
    return _read_json(PRINCIPAL_TARGETS_FILE).get(f"{principal}__{month_str}")


def save_principal_target(principal: str, month_str: str,
                          total_cases: int, breakdown: dict[str, int]) -> None:
    data = _read_json(PRINCIPAL_TARGETS_FILE)
    data[f"{principal}__{month_str}"] = {
        "total_cases":  int(total_cases),
        "breakdown":    {k: int(v) for k, v in breakdown.items()},
        "updated_at":   date.today().isoformat(),
    }
    _write_json(PRINCIPAL_TARGETS_FILE, data)


# ═══════════════════════════════════════════════════════════════════════════════
# FOCUS BRAND
# ═══════════════════════════════════════════════════════════════════════════════

def load_focus_brand() -> dict | None:
    """Return {'brand_id', 'brand_name', 'start_date', 'end_date', 'set_at'} or None."""
    return _read_json(FOCUS_BRAND_FILE) or None


def save_focus_brand(brand_id: int, brand_name: str,
                     start: date, end: date) -> None:
    _write_json(FOCUS_BRAND_FILE, {
        "brand_id":   int(brand_id),
        "brand_name": brand_name,
        "start_date": start.isoformat() if hasattr(start, "isoformat") else str(start),
        "end_date":   end.isoformat()   if hasattr(end,   "isoformat") else str(end),
        "set_at":     date.today().isoformat(),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# PER-PARTY MANUAL TARGET OVERRIDES
# ═══════════════════════════════════════════════════════════════════════════════

def load_party_target_override(party_id: str, month_str: str) -> dict | None:
    """Return the full override dict {'cases', 'note', 'updated_at'} or None."""
    return _read_json(PARTY_TARGETS_FILE).get(f"{party_id}__{month_str}")


def save_party_target_override(party_id: str, month_str: str,
                               cases: float, note: str = "") -> None:
    data = _read_json(PARTY_TARGETS_FILE)
    data[f"{party_id}__{month_str}"] = {
        "cases":      float(cases),
        "note":       note,
        "updated_at": date.today().isoformat(),
    }
    _write_json(PARTY_TARGETS_FILE, data)


def delete_party_target_override(party_id: str, month_str: str) -> None:
    data = _read_json(PARTY_TARGETS_FILE)
    data.pop(f"{party_id}__{month_str}", None)
    _write_json(PARTY_TARGETS_FILE, data)


# ═══════════════════════════════════════════════════════════════════════════════
# PER-SALESMAN MANUAL OVERRIDES (case target for the month)
# ═══════════════════════════════════════════════════════════════════════════════

def load_salesman_override(salesman: str, month_str: str) -> float | None:
    data = _read_json(SALESMAN_OVERRIDES_FILE).get(f"{salesman}__{month_str}")
    if data is None:
        return None
    return float(data.get("target_cases", 0))


def save_salesman_override(salesman: str, month_str: str,
                           target_cases: float) -> None:
    data = _read_json(SALESMAN_OVERRIDES_FILE)
    data[f"{salesman}__{month_str}"] = {
        "target_cases": float(target_cases),
        "updated_at":   date.today().isoformat(),
    }
    _write_json(SALESMAN_OVERRIDES_FILE, data)


def delete_salesman_override(salesman: str, month_str: str) -> None:
    data = _read_json(SALESMAN_OVERRIDES_FILE)
    data.pop(f"{salesman}__{month_str}", None)
    _write_json(SALESMAN_OVERRIDES_FILE, data)


# ═══════════════════════════════════════════════════════════════════════════════
# TARGET CONFIG (smart-target tuning knobs)
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_TARGET_CONFIG: dict[str, float] = {
    "min_l3m_threshold":     5.0,   # skip outlets averaging below this
    "standard_growth":       1.10,  # default push factor
    "growth_bonus":          1.15,  # when L3M trending up vs L6M
    "decline_factor":        1.00,  # when L3M trending down — recovery only
    "ceiling_multiplier":    1.5,   # cap at this × best month L12M
    "months_for_floor":      9,     # consistent-buyer floor threshold
}


def load_target_config() -> dict[str, float]:
    cfg = DEFAULT_TARGET_CONFIG.copy()
    cfg.update(_read_json(TARGET_CONFIG_FILE))
    return cfg


def save_target_config(config: dict[str, float]) -> None:
    _write_json(TARGET_CONFIG_FILE, {k: float(v) for k, v in config.items()})


# ═══════════════════════════════════════════════════════════════════════════════
# BRAND-LEVEL MONTHLY TARGETS (per principal, per month)
# ═══════════════════════════════════════════════════════════════════════════════

def load_brand_targets(principal: str, month_str: str) -> dict | None:
    """Return {'targets': {brand_name: cases, ...}, 'updated_at': ...} or None."""
    return _read_json(BRAND_TARGETS_FILE).get(f"{principal}__{month_str}")


def save_brand_targets(principal: str, month_str: str,
                       brand_targets: dict[str, float]) -> None:
    """brand_targets keyed by exact MsBrandMaster.BrandName."""
    data = _read_json(BRAND_TARGETS_FILE)
    # Drop zero/blank entries so the file stays tidy
    clean = {k: float(v) for k, v in brand_targets.items() if float(v) > 0}
    data[f"{principal}__{month_str}"] = {
        "targets":    clean,
        "updated_at": date.today().isoformat(),
    }
    _write_json(BRAND_TARGETS_FILE, data)


def delete_brand_targets(principal: str, month_str: str) -> None:
    data = _read_json(BRAND_TARGETS_FILE)
    data.pop(f"{principal}__{month_str}", None)
    _write_json(BRAND_TARGETS_FILE, data)
