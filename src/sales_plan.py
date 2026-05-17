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
from utils.helpers import format_inr
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

@st.cache_data(ttl=300, show_spinner=False)
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
            SUM(CAST(vi.TotalBottleQty AS decimal(18,4))
                / NULLIF(im.BottlesPerCase, 0))        AS Cases,
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
    if not df.empty:
        df["Cases"]    = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
        df["Revenue"]  = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
        df["LastBill"] = pd.to_datetime(df["LastBill"], errors="coerce")
        df["Channel"]  = df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return df


@st.cache_data(ttl=300, show_spinner=False)
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
                THEN CAST(vi.TotalBottleQty AS decimal(18,4))
                     / NULLIF(im.BottlesPerCase, 0)
                ELSE 0 END)                                     AS BrandCases,
            SUM(CAST(vi.TotalBottleQty AS decimal(18,4))
                / NULLIF(im.BottlesPerCase, 0))                 AS PrincipalCases,
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
    if not df.empty:
        for c in ("BrandCases", "PrincipalCases"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        df["BrandLastBill"] = pd.to_datetime(df["BrandLastBill"], errors="coerce")
        df["Channel"]       = df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return df


@st.cache_data(ttl=600, show_spinner=False)
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


def _smart_target(avg_6mo: float, avg_3mo: float,
                  override: float | None) -> float:
    """Smart target = avg±10% with growth/decline detection.  Override wins."""
    if override is not None:
        return float(override)
    if avg_6mo == 0:
        return 5.0
    if avg_3mo > avg_6mo * 1.05:
        return round(avg_6mo * 1.15, 0)
    if avg_3mo < avg_6mo * 0.85:
        return round(avg_6mo,         0)
    return round(avg_6mo * 1.10, 0)


def _action_for(this_mo: float, target: float, prev_mo: float) -> tuple[str, str]:
    """Return (action_label, hex_bg_color)."""
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
                        universe: frozenset) -> pd.DataFrame:
    """Build the outlet-by-outlet plan dataframe."""
    if hist_df.empty:
        return pd.DataFrame()

    last_6 = _last_n_months(7, op_month)[:-1]  # 6 prior months
    last_3 = last_6[-3:]
    prev_month = last_6[-1]

    # Per-party aggregation
    h = hist_df[hist_df["BillMonth"].isin(last_6 + [op_month])].copy()
    party_info = (
        h.groupby(["PartyID", "PartyName", "Channel"], as_index=False)
        .agg(LastBill=("LastBill", "max"))
    )

    # 6-month avg
    six = (
        h[h["BillMonth"].isin(last_6)]
        .groupby("PartyID")["Cases"].sum() / 6.0
    ).rename("Avg6").reset_index()

    # 3-month avg
    three = (
        h[h["BillMonth"].isin(last_3)]
        .groupby("PartyID")["Cases"].sum() / 3.0
    ).rename("Avg3").reset_index()

    # Previous month
    prev = (
        h[h["BillMonth"] == prev_month]
        .groupby("PartyID")["Cases"].sum()
        .rename("PrevMo").reset_index()
    )

    # Current operating month
    curr = (
        h[h["BillMonth"] == op_month]
        .groupby("PartyID")["Cases"].sum()
        .rename("ThisMo").reset_index()
    )

    df = (
        party_info
        .merge(six,   on="PartyID", how="left")
        .merge(three, on="PartyID", how="left")
        .merge(prev,  on="PartyID", how="left")
        .merge(curr,  on="PartyID", how="left")
        .fillna({"Avg6": 0.0, "Avg3": 0.0, "PrevMo": 0.0, "ThisMo": 0.0})
    )

    # Bring in universe outlets that don't appear in history at all
    seen = set(df["PartyID"])
    extras = [pid for pid in universe if pid not in seen]
    if extras:
        # Look them up from MsPartyMaster
        in_ph = ",".join("?" * len(extras))
        try:
            extra_rows = run_query(
                f"""
                SELECT p.PartyID,
                       ISNULL(p.PartyName, '(unknown)')   AS PartyName,
                       ISNULL(p.LicenseTypeID, '')        AS LicenseTypeID
                FROM MsPartyMaster p
                WHERE p.PartyID IN ({in_ph})
                """,
                tuple(extras),
            )
            if not extra_rows.empty:
                extra_rows["Channel"] = extra_rows["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
                for col in ("Avg6", "Avg3", "PrevMo", "ThisMo"):
                    extra_rows[col] = 0.0
                extra_rows["LastBill"] = pd.NaT
                df = pd.concat(
                    [df, extra_rows[["PartyID","PartyName","Channel","LastBill",
                                     "Avg6","Avg3","PrevMo","ThisMo"]]],
                    ignore_index=True,
                )
        except Exception:
            pass

    # Restrict to universe outlets
    df = df[df["PartyID"].isin(universe)].copy()

    # Smart target with override
    def _target_for(pid, a6, a3):
        ov = load_party_target_override(pid, op_month)
        return _smart_target(float(a6), float(a3), ov)

    df["Target"] = df.apply(
        lambda r: _target_for(r["PartyID"], r["Avg6"], r["Avg3"]),
        axis=1,
    )

    # Action + gap
    actions: list[tuple[str, str]] = df.apply(
        lambda r: _action_for(r["ThisMo"], r["Target"], r["PrevMo"]),
        axis=1,
    ).tolist()
    df["Action"]   = [a for a, _ in actions]
    df["RowBg"]    = [c for _, c in actions]
    df["Gap"]      = df["ThisMo"] - df["Target"]

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
        "Smart target = 6-month avg + 10% (or +15% if growing, "
        "= 6mo avg if declining). Click any row to expand and override."
    )

    if plan_df.empty:
        st.info("No outlets to plan for this principal."); return

    # Team filter
    sm_to_team: dict[str, str] = {}
    for team, sms in all_subteams.items():
        for sm in sms:
            sm_to_team[sm] = team
    team_options = ["All"] + list(all_subteams.keys())
    team = st.selectbox("Team", team_options, key=f"plan_team_{op_month}")

    df = plan_df.copy()
    # Order: PUSH first then GROW then HOLD, within each by Avg6 desc
    rank = df["Action"].map({"🔴 PUSH": 0, "🟡 GROW": 1, "✅ HOLD": 2}).fillna(3)
    df["_rank"] = rank
    df = df.sort_values(["_rank", "Avg6"], ascending=[True, False])

    # Build display table
    df["LastBillStr"] = pd.to_datetime(df["LastBill"], errors="coerce") \
        .dt.strftime("%d %b %Y").fillna("Never")
    display = df[[
        "Action", "PartyName", "Channel", "Avg6", "Avg3",
        "Target", "ThisMo", "Gap", "LastBillStr", "PartyID",
    ]].rename(columns={
        "PartyName": "Party", "Avg6": "Avg cs/mo", "Avg3": "Last 3mo avg",
        "Target": "Target", "ThisMo": "So Far", "Gap": "Gap",
        "LastBillStr": "Last Bill",
    })

    def _row_style(row):
        bg = df.iloc[row.name]["RowBg"]
        return [f"background-color:{bg}"] * len(row)

    styled = (
        display.drop(columns=["PartyID"])
        .style
        .apply(_row_style, axis=1)
        .format({
            "Avg cs/mo":      "{:,.1f}",
            "Last 3mo avg":   "{:,.1f}",
            "Target":         "{:,.0f}",
            "So Far":         "{:,.1f}",
            "Gap":            "{:+,.1f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=400)

    # ── Manual override expander ──
    with st.expander("✏️ Set manual target override for an outlet"):
        with st.form(f"override_{op_month}"):
            o1, o2, o3 = st.columns([3, 1, 1])
            with o1:
                pick = st.selectbox(
                    "Outlet",
                    options=df["PartyID"].tolist(),
                    format_func=lambda pid: f"{df[df['PartyID']==pid]['PartyName'].iloc[0]}",
                    key=f"ov_pick_{op_month}",
                )
            with o2:
                ov_cases = st.number_input(
                    "Target cases", min_value=0.0, step=1.0,
                    value=float(df[df["PartyID"]==pick]["Target"].iloc[0]) if pick else 0.0,
                    key=f"ov_cases_{op_month}",
                )
            with o3:
                ov_submit = st.form_submit_button("Save override")
        if ov_submit and pick:
            save_party_target_override(pick, op_month, float(ov_cases))
            st.success(f"Saved override: {ov_cases:.1f} cases for {pick}")
            st.rerun()

    # ── Excel export ──
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for team_name, sms in all_subteams.items():
            # NOTE: we don't have a salesman column per outlet without joining
            # MsPartyMaster SM fields. Export the whole plan as one sheet.
            df.drop(columns=["_rank", "RowBg"], errors="ignore").to_excel(
                writer, sheet_name=team_name[:31], index=False,
            )
            break  # one sheet for now; team-specific filtering needs SM fields
    buf.seek(0)
    st.download_button(
        "📥 Download Plan (Excel)", buf,
        file_name=f"sales_plan_{op_month}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _section_focus_brand(company_id: str,
                         op_month: str, prev_month: str,
                         brands_df: pd.DataFrame) -> None:
    st.subheader("Focus brand of the fortnight")
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


def _section_salesman_scoreboard(principal: str,
                                 universes: dict[str, frozenset],
                                 universes_by_p: dict[str, dict[str, frozenset]],
                                 master: pd.DataFrame,
                                 op_month: str,
                                 target_entry: dict | None) -> None:
    st.subheader("Salesman scoreboard — this month")

    cid = PRINCIPAL_TEAMS[principal]["company_id"]
    relevant_sm = [
        sm for sm, cfg in SALESMAN_MAP.items() if cid in cfg["principals"]
    ]
    if not relevant_sm:
        st.info("No salesmen handle this principal."); return

    # Total principal universe = union of principal universes per salesman
    total_pu = frozenset().union(*[
        universes_by_p.get(sm, {}).get(cid, frozenset()) for sm in relevant_sm
    ])
    total_target = (target_entry or {}).get("total_cases", 0)

    rows = []
    for sm in relevant_sm:
        pu = universes_by_p.get(sm, {}).get(cid, frozenset())
        uni_size = len(pu)
        # Outlets billed this month for this salesman+principal
        sub = master[
            (master["CompanyID"] == cid) &
            (master["PartyID"].isin(pu)) &
            (master["BillMonth"] == op_month)
        ]
        billed   = sub["PartyID"].nunique()
        cases    = float(sub["Cases"].sum())
        wod      = (billed / uni_size * 100) if uni_size else 0.0
        # Target allocated proportionally by universe share
        share    = (uni_size / len(total_pu)) if total_pu else 0
        sm_target = int(round(total_target * share))
        pct_done = (cases / sm_target * 100) if sm_target else 0.0

        if pct_done >= 80:    status = "✅ On Track"
        elif pct_done >= 50:  status = "⚠️ Behind"
        else:                 status = "🔴 Critical"

        rows.append({
            "Salesman":   sm,
            "Universe":   uni_size,
            "Billed":     billed,
            "WOD %":      round(wod, 1),
            "Cases":      round(cases, 1),
            "Target":     sm_target,
            "% Done":     round(pct_done, 1),
            "Status":     status,
        })

    df = pd.DataFrame(rows).sort_values("% Done", ascending=True)

    def _wod_style(v):
        try:
            n = float(v)
        except (TypeError, ValueError):
            return ""
        if n >= 70:  return "color:#16a34a; font-weight:600"
        if n >= 50:  return "color:#854d0e; font-weight:600"
        return "color:#dc2626; font-weight:600"

    styled = (
        df.style
        .format({
            "Universe": "{:,}", "Billed": "{:,}",
            "WOD %": "{:.1f}", "Cases": "{:,.1f}",
            "Target": "{:,}", "% Done": "{:.1f}",
        })
        .map(_wod_style, subset=["WOD %"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


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

    # Outlet history for this principal
    hist = _load_outlet_history(cid,
                                _month_bounds(op_month_date)[1],
                                months_back=7)

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
    _section_kpis(principal_uni, hist, op_month, prev_month)
    st.divider()

    # ── Section 3: Outlet plan ──
    plan_df = _compute_outlet_plan(hist, op_month, principal_uni)
    _section_outlet_plan(plan_df, cfg["subteams"], op_month)
    st.divider()

    # ── Section 4: Focus brand ──
    brands_df = _load_brands_for_principal(cid)
    _section_focus_brand(cid, op_month, prev_month, brands_df)
    st.divider()

    # ── Section 5: Salesman scoreboard ──
    _section_salesman_scoreboard(
        principal, universes, universes_by_p, master, op_month, target_entry,
    )
