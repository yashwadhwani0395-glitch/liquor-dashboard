"""src/sales_plan.py — Sales Plan (target-driven outlet planning).

The daily-use page. Pick a principal + month, set a target, see exactly
which outlets must buy how many cases and what action each needs.
"""
from __future__ import annotations

import calendar
import io
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import format_inr, CASES_SQL_EXPR as _CASES, safe_section
from src.distribution import (
    SALESMAN_MAP,
    _load_master,
    _load_active_universe,
    _build_universes,
    _last_complete_month,
    _fmt_month,
    _prev_month_str,
)
from src.targets import (
    load_principal_target, save_principal_target,
    load_focus_brand,     save_focus_brand,
    load_party_target_override, save_party_target_override,
    delete_party_target_override,
    load_salesman_override, save_salesman_override, delete_salesman_override,
    load_target_config, save_target_config, DEFAULT_TARGET_CONFIG,
)

SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# FY-CASE filter (the one that drops TrVocItem FY duplicates)
_FY_JOIN = """
    AND vi.FinancialYear = CASE
        WHEN MONTH(h.VoucherDate) >= 4
        THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
             + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
        ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
             + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
    END
"""

# ── Channel labels ──────────────────────────────────────────────────────────
_LT_LABEL: dict[str, str] = {
    "180001": "FL-II Wine Shop",
    "180002": "FL-III Permit Room",
    "180004": "FL-BR-II Beer Shopee",
    "180005": "FL-IV Club",
    "180007": "FL-IV One Day",
}

# ── Principal → company id, color, sub-teams ────────────────────────────────
PRINCIPAL_TEAMS: dict[str, dict] = {
    "United Breweries": {
        "company_id": "C00039",
        "color":      "#1D9E75",
        "subteams": {
            "KW Beer":      ["Aabid", "Omkar"],
            "Institution":  ["Anand Raj", "Deepak Pangare", "Shashank Desai",
                             "Pranav", "Gajendra Das", "Amol Sathe", "Rahul Ghone"],
            # Cross Supply: special — no assigned salesman; identified by
            # AcType3ID = '130007' on the outlet. Handled in achievement calc.
            "Cross Supply": [],
        },
    },
    "United Spirits": {
        "company_id": "C00025",
        "color":      "#1B4F72",
        "subteams": {
            "Wine Shops":   ["Shashank", "Sachin"],
            "Permit Rooms": ["Atish", "Tulsiram", "Saurabh", "Miran", "Prashant"],
        },
    },
    "Diageo": {
        "company_id": "C00040",
        "color":      "#378ADD",
        "subteams": {
            "Wine Shops":   ["Ajay", "Deepak Patil"],
            "Permit Rooms": ["Atish", "Tulsiram", "Saurabh", "Miran", "Prashant"],
        },
    },
    "Brown-Forman": {
        "company_id": "C00056",
        "color":      "#EF9F27",
        "subteams": {
            "Wine Shops":   ["Ajay", "Deepak Patil"],
            "Institution":  ["Anand Raj", "Deepak Pangare", "Shashank Desai", "Pranav"],
        },
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_outlet_history(company_id: str,
                         end_date: date,
                         months_back: int = 7) -> pd.DataFrame:
    """Per-(party, month) cases + revenue for this principal — last N months."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            d.PartyID,
            ISNULL(p.PartyName, '(unknown)')          AS PartyName,
            ISNULL(p.LicenseTypeID, '')               AS LicenseTypeID,
            ISNULL(p.AcType3ID,     '')               AS AcType3ID,
            FORMAT(h.VoucherDate, 'yyyy-MM')           AS BillMonth,
            SUM({_CASES})        AS Cases,
            SUM(CAST(vi.TotalAmount AS FLOAT))         AS Revenue,
            MAX(h.VoucherDate)                          AS LastBill
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.FreeItemYN  = 'N'
            AND vi.ItemID      LIKE 'I%'
            {_FY_JOIN}
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo
                                          ORDER BY Amount DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator='D' AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d  ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsPartyMaster   p  ON p.PartyID  = d.PartyID
        JOIN MsItemMaster    im ON im.ItemID  = vi.ItemID
        JOIN MsBrandMaster   b  ON b.BrandID  = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND b.CompanyID   = ?
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, ?)
          AND CAST(h.VoucherDate AS date) <= ?
        GROUP BY d.PartyID, p.PartyName, p.LicenseTypeID, p.AcType3ID,
                 FORMAT(h.VoucherDate, 'yyyy-MM')
    """
    df = run_query(sql, (company_id, str(end_date), str(end_date)))
    if df.empty:
        # Return an empty DF with the same shape callers expect, so they
        # can safely do hist["Cases"].sum() etc. without KeyError.
        return pd.DataFrame(columns=[
            "PartyID", "PartyName", "LicenseTypeID", "AcType3ID",
            "BillMonth", "Cases", "Revenue", "LastBill", "Channel",
        ])
    df["Cases"]    = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
    df["Revenue"]  = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
    df["LastBill"] = pd.to_datetime(df["LastBill"], errors="coerce")
    df["Channel"]  = df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_brand_buyers(company_id: str,
                       brand_id: int,
                       end_date: date) -> pd.DataFrame:
    """Per-party cases of THIS brand + cases of ANY brand from this principal,
    split by current month vs prior month. Used by Focus Brand section."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    # Current month + previous 5 months for context
    sql = f"""
        SELECT
            d.PartyID,
            ISNULL(p.PartyName, '(unknown)')                   AS PartyName,
            ISNULL(p.LicenseTypeID, '')                        AS LicenseTypeID,
            FORMAT(h.VoucherDate, 'yyyy-MM')                    AS BillMonth,
            SUM(CASE WHEN b.BrandID = ?
                THEN {_CASES}
                ELSE 0 END)                                     AS BrandCases,
            SUM({_CASES})                                       AS PrincipalCases,
            MAX(CASE WHEN b.BrandID = ? THEN h.VoucherDate END) AS BrandLastBill
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.FreeItemYN  = 'N'
            AND vi.ItemID      LIKE 'I%'
            {_FY_JOIN}
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo
                                          ORDER BY Amount DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator='D' AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d  ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsPartyMaster   p  ON p.PartyID  = d.PartyID
        JOIN MsItemMaster    im ON im.ItemID  = vi.ItemID
        JOIN MsBrandMaster   b  ON b.BrandID  = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND b.CompanyID   = ?
          AND h.VoucherDate >= DATEADD(MONTH, -6, ?)
          AND CAST(h.VoucherDate AS date) <= ?
        GROUP BY d.PartyID, p.PartyName, p.LicenseTypeID,
                 FORMAT(h.VoucherDate, 'yyyy-MM')
    """
    params = (brand_id, brand_id, company_id, str(end_date), str(end_date))
    df = run_query(sql, params)
    if df.empty:
        return pd.DataFrame(columns=[
            "PartyID", "PartyName", "LicenseTypeID", "BillMonth",
            "BrandCases", "PrincipalCases", "BrandLastBill", "Channel",
        ])
    for c in ("BrandCases", "PrincipalCases"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["BrandLastBill"] = pd.to_datetime(df["BrandLastBill"], errors="coerce")
    df["Channel"]       = df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return df


@st.cache_data(ttl=86400, show_spinner=False)   # master brand list — 24h
def _load_brands_for_principal(company_id: str) -> pd.DataFrame:
    """All brands belonging to a principal — used for focus-brand dropdown."""
    df = run_query(
        """
        SELECT DISTINCT b.BrandID, b.BrandName
        FROM MsBrandMaster b
        WHERE b.CompanyID = ?
        ORDER BY b.BrandName
        """,
        (company_id,),
    )
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_bounds(any_day: date) -> tuple[date, date]:
    first = any_day.replace(day=1)
    _, last_dom = calendar.monthrange(first.year, first.month)
    return first, first.replace(day=last_dom)


def _last_n_months(n: int, end_month: str) -> list[str]:
    yr, mo = int(end_month[:4]), int(end_month[5:])
    out = []
    for i in range(n):
        out.append(f"{yr:04d}-{mo:02d}")
        mo -= 1
        if mo <= 0:
            mo += 12
            yr -= 1
    return list(reversed(out))


def _pace_metrics(achieved: float, target: float, op_month: date) -> dict:
    today      = date.today()
    first, last = _month_bounds(op_month)
    days_passed = max(1, (min(today, last) - first).days + 1)
    days_in     = (last - first).days + 1
    days_left   = max(0, days_in - days_passed)

    pace_per_day  = (target - achieved) / max(days_left, 1) if days_left else 0
    if days_passed > 0:
        projected = achieved * days_in / days_passed
    else:
        projected = achieved
    pct_achieved  = (achieved / target * 100) if target else 0.0
    projected_pct = (projected / target * 100) if target else 0.0
    return {
        "days_passed":   days_passed,
        "days_in":       days_in,
        "days_left":     days_left,
        "pace_per_day":  pace_per_day,
        "projected":     projected,
        "pct_achieved":  pct_achieved,
        "projected_pct": projected_pct,
    }


def _calculate_outlet_target(metrics: dict, cfg: dict,
                             party_id: str, month_str: str) -> dict:
    """Smart per-outlet target with full transparency.

    metrics keys: l3m_avg, l6m_avg, l12m_avg, same_month_ly,
                  best_month_l12m, months_billed_l12m
    Returns: {target, reason, recommendation, ceiling_applied, threshold_skip,
              has_override}
    """
    # 0) Manual override always wins
    ov = load_party_target_override(party_id, month_str)
    if ov is not None:
        return {
            "target":           float(ov.get("cases", 0)),
            "reason":           "Manual override",
            "recommendation":   ov.get("note", ""),
            "ceiling_applied":  False,
            "threshold_skip":   False,
            "has_override":     True,
        }

    l3m  = metrics["l3m_avg"]
    l6m  = metrics["l6m_avg"]
    sm_ly = metrics["same_month_ly"]
    best  = metrics["best_month_l12m"]
    months_billed = metrics["months_billed_l12m"]

    threshold = cfg["min_l3m_threshold"]

    # 1) Threshold — outlets below threshold get no target
    if l3m < threshold:
        return {
            "target":          None,
            "reason":          f"Below threshold (L3M {l3m:.1f} < {threshold:.0f} cs/mo)",
            "recommendation":  "Bill if visited, no target burden",
            "ceiling_applied": False,
            "threshold_skip":  True,
            "has_override":    False,
        }

    # 2) Base = blend of L3M and same-month LY (if positive)
    if sm_ly > 0:
        base = (l3m + sm_ly) / 2.0
        base_label = f"avg(L3M {l3m:.0f}, LY-same {sm_ly:.0f})"
    else:
        base = l3m
        base_label = f"L3M {l3m:.0f}"

    # 3) Growth factor based on trend
    if l3m > l6m * 1.10:
        gf, trend_label = cfg["growth_bonus"], "Growing"
    elif l3m < l6m * 0.85:
        gf, trend_label = cfg["decline_factor"], "Declining — recovery"
    else:
        gf, trend_label = cfg["standard_growth"], "Stable"

    target_raw = base * gf

    # 4) Ceiling — at most ceiling_multiplier × best month
    ceiling = best * cfg["ceiling_multiplier"]
    ceiling_applied = (ceiling > 0) and (target_raw > ceiling)
    target = min(target_raw, ceiling) if ceiling > 0 else target_raw

    # 5) Floor — consistent buyers (≥9/12 months) never below L3M avg
    if months_billed >= cfg["months_for_floor"] and l3m > target:
        target = l3m
        floor_applied = True
    else:
        floor_applied = False

    target = max(0, round(target))

    reason = f"{base_label} × {gf:.2f} ({trend_label})"
    if ceiling_applied:
        reason += f" · capped at 1.5×best ({best:.0f})"
    if floor_applied:
        reason += " · floor=L3M"

    return {
        "target":          float(target),
        "reason":          reason,
        "recommendation": "",
        "ceiling_applied": ceiling_applied,
        "threshold_skip":  False,
        "has_override":    False,
    }


def _action_for(this_mo: float, target: float | None, prev_mo: float) -> tuple[str, str]:
    """Return (action_label, hex_bg_color)."""
    if target is None:
        return ("· skip",  "#f3f4f6")
    if target > 0 and this_mo >= target:
        return ("✅ HOLD",  "#dcfce7")
    if target > 0 and this_mo >= target * 0.8:
        return ("🟡 GROW",  "#fef3c7")
    if this_mo == 0 and prev_mo > 0:
        return ("🔴 PUSH",  "#fee2e2")
    if target > 0 and this_mo < target * 0.5:
        return ("🔴 PUSH",  "#fee2e2")
    return ("🟡 GROW",  "#fef3c7")


def _compute_outlet_plan(hist_df: pd.DataFrame,
                        op_month: str,
                        universe: frozenset,
                        cfg: dict) -> pd.DataFrame:
    """Build the outlet-by-outlet plan with full metrics + smart target."""
    if hist_df.empty:
        return pd.DataFrame()

    # 12 prior months + current
    last_12 = _last_n_months(13, op_month)[:-1]   # 12 months prior
    last_6  = last_12[-6:]
    last_3  = last_12[-3:]
    prev_month = last_12[-1]

    # Same month last year
    yr, mo = int(op_month[:4]), int(op_month[5:])
    ly_month = f"{yr-1:04d}-{mo:02d}"

    h = hist_df[hist_df["BillMonth"].isin(last_12 + [op_month])].copy()
    party_info = (
        h.groupby(["PartyID", "PartyName", "Channel", "AcType3ID"], as_index=False)
        .agg(LastBill=("LastBill", "max"))
    )

    def _avg(months, label):
        return (
            h[h["BillMonth"].isin(months)]
            .groupby("PartyID")["Cases"].sum() / len(months)
        ).rename(label).reset_index()

    avg12 = _avg(last_12, "Avg12")
    avg6  = _avg(last_6,  "Avg6")
    avg3  = _avg(last_3,  "Avg3")

    # Best month + months-billed across last 12
    last12_h = h[h["BillMonth"].isin(last_12)]
    best12 = (
        last12_h.groupby(["PartyID", "BillMonth"])["Cases"].sum()
        .reset_index().groupby("PartyID")["Cases"].max()
        .rename("Best12").reset_index()
    )
    mb12 = (
        last12_h[last12_h["Cases"] > 0]
        .groupby("PartyID")["BillMonth"].nunique()
        .rename("MonthsBilled12").reset_index()
    )

    # Previous month / current month / same-month-LY
    prev = (
        h[h["BillMonth"] == prev_month]
        .groupby("PartyID")["Cases"].sum().rename("PrevMo").reset_index()
    )
    curr = (
        h[h["BillMonth"] == op_month]
        .groupby("PartyID")["Cases"].sum().rename("ThisMo").reset_index()
    )
    ly_same = (
        h[h["BillMonth"] == ly_month]
        .groupby("PartyID")["Cases"].sum().rename("SameMoLy").reset_index()
    )

    df = (
        party_info
        .merge(avg12, on="PartyID", how="left")
        .merge(avg6,  on="PartyID", how="left")
        .merge(avg3,  on="PartyID", how="left")
        .merge(best12, on="PartyID", how="left")
        .merge(mb12,   on="PartyID", how="left")
        .merge(prev,   on="PartyID", how="left")
        .merge(curr,   on="PartyID", how="left")
        .merge(ly_same, on="PartyID", how="left")
        .fillna({
            "Avg12": 0.0, "Avg6": 0.0, "Avg3": 0.0,
            "Best12": 0.0, "MonthsBilled12": 0,
            "PrevMo": 0.0, "ThisMo": 0.0, "SameMoLy": 0.0,
        })
    )

    # Bring in universe outlets not in history
    seen = set(df["PartyID"])
    extras = [pid for pid in universe if pid not in seen]
    if extras:
        in_ph = ",".join("?" * len(extras))
        try:
            extra_rows = run_query(
                f"""
                SELECT p.PartyID,
                       ISNULL(p.PartyName, '(unknown)')   AS PartyName,
                       ISNULL(p.LicenseTypeID, '')        AS LicenseTypeID,
                       ISNULL(p.AcType3ID,     '')        AS AcType3ID
                FROM MsPartyMaster p
                WHERE p.PartyID IN ({in_ph})
                """,
                tuple(extras),
            )
            if not extra_rows.empty:
                extra_rows["Channel"] = extra_rows["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
                for col in ("Avg12","Avg6","Avg3","Best12","PrevMo","ThisMo","SameMoLy"):
                    extra_rows[col] = 0.0
                extra_rows["MonthsBilled12"] = 0
                extra_rows["LastBill"] = pd.NaT
                df = pd.concat(
                    [df, extra_rows[[
                        "PartyID","PartyName","Channel","AcType3ID","LastBill",
                        "Avg12","Avg6","Avg3","Best12","MonthsBilled12",
                        "PrevMo","ThisMo","SameMoLy",
                    ]]],
                    ignore_index=True,
                )
        except Exception:
            pass

    # Restrict to universe outlets
    df = df[df["PartyID"].isin(universe)].copy().reset_index(drop=True)
    if df.empty:
        return df

    # Compute target + reason per outlet
    def _calc(row):
        metrics = {
            "l3m_avg":            float(row["Avg3"]),
            "l6m_avg":            float(row["Avg6"]),
            "l12m_avg":           float(row["Avg12"]),
            "same_month_ly":      float(row["SameMoLy"]),
            "best_month_l12m":    float(row["Best12"]),
            "months_billed_l12m": int(row["MonthsBilled12"]),
        }
        return _calculate_outlet_target(metrics, cfg, row["PartyID"], op_month)

    results = df.apply(_calc, axis=1)
    df["Target"]          = [r["target"] for r in results]
    df["Reason"]          = [r["reason"] for r in results]
    df["CeilingApplied"]  = [r["ceiling_applied"] for r in results]
    df["ThresholdSkip"]   = [r["threshold_skip"] for r in results]
    df["HasOverride"]     = [r["has_override"] for r in results]

    # Action + gap
    actions = [
        _action_for(this_mo, tgt, prev)
        for this_mo, tgt, prev in zip(df["ThisMo"], df["Target"], df["PrevMo"])
    ]
    df["Action"] = [a for a, _ in actions]
    df["RowBg"]  = [c for _, c in actions]
    df["Gap"]    = df.apply(
        lambda r: (r["ThisMo"] - r["Target"]) if r["Target"] is not None else 0.0,
        axis=1,
    )

    return df


# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _hero_card(principal: str, color: str, month_label: str,
               target_total: int, achieved: float, pace: dict) -> str:
    pct = max(0, min(100, pace["pct_achieved"]))
    return f"""
    <div style='background:{color};color:#fff;padding:24px;border-radius:12px;
                display:flex;justify-content:space-between;align-items:center;
                margin-bottom:18px;border-bottom:3px solid #E8A838'>
        <div>
            <div style='font-size:11px;text-transform:uppercase;
                        letter-spacing:0.5px;opacity:0.75'>
                {month_label} target · {principal}
            </div>
            <div style='font-size:2rem;font-weight:700;margin-top:4px'>
                {target_total:,} cases
            </div>
            <div style='font-size:0.78rem;opacity:0.78;margin-top:4px'>
                Days left: <b>{pace['days_left']}</b>  ·
                Pace needed: <b>{pace['pace_per_day']:,.0f}</b> cs/day
            </div>
        </div>
        <div style='text-align:right;min-width:280px'>
            <div style='font-size:0.72rem;opacity:0.78'>
                Achieved <b>{achieved:,.0f}</b> cs ·
                <b>{pct:.1f}%</b>
            </div>
            <div style='background:rgba(255,255,255,0.15);height:10px;
                        border-radius:5px;margin:8px 0;overflow:hidden'>
                <div style='background:#FAC775;height:100%;
                            width:{pct}%'></div>
            </div>
            <div style='font-size:0.78rem;opacity:0.85;margin-top:6px'>
                At current pace: <b>{pace['projected']:,.0f}</b> cs
                ({pace['projected_pct']:.0f}% of target)
            </div>
        </div>
    </div>
    """


def _no_target_form(principal: str, color: str, month_str: str,
                    subteams: dict[str, list[str]]) -> None:
    st.markdown(f"""
    <div style='background:{color};color:#fff;padding:20px;border-radius:10px;
                text-align:center;margin-bottom:18px'>
        <div style='font-size:0.78rem;opacity:0.78;text-transform:uppercase;
                    letter-spacing:0.05em'>No target set yet</div>
        <div style='font-size:1.4rem;font-weight:700;margin-top:6px'>
            Set the {_fmt_month(month_str)} target for {principal}
        </div>
    </div>
    """, unsafe_allow_html=True)

    with st.form(f"set_tgt_{principal}_{month_str}"):
        cols = st.columns(len(subteams))
        breakdown_inputs: dict[str, int] = {}
        for col, (team_name, _) in zip(cols, subteams.items()):
            with col:
                v = st.number_input(
                    f"{team_name} cases", min_value=0, step=500,
                    value=0, key=f"tgt_{principal}_{team_name}_{month_str}",
                )
                breakdown_inputs[team_name] = int(v)
        total = sum(breakdown_inputs.values())
        st.markdown(
            f"**Total target: {total:,} cases**  "
            f"<span style='color:#6b7280;font-size:0.85rem'>"
            f"(auto-summed from sub-targets)</span>",
            unsafe_allow_html=True,
        )
        submitted = st.form_submit_button("💾 Save target", type="primary")
        if submitted:
            if total == 0:
                st.warning("Enter at least one sub-target.")
            else:
                save_principal_target(principal, month_str, total, breakdown_inputs)
                st.success(f"Target saved: {total:,} cases for {principal}")
                st.rerun()


def _subtarget_cards(target_entry: dict, achieved_by_team: dict[str, float]) -> None:
    breakdown = target_entry.get("breakdown", {})
    cols = st.columns(len(breakdown) if breakdown else 1)
    for col, (team, tgt) in zip(cols, breakdown.items()):
        ach = float(achieved_by_team.get(team, 0))
        pct = (ach / tgt * 100) if tgt else 0
        col_bg = "#dcfce7" if pct >= 80 else ("#fef3c7" if pct >= 50 else "#fee2e2")
        with col:
            st.markdown(f"""
            <div style='background:{col_bg};padding:10px 14px;border-radius:6px;
                        border-left:4px solid #1B4F72'>
                <div style='font-size:0.7rem;color:#374151;
                            text-transform:uppercase;letter-spacing:0.04em'>{team}</div>
                <div style='font-size:1.15rem;font-weight:700;color:#111827;
                            margin-top:2px'>
                    {ach:,.0f} <span style='font-size:0.78rem;
                                            font-weight:500;color:#6b7280'>/ {tgt:,}</span>
                </div>
                <div style='font-size:0.72rem;color:#374151;font-weight:600'>
                    {pct:.0f}% of target
                </div>
            </div>
            """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _section_kpis(universe: frozenset, hist_df: pd.DataFrame,
                  op_month: str, prev_month: str) -> None:
    uni_size = len(universe)
    cm_set = frozenset(
        hist_df[hist_df["BillMonth"] == op_month]["PartyID"]
    ) & universe
    pm_set = frozenset(
        hist_df[hist_df["BillMonth"] == prev_month]["PartyID"]
    ) & universe
    lapsed = pm_set - cm_set

    wod = (len(cm_set) / uni_size * 100) if uni_size else 0.0

    c1, c2, c3 = st.columns(3)
    c1.metric("Active Universe", f"{uni_size:,}",
              "Outlets that billed this principal in last 12 months")
    c2.metric(f"Billed in {_fmt_month(op_month)}",
              f"{len(cm_set):,}",
              f"WOD {wod:.1f}%")
    c3.metric("Outlets at Risk",
              f"{len(lapsed):,}",
              "Billed last month, not this",
              delta_color="inverse")


def _section_outlet_plan(plan_df: pd.DataFrame,
                         all_subteams: dict[str, list[str]],
                         op_month: str) -> None:
    st.subheader("Outlet-by-outlet plan for this month")
    st.caption(
        "Smart target: base × growth-factor, capped at 1.5×best-month, "
        "floored at L3M for consistent buyers. Outlets averaging < threshold "
        "get no target. Manual overrides always win — click 'Set override' below."
    )

    if plan_df.empty:
        st.info("No outlets to plan for this principal."); return

    df = plan_df.copy()
    # Bucket counts
    n_total    = len(df)
    n_threshold = int(df["ThresholdSkip"].sum())
    n_ceiling  = int(df["CeilingApplied"].sum())
    n_override = int(df["HasOverride"].sum())

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Total outlets", f"{n_total:,}")
    s2.metric("Below threshold (no target)", f"{n_threshold:,}")
    s3.metric("Ceiling applied", f"{n_ceiling:,}")
    s4.metric("Manual overrides", f"{n_override:,}")

    # Order: PUSH first then GROW then HOLD, within each by Avg6 desc
    rank = df["Action"].map({"🔴 PUSH": 0, "🟡 GROW": 1,
                              "✅ HOLD": 2, "· skip": 3}).fillna(4)
    df["_rank"] = rank
    df = df.sort_values(["_rank", "Avg6"], ascending=[True, False])

    df["LastBillStr"] = pd.to_datetime(df["LastBill"], errors="coerce") \
        .dt.strftime("%d %b %Y").fillna("Never")
    df["TargetDisp"] = df["Target"].apply(
        lambda v: "—" if v is None or pd.isna(v) else f"{int(v):,}"
    )

    display = df[[
        "Action", "PartyName", "Channel", "Avg3", "Avg6",
        "TargetDisp", "ThisMo", "Gap", "Reason", "LastBillStr",
    ]].rename(columns={
        "PartyName":    "Party",
        "Avg3":         "L3M avg/mo",
        "Avg6":         "L6M avg/mo",
        "TargetDisp":   "Target",
        "ThisMo":       "So Far",
        "Gap":          "Gap",
        "LastBillStr":  "Last Bill",
    })

    def _row_style(row):
        bg = df.iloc[row.name]["RowBg"]
        return [f"background-color:{bg}"] * len(row)

    styled = (
        display.style
        .apply(_row_style, axis=1)
        .format({
            "L3M avg/mo":   "{:,.1f}",
            "L6M avg/mo":   "{:,.1f}",
            "So Far":       "{:,.1f}",
            "Gap":          "{:+,.1f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=420)

    # ── Per-outlet detail expander ──
    with st.expander("🔍 Explore one outlet (12-month history + target reasoning)"):
        # Sort by L6M desc for the dropdown
        pick = st.selectbox(
            "Outlet",
            options=df.sort_values("Avg6", ascending=False)["PartyID"].tolist(),
            format_func=lambda pid: df[df["PartyID"]==pid]["PartyName"].iloc[0],
            key=f"plan_explore_{op_month}",
        )
        if pick:
            row = df[df["PartyID"] == pick].iloc[0]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("L3M avg", f"{row['Avg3']:,.1f}")
            c2.metric("L6M avg", f"{row['Avg6']:,.1f}")
            c3.metric("Best L12M", f"{row['Best12']:,.1f}")
            c4.metric("Months billed", f"{int(row['MonthsBilled12'])}/12")

            d1, d2 = st.columns([2, 1])
            with d1:
                tgt_label = "—" if row["Target"] is None or pd.isna(row["Target"]) else f"{int(row['Target']):,}"
                st.markdown(f"**Target:** {tgt_label} cases  ·  **Reason:** {row['Reason']}")
                if row.get("CeilingApplied"):
                    st.warning(f"Ceiling applied at 1.5 × best month ({row['Best12']:.0f})")
                if row.get("ThresholdSkip"):
                    st.info("This outlet is below the threshold — no target burden.")
            with d2:
                st.markdown(f"**Same month LY:** {row['SameMoLy']:,.1f} cs")
                st.markdown(f"**Previous month:** {row['PrevMo']:,.1f} cs")
                st.markdown(f"**This month so far:** {row['ThisMo']:,.1f} cs")

        # Manual override form
        with st.form(f"override_{op_month}"):
            colp, colc, coln, colb = st.columns([3, 1, 3, 1])
            with colp:
                ov_pick = st.selectbox(
                    "Outlet to override",
                    options=df["PartyID"].tolist(),
                    format_func=lambda pid: df[df["PartyID"]==pid]["PartyName"].iloc[0],
                    key=f"ov_pick_{op_month}",
                )
            with colc:
                cur = df[df["PartyID"] == ov_pick]["Target"].iloc[0] if ov_pick else 0
                ov_cases = st.number_input(
                    "Target cases", min_value=0.0, step=1.0,
                    value=float(cur) if cur is not None else 0.0,
                    key=f"ov_cases_{op_month}",
                )
            with coln:
                ov_note = st.text_input("Reason for override", "",
                                        key=f"ov_note_{op_month}")
            with colb:
                ov_submit = st.form_submit_button("Save")
            if ov_submit and ov_pick:
                save_party_target_override(ov_pick, op_month, float(ov_cases), ov_note)
                st.success(f"Saved override: {ov_cases:.0f} cases")
                st.rerun()

    # ── Excel export ──
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        export = df.drop(columns=["_rank", "RowBg"], errors="ignore").copy()
        export.to_excel(writer, sheet_name="Outlet Plan", index=False)
    buf.seek(0)
    st.download_button(
        "📥 Download Plan (Excel)", buf,
        file_name=f"sales_plan_{op_month}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _section_focus_brand(company_id: str,
                         op_month: str, prev_month: str,
                         brands_df: pd.DataFrame) -> None:
    st.caption("Pick a brand to push across all outlets — see recover / convert / grow lists")

    if brands_df.empty:
        st.info("No brands for this principal."); return

    focus = load_focus_brand() or {}
    brand_options = brands_df["BrandName"].tolist()
    default_idx = 0
    if focus.get("brand_name") in brand_options:
        default_idx = brand_options.index(focus["brand_name"])

    col_pick, col_btn = st.columns([3, 1])
    with col_pick:
        chosen = st.selectbox("Select brand", brand_options,
                              index=default_idx, key=f"focus_pick_{op_month}")
    chosen_row = brands_df[brands_df["BrandName"] == chosen].iloc[0]
    brand_id = int(chosen_row["BrandID"])
    with col_btn:
        if st.button("Set as Focus", type="primary",
                     key=f"focus_set_{op_month}"):
            today = date.today()
            save_focus_brand(brand_id, chosen, today, today + timedelta(days=14))
            st.success(f"Focus brand set: {chosen}")

    # Load brand-buyer matrix
    end_d = _month_bounds(date(int(op_month[:4]), int(op_month[5:]), 1))[1]
    bb = _load_brand_buyers(company_id, brand_id, end_d)
    if bb.empty:
        st.info("No billing data for this brand period."); return

    # Aggregate per party across months
    def _by_party(df, this_mo):
        out = (
            df.groupby(["PartyID", "PartyName", "Channel"], as_index=False)
            .agg(
                BrandCM=("BrandCases",     "sum"),
                PrincCM=("PrincipalCases", "sum"),
            )
        )
        # Add this-month-only and prev-month-only columns
        cm = df[df["BillMonth"] == this_mo].groupby("PartyID")["BrandCases"].sum().rename("BrandThis").reset_index()
        pm = df[df["BillMonth"] == prev_month].groupby("PartyID")["BrandCases"].sum().rename("BrandPrev").reset_index()
        out = out.merge(cm, on="PartyID", how="left").merge(pm, on="PartyID", how="left").fillna(0)
        # Last brand bill
        lb = df.groupby("PartyID")["BrandLastBill"].max().rename("LastBrandBill").reset_index()
        out = out.merge(lb, on="PartyID", how="left")
        return out

    party_brand = _by_party(bb, op_month)

    # ── Three buckets ──
    # 1. RECOVER: bought this brand last month, not this month
    recover = party_brand[
        (party_brand["BrandPrev"] > 0) & (party_brand["BrandThis"] == 0)
    ].copy()

    # 2. CONVERT: buys other brands from this principal (PrincCM > 0) but never this brand (BrandCM == 0)
    convert = party_brand[
        (party_brand["PrincCM"] > 0) & (party_brand["BrandCM"] == 0)
    ].copy()

    # 3. GROW: already buying this brand (BrandCM > 0 AND BrandThis > 0)
    grow = party_brand[
        party_brand["BrandThis"] > 0
    ].copy()

    col_r, col_c, col_g = st.columns(3)

    with col_r:
        st.markdown(f"#### 🔴 RECOVER · {len(recover)}")
        st.caption(f"Bought {chosen} last month, not this")
        if recover.empty:
            st.success("No recoveries needed.")
        else:
            tbl = recover.sort_values("BrandPrev", ascending=False).head(15).copy()
            tbl["Last Bill"] = pd.to_datetime(tbl["LastBrandBill"], errors="coerce") \
                .dt.strftime("%d %b %Y").fillna("Never")
            disp = tbl[["PartyName", "Channel", "BrandPrev", "BrandThis", "Last Bill"]] \
                .rename(columns={"PartyName": "Party", "BrandPrev": "Prev",
                                 "BrandThis": "This"})
            st.dataframe(
                disp.style.format({"Prev": "{:,.1f}", "This": "{:,.1f}"}),
                use_container_width=True, hide_index=True,
            )

    with col_c:
        st.markdown(f"#### 🟡 CONVERT · {len(convert)}")
        st.caption(f"Buys other brands of this principal · never {chosen}")
        if convert.empty:
            st.info("No conversion opportunities.")
        else:
            tbl = convert.sort_values("PrincCM", ascending=False).head(15).copy()
            disp = tbl[["PartyName", "Channel", "PrincCM"]] \
                .rename(columns={"PartyName": "Party", "PrincCM": "Avg cs (6mo principal)"})
            disp["Avg cs (6mo principal)"] = (disp["Avg cs (6mo principal)"] / 6).round(1)
            st.dataframe(
                disp.style.format({"Avg cs (6mo principal)": "{:,.1f}"}),
                use_container_width=True, hide_index=True,
            )

    with col_g:
        st.markdown(f"#### 🟢 GROW · {len(grow)}")
        st.caption(f"Already buying {chosen}")
        if grow.empty:
            st.info("No active buyers yet.")
        else:
            tbl = grow.sort_values("BrandCM", ascending=False).head(15).copy()
            disp = tbl[["PartyName", "Channel", "BrandPrev", "BrandThis"]] \
                .rename(columns={"PartyName": "Party",
                                 "BrandPrev": "Last Mo", "BrandThis": "This Mo"})
            st.dataframe(
                disp.style.format({"Last Mo": "{:,.1f}", "This Mo": "{:,.1f}"}),
                use_container_width=True, hide_index=True,
            )

    # Export
    if not (recover.empty and convert.empty and grow.empty):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            recover.to_excel(writer, sheet_name="Recover", index=False)
            convert.to_excel(writer, sheet_name="Convert", index=False)
            grow.to_excel(   writer, sheet_name="Grow",    index=False)
        buf.seek(0)
        st.download_button(
            "📥 Focus Brand Action Lists", buf,
            file_name=f"focus_{chosen}_{op_month}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def _compute_salesman_share(team_salesmen: list[str], hist: pd.DataFrame,
                            l3_months: list[str],
                            universes_by_p: dict[str, dict[str, frozenset]],
                            cid: str) -> tuple[dict[str, dict], float]:
    """Return ({sm: {l3m_cases, share_pct}}, team_total_l3m)."""
    l3 = hist[hist["BillMonth"].isin(l3_months)]
    per_sm: dict[str, dict] = {}
    total = 0.0
    for sm in team_salesmen:
        uni = universes_by_p.get(sm, {}).get(cid, frozenset())
        cases = float(l3[l3["PartyID"].isin(uni)]["Cases"].sum())
        per_sm[sm] = {"l3m_cases": cases, "share_pct": 0.0,
                      "universe_size": len(uni)}
        total += cases
    if total > 0:
        for sm in per_sm:
            per_sm[sm]["share_pct"] = per_sm[sm]["l3m_cases"] / total * 100
    return per_sm, total


def _section_salesman_scoreboard(principal: str,
                                 universes: dict[str, frozenset],
                                 universes_by_p: dict[str, dict[str, frozenset]],
                                 master: pd.DataFrame,
                                 hist: pd.DataFrame,
                                 op_month: str,
                                 target_entry: dict | None,
                                 allocation_method: str) -> None:
    st.subheader("Salesman scoreboard — this month")

    cfg_p   = PRINCIPAL_TEAMS[principal]
    cid     = cfg_p["company_id"]
    subteams = cfg_p["subteams"]
    breakdown = (target_entry or {}).get("breakdown", {})

    # Compute L3M share per team
    l3_months = _last_n_months(4, op_month)[:-1]  # 3 prior months

    rows = []
    for team_name, sms in subteams.items():
        if not sms:  # e.g. Cross Supply
            continue
        team_target = breakdown.get(team_name, 0)
        per_sm, team_total = _compute_salesman_share(
            sms, hist, l3_months, universes_by_p, cid,
        )

        for sm in sms:
            d = per_sm[sm]
            # Target allocation
            if allocation_method == "Equal split":
                auto_target = team_target / len(sms) if sms else 0
            elif allocation_method == "Manual" and team_total == 0:
                auto_target = team_target / len(sms) if sms else 0
            else:  # Auto by L3M share (default)
                auto_target = team_target * d["share_pct"] / 100 if team_total else (
                    team_target / len(sms) if sms else 0
                )

            ov = load_salesman_override(sm, op_month)
            effective = ov if ov is not None else auto_target

            # Cases this month
            pu = universes_by_p.get(sm, {}).get(cid, frozenset())
            sub = master[
                (master["CompanyID"] == cid) &
                (master["PartyID"].isin(pu)) &
                (master["BillMonth"] == op_month)
            ]
            billed   = sub["PartyID"].nunique()
            cases    = float(sub["Cases"].sum())
            wod      = (billed / max(len(pu), 1) * 100)
            pct_done = (cases / effective * 100) if effective else 0.0

            if pct_done >= 80:    status = "✅ On Track"
            elif pct_done >= 50:  status = "⚠️ Behind"
            else:                 status = "🔴 Critical"

            rows.append({
                "Salesman":     sm,
                "Team":         team_name,
                "Universe":     len(pu),
                "L3M Avg/mo":   round(d["l3m_cases"] / 3, 1),
                "Share %":      round(d["share_pct"], 1),
                "Auto Target":  int(round(auto_target)),
                "Override":     int(round(ov)) if ov is not None else 0,
                "Effective":    int(round(effective)),
                "This Mo":      round(cases, 1),
                "WOD %":        round(wod, 1),
                "% Done":       round(pct_done, 1),
                "Status":       status,
            })

    if not rows:
        st.info("Set a principal target first to see allocated salesman targets.")
        return

    df = pd.DataFrame(rows).sort_values("% Done", ascending=True)

    def _wod_style(v):
        try:    n = float(v)
        except (TypeError, ValueError): return ""
        if n >= 70:  return "color:#16a34a; font-weight:600"
        if n >= 50:  return "color:#854d0e; font-weight:600"
        return "color:#dc2626; font-weight:600"

    styled = (
        df.style
        .format({
            "Universe": "{:,}",
            "L3M Avg/mo": "{:.1f}",
            "Share %": "{:.1f}",
            "Auto Target": "{:,}",
            "Override": lambda v: "—" if v == 0 else f"{v:,}",
            "Effective": "{:,}",
            "This Mo": "{:,.1f}",
            "WOD %": "{:.1f}",
            "% Done": "{:.1f}",
        })
        .map(_wod_style, subset=["WOD %"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Override input
    with st.expander("✏️ Set manual salesman override"):
        with st.form(f"sm_override_{op_month}"):
            o1, o2, o3 = st.columns([3, 1, 1])
            with o1:
                sm_pick = st.selectbox("Salesman", df["Salesman"].tolist(),
                                       key=f"sm_ov_pick_{op_month}")
            with o2:
                cur_eff = int(df[df["Salesman"]==sm_pick]["Effective"].iloc[0]) if sm_pick else 0
                sm_cases = st.number_input("Target cases", min_value=0,
                                           step=100, value=cur_eff,
                                           key=f"sm_ov_cases_{op_month}")
            with o3:
                ov_submit = st.form_submit_button("Save override")
            if ov_submit and sm_pick:
                save_salesman_override(sm_pick, op_month, float(sm_cases))
                st.success(f"Saved override: {sm_cases:,} cases for {sm_pick}")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("Sales Plan")
    st.caption("Target-driven outlet planning — what each party should buy this month")
    st.divider()

    # ── Principal selector ──
    principal = st.radio(
        "Principal",
        list(PRINCIPAL_TEAMS.keys()),
        horizontal=True, index=0,
        key="sp_principal",
    )
    cfg = PRINCIPAL_TEAMS[principal]
    cid = cfg["company_id"]
    color = cfg["color"]

    # ── Month selector ──
    today = date.today()
    op_month_date = st.date_input(
        "Month",
        value=today.replace(day=1),
        key="sp_month",
    )
    if isinstance(op_month_date, (tuple, list)):
        op_month_date = op_month_date[0] if op_month_date else today.replace(day=1)
    op_month = _month_str(op_month_date)
    prev_month = _prev_month_str(op_month)

    # ── Load data ──
    with st.spinner("Loading…"):
        master      = _load_master(13)
        uni_df      = _load_active_universe()

    if master.empty or uni_df.empty:
        st.error("No data available. Check DB connection."); return

    universes, universes_by_p = _build_universes(uni_df)

    # Principal universe = union across salesmen who handle this principal
    relevant_sm = [
        sm for sm, c in SALESMAN_MAP.items() if cid in c["principals"]
    ]
    principal_uni = frozenset().union(*[
        universes_by_p.get(sm, {}).get(cid, frozenset()) for sm in relevant_sm
    ])

    # Outlet history for this principal — 13 months back so we have
    # 12 prior months + current for L12M / same-month-LY / best month.
    hist = _load_outlet_history(cid,
                                _month_bounds(op_month_date)[1],
                                months_back=13)

    # Smart-target tuning config (with persistence)
    tgt_cfg = load_target_config()

    # ── Section 1: Target banner / set-target form ──
    target_entry = load_principal_target(principal, op_month)

    if target_entry is None:
        _no_target_form(principal, color, op_month, cfg["subteams"])
    else:
        total_target = target_entry["total_cases"]
        breakdown    = target_entry.get("breakdown", {})

        # Achieved this month so far for the principal
        achieved_total = float(
            hist[hist["BillMonth"] == op_month]["Cases"].sum()
        )

        # Achieved per sub-team. Two modes:
        #   - team has salesman list: filter by union of their principal universes
        #   - team has empty list and is named "Cross Supply": filter by
        #     AcType3ID = '130007' on the outlet (special UBL cross-supply rule)
        # Outlets with AcType3='130007' that are ALSO in Aabid/Omkar's universe
        # are counted in BOTH KW Beer and Cross Supply — these are two views
        # of the same outlets (the sub-targets sum to the principal total but
        # achievement may show small overlap).
        achieved_by_team: dict[str, float] = {}
        cm_hist = hist[hist["BillMonth"] == op_month]
        for team_name, sms in cfg["subteams"].items():
            if sms:
                team_uni = frozenset().union(*[
                    universes_by_p.get(sm, {}).get(cid, frozenset()) for sm in sms
                ])
                ach = float(cm_hist[cm_hist["PartyID"].isin(team_uni)]["Cases"].sum())
            elif team_name == "Cross Supply":
                ach = float(cm_hist[cm_hist["AcType3ID"] == "130007"]["Cases"].sum())
            else:
                ach = 0.0
            achieved_by_team[team_name] = ach

        pace = _pace_metrics(achieved_total, total_target, op_month_date)
        st.markdown(
            _hero_card(principal, color, _fmt_month(op_month),
                       total_target, achieved_total, pace),
            unsafe_allow_html=True,
        )
        _subtarget_cards(target_entry, achieved_by_team)

        if st.button("✏️ Edit target", key=f"edit_tgt_{op_month}"):
            # Clear by writing empty entry so the form reappears
            data = {}
            from src.targets import _read_json, _write_json, PRINCIPAL_TARGETS_FILE
            data = _read_json(PRINCIPAL_TARGETS_FILE)
            data.pop(f"{principal}__{op_month}", None)
            _write_json(PRINCIPAL_TARGETS_FILE, data)
            st.rerun()

    st.divider()

    # ── Section 2: KPI row ──
    safe_section("KPIs", _section_kpis, principal_uni, hist, op_month, prev_month)
    st.divider()

    # ── Allocation method (for salesman scoreboard) ──
    with st.expander("⚙️ Target allocation method"):
        allocation_method = st.radio(
            "How to split sub-targets across salesmen",
            ["Auto by L3M share (recommended)", "Equal split", "Manual"],
            index=0, key="allocation_method",
        )

    # ── Section 3: Outlet plan ──
    try:
        plan_df = _compute_outlet_plan(hist, op_month, principal_uni, tgt_cfg)
    except Exception as e:
        st.error(f"⚠️ Outlet plan failed to compute: {type(e).__name__}: {e}")
        plan_df = pd.DataFrame()

    col_rc, _ = st.columns([1, 4])
    with col_rc:
        if st.button("♻️ Recompute targets", key=f"recompute_{op_month}",
                     help="Refresh data and recompute formulas. Manual overrides preserved."):
            _load_outlet_history.clear()
            _load_master.clear()
            st.rerun()

    safe_section("Outlet plan",
                 _section_outlet_plan, plan_df, cfg["subteams"], op_month)
    st.divider()

    # ── Section 4: Focus brand (lazy — only loads when opened) ──
    with st.expander("🎯 Focus Brand of the Fortnight", expanded=False):
        brands_df = _load_brands_for_principal(cid)
        safe_section("Focus brand",
                     _section_focus_brand, cid, op_month, prev_month, brands_df)
    st.divider()

    # ── Section 5: Salesman scoreboard (uses hist for L3M share) ──
    safe_section("Salesman scoreboard",
                 _section_salesman_scoreboard,
                 principal, universes, universes_by_p, master, hist,
                 op_month, target_entry, allocation_method)
    st.divider()

    # ── Section 6: Target config (advanced) ──
    with st.expander("⚙️ Target Configuration (advanced)"):
        st.caption("Tune the smart-target formula. Changes apply on next page load.")
        with st.form(f"tgt_cfg_{op_month}"):
            cc1, cc2 = st.columns(2)
            with cc1:
                min_l3m = st.number_input(
                    "Min L3M to set target (cases/mo)",
                    min_value=0.0, max_value=50.0,
                    value=float(tgt_cfg["min_l3m_threshold"]), step=1.0,
                )
                std_g = st.number_input(
                    "Standard growth factor",
                    min_value=1.0, max_value=2.0,
                    value=float(tgt_cfg["standard_growth"]), step=0.05,
                )
                bonus = st.number_input(
                    "Growth bonus (trending up)",
                    min_value=1.0, max_value=2.0,
                    value=float(tgt_cfg["growth_bonus"]), step=0.05,
                )
            with cc2:
                decline = st.number_input(
                    "Decline factor (trending down)",
                    min_value=0.5, max_value=1.5,
                    value=float(tgt_cfg["decline_factor"]), step=0.05,
                )
                ceil = st.number_input(
                    "Best-month ceiling multiplier",
                    min_value=1.0, max_value=3.0,
                    value=float(tgt_cfg["ceiling_multiplier"]), step=0.1,
                )
                mb_floor = st.number_input(
                    "Months billed for L3M floor",
                    min_value=1, max_value=12,
                    value=int(tgt_cfg["months_for_floor"]), step=1,
                )
            save_cfg = st.form_submit_button("💾 Save config", type="primary")
            if save_cfg:
                save_target_config({
                    "min_l3m_threshold":  min_l3m,
                    "standard_growth":    std_g,
                    "growth_bonus":       bonus,
                    "decline_factor":     decline,
                    "ceiling_multiplier": ceil,
                    "months_for_floor":   mb_floor,
                })
                st.success("Saved. Click ♻️ Recompute to apply.")
