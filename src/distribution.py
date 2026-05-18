"""pages/distribution.py — Comprehensive Field Sales Analytics Dashboard

Tracks Width of Distribution (WOD), Depth of Distribution (DOD),
outlet lapse/recovery, and individual salesman scorecards.

Universe per salesman = outlets directly assigned in MsPartyMaster via
SalesManID / SalesManID1 / SalesManID2 fields, filtered by license type
and AcType3 as per territory rules.  This is stable master data — not
billing history — so outlets that happen to go silent in a month still
remain in the universe denominator.

Billing data is a separate query (last 13 months) used only for WOD/DOD
numerator calculations.

NOTE:
  - Party IDs are D-prefixed (D00001 etc.) — no LIKE filter.
  - FinancialYear computed from VoucherDate to avoid TrVocItem duplication
    at the fiscal-year boundary.
  - Miran's SalesManID = '000039' (person); C00039 = United Breweries
    (company). Different tables, different ID formats — no collision.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, section_header, CASES_SQL_EXPR as _CASES

# ── Sales types ─────────────────────────────────────────────────────────────
SALES_TYPES = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Light chart theme ────────────────────────────────────────────────────────
_BG     = "rgba(0,0,0,0)"
_GRID   = dict(gridcolor="#E8E8E8")
_LAYOUT = dict(
    paper_bgcolor=_BG, plot_bgcolor=_BG,
    font=dict(color="#1A1A1A"),
    template="plotly_white",
    margin=dict(t=40, b=20),
)
_PAL = px.colors.qualitative.Bold

# ── Channel labels ───────────────────────────────────────────────────────────
_LT_LABEL: dict[str, str] = {
    "180001": "FL-II Wine Shop",
    "180002": "FL-III Permit Room",
    "180004": "FL-BR-II Beer Shopee",
    "180005": "FL-IV Club",
    "180007": "FL-IV One Day",
}

# ── Principal → MsPartyMaster SM field (team-based, matches ERP logic) ──────
# Empirically, the SM field is team-determined, not principal-determined.
# Each salesman's sm_field below is the column they appear in for ALL their
# principals.  Wine-shop sellers (Ajay/DP) use SM2 for both Diageo and BF;
# POP sellers (Atish/Tulsiram/etc.) use SM1 for both USL and Diageo; UBL
# beer + Institution salesmen use SM3 for all their principals.
#
# SM1 = SalesManID (primary)
# SM2 = SalesManID1 (secondary)
# SM3 = SalesManID2 (tertiary)

# ═══════════════════════════════════════════════════════════════════════════════
# SALESMAN MAP
# ═══════════════════════════════════════════════════════════════════════════════
SALESMAN_MAP: dict[str, dict] = {
    # ── USL Wine Shops (SM0) ─────────────────────────────────────────────
    # NOTE: 130007 (cross-supply wine shops) IS valid for USL/Diageo teams.
    # Suresh Nair (000040) handles cross-supply UBL beer — he is in SM2 for
    # these same outlets.  Shashank/Sachin still service them for USL wine.
    "Shashank":       {"team": "USL Wine Shops",       "sm_id": "000014",
                       "sm_field": "SM1", "principals": ["C00025"]},
    "Sachin":         {"team": "USL Wine Shops",       "sm_id": "000012",
                       "sm_field": "SM1", "principals": ["C00025"]},
    # ── USL+Diageo Permit Rooms (SM1 for BOTH principals) ─────────────────
    "Tulsiram":       {"team": "USL+Diageo POP",       "sm_id": "000024",
                       "sm_field": "SM1", "principals": ["C00025","C00040"]},
    "Saurabh":        {"team": "USL+Diageo POP",       "sm_id": "000018",
                       "sm_field": "SM1", "principals": ["C00025","C00040"]},
    "Miran":          {"team": "USL+Diageo POP",       "sm_id": "000039",
                       "sm_field": "SM1", "principals": ["C00025","C00040"]},
    "Prashant":       {"team": "USL+Diageo POP",       "sm_id": "000025",
                       "sm_field": "SM1", "principals": ["C00025","C00040"]},
    "Atish":          {"team": "USL+Diageo POP",       "sm_id": "000033",
                       "sm_field": "SM1", "principals": ["C00025","C00040"]},
    # ── Diageo+BF Wine Shops (SM2 for BOTH principals) ────────────────────
    "Ajay":           {"team": "Diageo+BF Wine Shops", "sm_id": "000030",
                       "sm_field": "SM2", "principals": ["C00040","C00056"]},
    "Deepak Patil":   {"team": "Diageo+BF Wine Shops", "sm_id": "000004",
                       "sm_field": "SM2", "principals": ["C00040","C00056"]},
    # ── UBL KW Beer (SM3) ────────────────────────────────────────────────
    "Aabid":          {"team": "UBL KW Beer",          "sm_id": "000028",
                       "sm_field": "SM3", "principals": ["C00039"]},
    "Omkar":          {"team": "UBL KW Beer",          "sm_id": "000032",
                       "sm_field": "SM3", "principals": ["C00039"]},
    # ── KW Institution (SM3 for BOTH UBL + BF) ───────────────────────────
    "Anand Raj":      {"team": "KW Institution",       "sm_id": "000037",
                       "sm_field": "SM3", "principals": ["C00039","C00056"]},
    "Deepak Pangare": {"team": "KW Institution",       "sm_id": "000038",
                       "sm_field": "SM3", "principals": ["C00039","C00056"]},
    "Shashank Desai": {"team": "KW Institution",       "sm_id": "000036",
                       "sm_field": "SM3", "principals": ["C00039","C00056"]},
    "Pranav":         {"team": "KW Institution",       "sm_id": "000026",
                       "sm_field": "SM3", "principals": ["C00039","C00056"]},
    # ── PCMC Institution (SM3) ───────────────────────────────────────────
    "Gajendra Das":   {"team": "PCMC Institution",     "sm_id": "000042",
                       "sm_field": "SM3", "principals": ["C00039"]},
    "Amol Sathe":     {"team": "PCMC Institution",     "sm_id": "000041",
                       "sm_field": "SM3", "principals": ["C00039"]},
    "Rahul Ghone":    {"team": "PCMC Institution",     "sm_id": "000043",
                       "sm_field": "SM3", "principals": ["C00039"]},
}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_month(ym: str) -> str:
    """'2026-04' → 'Apr-26'"""
    yr, mo = int(ym[:4]), int(ym[5:])
    return date(yr, mo, 1).strftime("%b-%y")


def _prev_month_str(ym: str) -> str:
    yr, mo = int(ym[:4]), int(ym[5:])
    return f"{yr - 1:04d}-12" if mo == 1 else f"{yr:04d}-{mo - 1:02d}"


def _last_n_month_strs(n: int = 12) -> list[str]:
    """Last n months as 'yyyy-MM' strings, oldest → newest."""
    today = date.today()
    out   = []
    for i in range(n - 1, -1, -1):
        d = (today.replace(day=1) - timedelta(days=1)) if i == 0 else today
        # safer: subtract months properly
        mo = today.month - i
        yr = today.year
        while mo <= 0:
            mo += 12
            yr -= 1
        out.append(f"{yr:04d}-{mo:02d}")
    return out


def _last_complete_month() -> str:
    """Return last fully-elapsed month as 'yyyy-MM'."""
    first_this_month = date.today().replace(day=1)
    last_prev        = first_this_month - timedelta(days=1)
    return f"{last_prev.year:04d}-{last_prev.month:02d}"


def _filter_sm(df: pd.DataFrame, sm_key: str, uni_ids: frozenset) -> pd.DataFrame:
    """Filter master billing DataFrame to a salesman's billing-active universe.

    Rows are kept where the brand belongs to one of the salesman's principals
    AND the party is in the salesman's active universe (12-month billing
    intersection with the correct MsPartyMaster SM-field assignment).
    """
    cfg = SALESMAN_MAP[sm_key]
    return df[
        df["CompanyID"].isin(cfg["principals"])
        & df["PartyID"].isin(uni_ids)
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADER  (single master query, cached 1 hour)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_master(months_back: int = 13) -> pd.DataFrame:
    """
    Single master query covering last 13 months.
    One row per (BillMonth, PartyID, CompanyID).
    Used for all analytics on this page.
    """
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    df = run_query(f"""
        SELECT
            FORMAT(h.VoucherDate,'yyyy-MM')        AS BillMonth,
            MAX(h.VoucherDate)                     AS LastBillDate,
            d.PartyID,
            p.PartyName,
            ISNULL(p.LicenseTypeID,'')             AS LicenseTypeID,
            ISNULL(p.AcType3ID,    '')             AS AcType3ID,
            b.CompanyID,
            COUNT(DISTINCT h.VoucherNo)            AS InvoiceCount,
            SUM({_CASES}) AS Cases,
            SUM(CAST(vi.TotalAmount AS FLOAT))     AS Revenue
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
            END
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID
            FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (
                           PARTITION BY TransTypeID, VoucherNo
                           ORDER BY Amount DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator = 'D'
                      AND PartyID LIKE 'D%'   -- customer outlets only
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsPartyMaster p  ON p.PartyID  = d.PartyID
        JOIN MsItemMaster  im ON im.ItemID  = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND d.PartyID     IS NOT NULL
          AND p.LicenseTypeID IN ('180001','180002','180004','180005','180007')
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY
            FORMAT(h.VoucherDate,'yyyy-MM'),
            d.PartyID, p.PartyName, p.LicenseTypeID, p.AcType3ID, b.CompanyID
    """)
    if df.empty:
        return df
    df["InvoiceCount"] = pd.to_numeric(df["InvoiceCount"], errors="coerce").fillna(0).astype(int)
    df["Cases"]        = pd.to_numeric(df["Cases"],        errors="coerce").fillna(0.0)
    df["Revenue"]       = pd.to_numeric(df["Revenue"],       errors="coerce").fillna(0.0)
    df["LicenseTypeID"] = df["LicenseTypeID"].fillna("").astype(str)
    df["AcType3ID"]     = df["AcType3ID"].fillna("").astype(str)
    df["CompanyID"]     = df["CompanyID"].fillna("").astype(str)
    df["LastBillDate"]  = pd.to_datetime(df["LastBillDate"],  errors="coerce")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSE LOADER  (Billing-active, cached 1 hour)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=86400, show_spinner=False)   # billing-active universe — 24h
def _load_active_universe() -> pd.DataFrame:
    """
    Active universe = parties that have billed any tracked principal in the
    last 12 months, joined with their MsPartyMaster SM1/SM2/SM3 IDs.

    The per-salesman universe is built in Python by intersecting:
       (CompanyID == this salesman's principal)
       ∩ (the SM field for that principal == this salesman's sm_id)

    This matches the ERP report logic exactly (billing-based, not
    pure master-based), giving the same denominator the principal
    review decks use.
    """
    df = run_query("""
        SELECT DISTINCT
            d.PartyID,
            p.PartyName,
            ISNULL(p.LicenseTypeID,'')          AS LicenseTypeID,
            ISNULL(p.AcType3ID,    '')          AS AcType3ID,
            b.CompanyID,
            RTRIM(ISNULL(p.SalesManID,  ''))    AS SM1,
            RTRIM(ISNULL(p.SalesManID1, ''))    AS SM2,
            RTRIM(ISNULL(p.SalesManID2, ''))    AS SM3
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.FreeItemYN  = 'N'
            AND vi.ItemID      LIKE 'I%'
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID
            FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (
                           PARTITION BY TransTypeID, VoucherNo
                           ORDER BY Amount DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL
                  AND DrCrIndicator = 'D'
                  AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsPartyMaster p  ON p.PartyID = d.PartyID
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN (18,19,23,35,37,38,39,40,41,44,47,49,51,53)
          AND h.Cancelled   = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -12, GETDATE())
          AND p.BannedPartyYN = 'N'
          AND b.CompanyID IN ('C00025','C00040','C00056','C00039')
    """)
    if df.empty:
        return df
    for col in ("LicenseTypeID", "AcType3ID", "SM1", "SM2", "SM3", "CompanyID"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    return df


def _build_universes(uni_df: pd.DataFrame) -> tuple[dict, dict]:
    """Build per-salesman active billing universes.

    For each (salesman, principal) pair, take outlets where:
       (CompanyID == principal) AND (the SM field for that principal == sm_id)
    Then union across the salesman's principals to get the total universe.

    Returns:
       main_uni:        {sm_key: frozenset(party_ids)}  — union across principals
       by_principal:    {sm_key: {principal_id: frozenset(party_ids)}}
    """
    main_uni: dict[str, frozenset] = {}
    by_principal: dict[str, dict[str, frozenset]] = {}

    for sm_key, cfg in SALESMAN_MAP.items():
        sm_id        = cfg["sm_id"]
        sm_field     = cfg["sm_field"]
        principals   = cfg["principals"]
        all_parties: set = set()
        per_p: dict[str, frozenset] = {}

        for principal in principals:
            mask = (
                (uni_df["CompanyID"] == principal) &
                (uni_df[sm_field]    == sm_id)
            )
            parties = frozenset(uni_df.loc[mask, "PartyID"])
            per_p[principal] = parties
            all_parties |= parties

        main_uni[sm_key]     = frozenset(all_parties)
        by_principal[sm_key] = per_p

    return main_uni, by_principal


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════

def _party_month_agg(sm_df: pd.DataFrame, month_str: str) -> pd.DataFrame:
    """Aggregate to (PartyID, PartyName, LicenseTypeID) level for one month."""
    month_df = sm_df[sm_df["BillMonth"] == month_str]
    if month_df.empty:
        return pd.DataFrame(
            columns=["PartyID", "PartyName", "LicenseTypeID", "InvoiceCount", "Cases", "Revenue"]
        )
    return (
        month_df
        .groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
        .agg(
            InvoiceCount=("InvoiceCount", "sum"),
            Cases=("Cases", "sum"),
            Revenue=("Revenue", "sum"),
        )
    )


def _monthly_metrics(sm_df: pd.DataFrame, months: list[str], uni_ids: frozenset) -> pd.DataFrame:
    """Per-month WOD, strike rate, DOD for one salesman.

    billed_ids is intersected with uni_ids so that out-of-territory
    billing rows (same principal, different salesman's outlet) are never
    counted toward this salesman's WOD numerator.
    """
    rows = []
    uni_size = len(uni_ids)
    for mo in months:
        pa = _party_month_agg(sm_df, mo)
        # Restrict to universe parties only ← critical intersection
        billed_ids   = frozenset(pa["PartyID"]) & uni_ids
        billed_cnt   = len(billed_ids)
        wod_pct      = billed_cnt / uni_size * 100 if uni_size else 0.0
        # Strike rate: only over billed universe parties
        billed_pa    = pa[pa["PartyID"].isin(billed_ids)]
        multi_bill   = int((billed_pa["InvoiceCount"] >= 2).sum())
        strike_rate  = multi_bill / billed_cnt * 100 if billed_cnt else 0.0
        revenue      = float(billed_pa["Revenue"].sum())
        cases        = float(billed_pa["Cases"].sum())
        invoices     = int(billed_pa["InvoiceCount"].sum())
        billed_safe  = max(billed_cnt, 1)
        rows.append({
            "BillMonth":   mo,
            "month_label": _fmt_month(mo),
            "universe":    uni_size,
            "billed":      billed_cnt,
            "unbilled":    uni_size - billed_cnt,
            "wod_pct":     round(wod_pct, 1),
            "strike_rate": round(strike_rate, 1),
            "revenue":     revenue,
            "cases":       cases,
            "invoices":    invoices,
            "avg_rev":     revenue / billed_safe,
            "avg_cas":     cases   / billed_safe,
            "avg_inv":     invoices / billed_safe,
        })
    return pd.DataFrame(rows)


def _lapsed_new_ids(
    sm_df: pd.DataFrame, sel_mo: str, prev_mo: str, uni_ids: frozenset
) -> dict[str, frozenset]:
    """Lapsed/new/retained outlet sets — restricted to universe parties."""
    sel_ids  = frozenset(sm_df[sm_df["BillMonth"] == sel_mo]["PartyID"])  & uni_ids
    prev_ids = frozenset(sm_df[sm_df["BillMonth"] == prev_mo]["PartyID"]) & uni_ids
    return {
        "lapsed":   prev_ids - sel_ids,
        "new":      sel_ids  - prev_ids,
        "retained": sel_ids  & prev_ids,
    }


def _concentration(sm_df: pd.DataFrame, month_str: str) -> dict:
    pa = sm_df[sm_df["BillMonth"] == month_str].groupby("PartyID")["Revenue"].sum()
    pa = pa.sort_values(ascending=False)
    total = pa.sum()
    if total == 0 or pa.empty:
        return {"top10_pct": 0.0, "next40_pct": 0.0, "bot50_pct": 100.0, "concentrated": False}
    n      = len(pa)
    top10n = max(1, round(n * 0.10))
    nxt40n = max(1, round(n * 0.40))
    top10_rev  = pa.iloc[:top10n].sum()
    next40_rev = pa.iloc[top10n : top10n + nxt40n].sum()
    bot50_rev  = pa.iloc[top10n + nxt40n :].sum()
    return {
        "top10_pct":  top10_rev  / total * 100,
        "next40_pct": next40_rev / total * 100,
        "bot50_pct":  bot50_rev  / total * 100,
        "concentrated": (top10_rev / total) > 0.60,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def _chart_wod_trend(
    all_metrics: dict[str, pd.DataFrame],
    months: list[str],
    sel_sm: list[str],
    target_pct: float,
) -> go.Figure:
    ordered = [_fmt_month(m) for m in months]
    fig = go.Figure()
    for i, sm in enumerate(sel_sm):
        df = all_metrics[sm].sort_values("BillMonth")
        fig.add_trace(go.Scatter(
            x=df["month_label"], y=df["wod_pct"],
            mode="lines+markers", name=sm,
            line=dict(color=_PAL[i % len(_PAL)], width=2),
            marker=dict(size=5),
            hovertemplate=f"<b>{sm}</b><br>%{{x}}: %{{y:.1f}}%<extra></extra>",
        ))
    fig.add_hline(
        y=target_pct, line_dash="dash", line_color="#FF4444", line_width=1.5,
        annotation_text=f"Target {target_pct:.0f}%", annotation_font_color="#FF4444",
    )
    fig.update_layout(
        **_LAYOUT,
        title="Width of Distribution — 12-Month Trend",
        xaxis=dict(categoryorder="array", categoryarray=ordered, tickangle=-30, **_GRID),
        yaxis=dict(range=[0, 105], ticksuffix="%", **_GRID),
        legend=dict(font=dict(size=10), title=""),
    )
    return fig


def _chart_outlet_movement(
    movement: dict[str, dict[str, int]],
    sel_sm: list[str],
) -> go.Figure:
    labels = sel_sm
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Retained", x=labels,
                         y=[movement[s]["retained"] for s in labels],
                         marker_color="#3498db"))
    fig.add_trace(go.Bar(name="🟢 New Active", x=labels,
                         y=[movement[s]["new"] for s in labels],
                         marker_color="#2ecc71"))
    fig.add_trace(go.Bar(name="🔴 Lapsed", x=labels,
                         y=[movement[s]["lapsed"] for s in labels],
                         marker_color="#e74c3c"))
    fig.update_layout(
        **_LAYOUT,
        title="Outlet Movement vs Previous Month",
        barmode="group",
        xaxis=dict(**_GRID),
        yaxis=dict(title="Outlets", **_GRID),
        legend=dict(font=dict(size=10)),
    )
    return fig


def _chart_dod_trend(
    all_metrics: dict[str, pd.DataFrame],
    months: list[str],
    metric: str,
    title: str,
    sel_sm: list[str],
    y_fmt: str = "",
) -> go.Figure:
    ordered = [_fmt_month(m) for m in months]
    fig = go.Figure()
    for i, sm in enumerate(sel_sm):
        df = all_metrics[sm].sort_values("BillMonth")
        fig.add_trace(go.Scatter(
            x=df["month_label"], y=df[metric],
            mode="lines+markers", name=sm,
            line=dict(color=_PAL[i % len(_PAL)], width=2),
            marker=dict(size=4),
        ))
    fig.update_layout(
        **{k: v for k, v in _LAYOUT.items() if k != "margin"},
        title=title,
        xaxis=dict(categoryorder="array", categoryarray=ordered, tickangle=-30, **_GRID),
        yaxis=dict(ticksuffix=y_fmt, **_GRID),
        legend=dict(font=dict(size=9), title=""),
        margin=dict(t=50, b=30),
    )
    return fig


def _chart_concentration(
    concentration: dict[str, dict],
    sel_sm: list[str],
) -> go.Figure:
    top10  = [concentration[s]["top10_pct"]  for s in sel_sm]
    next40 = [concentration[s]["next40_pct"] for s in sel_sm]
    bot50  = [concentration[s]["bot50_pct"]  for s in sel_sm]
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Top 10% outlets",  x=sel_sm, y=top10,  marker_color="#e74c3c", orientation="v"))
    fig.add_trace(go.Bar(name="Next 40% outlets", x=sel_sm, y=next40, marker_color="#f39c12", orientation="v"))
    fig.add_trace(go.Bar(name="Bottom 50%",       x=sel_sm, y=bot50,  marker_color="#2ecc71", orientation="v"))
    fig.update_layout(
        **_LAYOUT,
        title="Revenue Concentration Risk",
        barmode="stack",
        xaxis=dict(**_GRID),
        yaxis=dict(ticksuffix="%", range=[0, 105], **_GRID),
        legend=dict(font=dict(size=10)),
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED STYLE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _style_wod_table(df: pd.DataFrame, target: float) -> "pd.io.formats.style.Styler":
    def _c_wod(v: str) -> str:
        try:
            p = float(str(v).replace("%", ""))
        except Exception:
            return ""
        if p >= target:
            return "background-color:#d4edda;color:#155724"
        if p >= target - 10:
            return "background-color:#fff3cd;color:#856404"
        return "background-color:#f8d7da;color:#721c24"

    def _c_sr(v: str) -> str:
        try:
            p = float(str(v).replace("%", ""))
        except Exception:
            return ""
        if p > 30:
            return "background-color:#d4edda;color:#155724"
        if p >= 15:
            return "background-color:#fff3cd;color:#856404"
        return "background-color:#f8d7da;color:#721c24"

    return (
        df.style
        .map(_c_wod, subset=["WOD %"])
        .map(_c_sr,  subset=["Strike Rate"])
    )


def _delta_arrow(v: float, fmt: str = ".1f") -> str:
    sym = "↑" if v >= 0 else "↓"
    col = "#2ecc71" if v >= 0 else "#e74c3c"
    return f'<span style="color:{col}">{sym} {abs(v):{fmt}}</span>'


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def render():  # noqa: C901 (complexity — intentional for a single-page analytics hub)
    st.title("Distribution Analytics")
    st.caption("Width & Depth of Distribution · Outlet Intelligence · Salesman Scorecard | Source: TEKNIK ERP")
    st.divider()

    # ── Inline filter row ────────────────────────────────────────────────────
    all_sm    = sorted(SALESMAN_MAP.keys())
    months_12 = _last_n_month_strs(12)
    months_13 = _last_n_month_strs(13)

    default_month = _last_complete_month()
    avail_months  = list(reversed(months_12))          # newest first for picker
    try:
        default_idx = avail_months.index(default_month)
    except ValueError:
        default_idx = 0

    with st.container():
        fc1, fc2, fc3 = st.columns([2.5, 2.5, 2.0])
        with fc1:
            sel_sm = st.multiselect(
                "Salesman", options=all_sm, default=all_sm, key="dist_sm"
            )
            if not sel_sm:
                sel_sm = all_sm
        with fc2:
            sel_month_str = st.selectbox(
                "Reference Month",
                options=avail_months,
                index=default_idx,
                format_func=_fmt_month,
                key="dist_month",
            )
        with fc3:
            target_wod = st.slider(
                "WOD Target %", min_value=50, max_value=95, value=70, step=5,
                key="dist_target",
            )

    prev_month_str = _prev_month_str(sel_month_str)

    # ── Load data ─────────────────────────────────────────────────────────────
    with st.spinner("Loading distribution analytics…"):
        master = _load_master(13)
        uni_df = _load_active_universe()

    if master.empty:
        st.error("No billing data returned. Check DB connection.")
        return
    if uni_df.empty:
        st.error("No universe data returned. Check DB connection.")
        return

    # ── Build per-salesman active universes (billing × master-SM intersection) ──
    universes, universes_by_p = _build_universes(uni_df)

    # ── Pre-compute per-salesman slices & metrics ────────────────────────────
    sm_data: dict[str, pd.DataFrame]         = {}
    uni_ids: dict[str, frozenset]            = {}
    all_metrics: dict[str, pd.DataFrame]     = {}
    movement:    dict[str, dict[str, int]]   = {}
    concentration: dict[str, dict]           = {}

    for sm in SALESMAN_MAP:           # compute for ALL salesmen (scorecard tab needs full set)
        uni_ids[sm]     = universes[sm]
        df_sm           = _filter_sm(master, sm, uni_ids[sm])
        sm_data[sm]     = df_sm
        all_metrics[sm] = _monthly_metrics(df_sm, months_12, uni_ids[sm])
        ids = _lapsed_new_ids(df_sm, sel_month_str, prev_month_str, uni_ids[sm])
        movement[sm] = {k: len(v) for k, v in ids.items()}
        movement[sm]["lapsed_ids"] = ids["lapsed"]
        movement[sm]["new_ids"]    = ids["new"]
        concentration[sm] = _concentration(df_sm, sel_month_str)

    # ── Print universe sizes for verification (terminal-only) ────────────────
    print("\n=== ACTIVE UNIVERSE VERIFICATION (12-month billing) ===")
    for sm in sorted(SALESMAN_MAP.keys()):
        u = len(uni_ids[sm])
        per_p = " ".join(
            f"{p}={len(universes_by_p[sm][p])}" for p in universes_by_p[sm]
        )
        sel_row = all_metrics[sm][all_metrics[sm]["BillMonth"] == sel_month_str]
        b  = int(sel_row["billed"].iloc[0])  if not sel_row.empty else 0
        wp = sel_row["wod_pct"].iloc[0]      if not sel_row.empty else 0.0
        print(f"  {sm:<18} uni={u:>4}  billed={b:>4}  WOD={wp:5.1f}%   ({per_p})")

    # ── Aggregate KPI (unique parties across selected salesmen) ───────────────
    all_uni_union     = set()
    all_billed_union  = set()
    all_lapsed_union  = set()
    for sm in sel_sm:
        all_uni_union    |= uni_ids[sm]
        mo_df = sm_data[sm][sm_data[sm]["BillMonth"] == sel_month_str]
        all_billed_union |= set(mo_df["PartyID"].unique())
        all_lapsed_union |= movement[sm]["lapsed_ids"]

    total_uni      = len(all_uni_union)
    total_billed   = len(all_uni_union & all_billed_union)
    total_lapsed   = len(all_lapsed_union)
    total_unbilled = total_uni - total_billed
    overall_wod    = total_billed / total_uni * 100 if total_uni else 0.0

    # ════════════════════════════════════════════════════════════════════════════
    # 4 TABS
    # ════════════════════════════════════════════════════════════════════════════
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Overview",
        "📈 Depth of Distribution",
        "🔍 Outlet Intelligence",
        "👤 Salesman Scorecard",
    ])

    # ══════════════════════════════════════════
    # TAB 1 — OVERVIEW
    # ══════════════════════════════════════════
    with tab1:
        section_header(
            f"Overview — {_fmt_month(sel_month_str)}",
            "Aggregate across selected salesmen",
        )

        # KPI row
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.metric("Total Universe", f"{total_uni:,}")
        with k2:
            st.metric("Billed This Month", f"{total_billed:,}")
        with k3:
            wod_delta = overall_wod - target_wod
            wod_color = "normal" if wod_delta >= 0 else "inverse"
            st.metric(
                "Overall WOD %", f"{overall_wod:.1f}%",
                delta=f"{wod_delta:+.1f}% vs target",
                delta_color=wod_color,
            )
        with k4:
            st.metric("Lapsed This Month", f"{total_lapsed:,}")

        st.divider()

        # WOD Trend chart
        st.plotly_chart(
            _chart_wod_trend(all_metrics, months_12, sel_sm, target_wod),
            use_container_width=True,
        )

        st.divider()

        # WOD Summary Table
        st.markdown("#### WOD Summary — sorted worst first")
        rows = []
        for sm in sel_sm:
            mo_row = all_metrics[sm][all_metrics[sm]["BillMonth"] == sel_month_str]
            if mo_row.empty:
                continue
            r = mo_row.iloc[0]
            rows.append({
                "Salesman":     sm,
                "Team":         SALESMAN_MAP[sm]["team"],
                "Universe":     int(r["universe"]),
                "Billed":       int(r["billed"]),
                "Unbilled":     int(r["unbilled"]),
                "WOD %":        f"{r['wod_pct']:.1f}%",
                "Strike Rate":  f"{r['strike_rate']:.1f}%",
                "vs Target":    f"{r['wod_pct'] - target_wod:+.1f}%",
            })
        if rows:
            summary_df = pd.DataFrame(rows).sort_values("WOD %")
            st.dataframe(
                _style_wod_table(summary_df, target_wod),
                use_container_width=True,
                hide_index=True,
            )

        st.divider()

        # Lapsed vs New vs Retained chart
        st.plotly_chart(
            _chart_outlet_movement(movement, sel_sm),
            use_container_width=True,
        )

    # ══════════════════════════════════════════
    # TAB 2 — DEPTH OF DISTRIBUTION
    # ══════════════════════════════════════════
    with tab2:
        section_header(
            f"Depth of Distribution — {_fmt_month(sel_month_str)}",
            f"vs {_fmt_month(prev_month_str)}",
        )

        # DOD Metrics Table
        dod_rows = []
        for sm in sel_sm:
            cur  = all_metrics[sm][all_metrics[sm]["BillMonth"] == sel_month_str]
            prev = all_metrics[sm][all_metrics[sm]["BillMonth"] == prev_month_str]
            if cur.empty:
                continue
            c = cur.iloc[0]
            p = prev.iloc[0] if not prev.empty else None
            dod_rows.append({
                "Salesman":          sm,
                "Avg Rev/Outlet":    format_inr(c["avg_rev"]),
                "Avg Cases/Outlet":  f"{c['avg_cas']:.1f}",
                "Avg Inv/Outlet":    f"{c['avg_inv']:.1f}",
                "MoM Rev Δ":         (
                    ("↑ " if c["avg_rev"] >= (p["avg_rev"] if p is not None else 0) else "↓ ") +
                    format_inr(abs(c["avg_rev"] - (p["avg_rev"] if p is not None else 0)))
                ),
                "MoM Cases Δ":       (
                    f"{'↑' if p is None or c['avg_cas'] >= p['avg_cas'] else '↓'} "
                    f"{abs(c['avg_cas'] - (p['avg_cas'] if p is not None else 0)):.1f}"
                ),
            })
        if dod_rows:
            st.dataframe(pd.DataFrame(dod_rows), use_container_width=True, hide_index=True)

        st.divider()

        # DOD Trend charts — 3 side by side
        st.markdown("#### DOD Trends — Last 12 Months")
        dc1, dc2, dc3 = st.columns(3)
        with dc1:
            st.plotly_chart(
                _chart_dod_trend(all_metrics, months_12, "avg_rev",
                                 "Avg Revenue / Outlet", sel_sm),
                use_container_width=True,
            )
        with dc2:
            st.plotly_chart(
                _chart_dod_trend(all_metrics, months_12, "avg_cas",
                                 "Avg Cases / Outlet", sel_sm, y_fmt=" cs"),
                use_container_width=True,
            )
        with dc3:
            st.plotly_chart(
                _chart_dod_trend(all_metrics, months_12, "avg_inv",
                                 "Avg Invoices / Outlet", sel_sm),
                use_container_width=True,
            )

        st.divider()

        # Revenue Concentration chart
        st.plotly_chart(
            _chart_concentration(concentration, sel_sm),
            use_container_width=True,
        )
        # Flag concentrated salesmen
        flagged = [sm for sm in sel_sm if concentration[sm]["concentrated"]]
        if flagged:
            st.warning(
                f"⚠️ High concentration risk (top 10% outlets > 60% revenue): "
                f"{', '.join(flagged)}"
            )

    # ══════════════════════════════════════════
    # TAB 3 — OUTLET INTELLIGENCE
    # ══════════════════════════════════════════
    with tab3:
        section_header("Outlet Intelligence", f"Reference month: {_fmt_month(sel_month_str)}")

        focus_sm = st.selectbox(
            "Focus Salesman",
            options=sel_sm,
            key="dist_focus_sm_t3",
        )

        if focus_sm:
            df_sm    = sm_data[focus_sm]
            uni_set  = uni_ids[focus_sm]
            today    = date.today()

            # Last bill date per party (across all history)
            last_date_ser = (
                df_sm.groupby("PartyID")["LastBillDate"].max()
            )
            # Per-party info
            party_info = (
                df_sm[["PartyID", "PartyName", "LicenseTypeID"]]
                .drop_duplicates("PartyID")
                .set_index("PartyID")
            )

            def _days_since(pid: str) -> int:
                d = last_date_ser.get(pid)
                if pd.isna(d) or d is None:
                    return 9999
                return (today - pd.Timestamp(d).date()).days

            def _last_bill_str(pid: str) -> str:
                d = last_date_ser.get(pid)
                if pd.isna(d) or d is None:
                    return "Never"
                return pd.Timestamp(d).strftime("%d %b %Y")

            # ── Lapsed Outlets ─────────────────────────────────────────────
            st.markdown("### 🔴 Lapsed This Month — Needs Immediate Visit")
            lapsed_ids = movement[focus_sm]["lapsed_ids"]
            if not lapsed_ids:
                st.success("No lapsed outlets this month!")
            else:
                # Previous month revenue per lapsed outlet
                prev_rev = (
                    df_sm[(df_sm["BillMonth"] == prev_month_str)
                          & df_sm["PartyID"].isin(lapsed_ids)]
                    .groupby("PartyID")["Revenue"].sum()
                )
                # Avg monthly revenue (all months)
                avg_rev = (
                    df_sm[df_sm["PartyID"].isin(lapsed_ids)]
                    .groupby(["PartyID", "BillMonth"])["Revenue"].sum()
                    .groupby("PartyID").mean()
                )
                lapsed_rows = []
                for pid in lapsed_ids:
                    info = party_info.loc[pid] if pid in party_info.index else None
                    lapsed_rows.append({
                        "Party Name":        info["PartyName"]    if info is not None else pid,
                        "Channel":           _LT_LABEL.get(info["LicenseTypeID"], "Other") if info is not None else "—",
                        "Last Bill Date":    _last_bill_str(pid),
                        "Days Since":        _days_since(pid),
                        "Last Mo Revenue":   format_inr(float(prev_rev.get(pid, 0))),
                        "Avg Monthly Rev":   format_inr(float(avg_rev.get(pid, 0))),
                    })
                lapsed_df = (
                    pd.DataFrame(lapsed_rows)
                    .sort_values("Days Since", ascending=False)
                    .reset_index(drop=True)
                )
                st.dataframe(lapsed_df, use_container_width=True, hide_index=True)
                csv_bytes = lapsed_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "📥 Download Lapsed Outlets CSV",
                    data=csv_bytes,
                    file_name=f"lapsed_{focus_sm}_{sel_month_str}.csv",
                    mime="text/csv",
                )

            st.divider()

            # ── Never Billed in Last 3 Months ─────────────────────────────
            st.markdown("### ⚠️ Not Billed in Last 3 Months")
            last_3 = months_12[-3:]
            active_3mo = frozenset(df_sm[df_sm["BillMonth"].isin(last_3)]["PartyID"])
            cold_ids   = uni_set - active_3mo

            if not cold_ids:
                st.success("All universe outlets billed in at least one of the last 3 months!")
            else:
                cold_rows = []
                for pid in cold_ids:
                    info = party_info.loc[pid] if pid in party_info.index else None
                    ds   = _days_since(pid)
                    cold_rows.append({
                        "Party Name":     info["PartyName"]    if info is not None else pid,
                        "Channel":        _LT_LABEL.get(info["LicenseTypeID"], "Other") if info is not None else "—",
                        "Last Bill Date": _last_bill_str(pid),
                        "Days Since":     ds,   # keep int; 9999 = never billed
                    })
                cold_df = (
                    pd.DataFrame(cold_rows)
                    .sort_values("Days Since", ascending=False)  # sort by int first
                    .reset_index(drop=True)
                )
                # Format ALL values as strings (consistent type) so Arrow can
                # serialise the column; "Never" replaces 9999.
                cold_df["Days Since"] = cold_df["Days Since"].apply(
                    lambda x: "Never" if x >= 9999 else f"{int(x)}"
                )
                st.dataframe(cold_df, use_container_width=True, hide_index=True)

            st.divider()

            # ── New Outlets This Month ─────────────────────────────────────
            st.markdown("### 🟢 New Outlets This Month")
            new_ids = movement[focus_sm]["new_ids"]
            # "New" = never appeared in any prior month in the 13-month window
            first_seen = df_sm.groupby("PartyID")["BillMonth"].min()
            truly_new  = frozenset(
                pid for pid in new_ids
                if first_seen.get(pid, "9999-99") == sel_month_str
            )
            if not truly_new:
                st.info("No brand-new outlets this month (all new-active outlets were seen before).")
            else:
                new_mo_df = df_sm[
                    df_sm["BillMonth"].eq(sel_month_str) & df_sm["PartyID"].isin(truly_new)
                ]
                new_party = (
                    new_mo_df
                    .groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
                    .agg(Revenue=("Revenue", "sum"), Cases=("Cases", "sum"))
                )
                new_party["Channel"]        = new_party["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
                new_party["First Bill Date"] = new_party["PartyID"].apply(_last_bill_str)
                new_party["Revenue"]        = new_party["Revenue"].apply(format_inr)
                st.dataframe(
                    new_party[["PartyName", "Channel", "First Bill Date", "Revenue", "Cases"]]
                    .rename(columns={"PartyName": "Party Name"}),
                    use_container_width=True,
                    hide_index=True,
                )

    # ══════════════════════════════════════════
    # TAB 4 — SALESMAN SCORECARD
    # ══════════════════════════════════════════
    with tab4:
        card_sm = st.selectbox(
            "Select Salesman",
            options=all_sm,
            key="dist_card_sm",
        )

        if card_sm:
            df_sm    = sm_data[card_sm]
            uni_set  = uni_ids[card_sm]
            metrics  = all_metrics[card_sm]
            mo_row   = metrics[metrics["BillMonth"] == sel_month_str]
            mv       = movement[card_sm]

            uni_size   = len(uni_set)
            cur_billed = int(mo_row["billed"].iloc[0])   if not mo_row.empty else 0
            cur_wod    = mo_row["wod_pct"].iloc[0]        if not mo_row.empty else 0.0
            cur_sr     = mo_row["strike_rate"].iloc[0]    if not mo_row.empty else 0.0

            st.markdown(
                f"## {card_sm}  "
                f"<span style='font-size:1rem;color:#888;'>{SALESMAN_MAP[card_sm]['team']}</span>  "
                f"<span style='font-size:0.9rem;color:#E8A838;'>Universe: {uni_size:,} outlets</span>",
                unsafe_allow_html=True,
            )
            st.divider()

            # KPI cards
            sc1, sc2, sc3, sc4 = st.columns(4)
            with sc1:
                st.metric("WOD %",      f"{cur_wod:.1f}%",
                          delta=f"{cur_wod - target_wod:+.1f}% vs target",
                          delta_color="normal" if cur_wod >= target_wod else "inverse")
            with sc2:
                st.metric("Strike Rate", f"{cur_sr:.1f}%")
            with sc3:
                st.metric("Lapsed",     mv["lapsed"])
            with sc4:
                st.metric("New Outlets", mv["new"])

            st.divider()

            # Mini charts 2×2
            mc1, mc2 = st.columns(2)
            with mc1:
                st.plotly_chart(
                    _chart_wod_trend(all_metrics, months_12, [card_sm], target_wod),
                    use_container_width=True,
                )
                st.plotly_chart(
                    _chart_dod_trend(all_metrics, months_12, "avg_cas",
                                     "Avg Cases / Outlet", [card_sm], " cs"),
                    use_container_width=True,
                )
            with mc2:
                st.plotly_chart(
                    _chart_dod_trend(all_metrics, months_12, "avg_rev",
                                     "Avg Revenue / Outlet", [card_sm]),
                    use_container_width=True,
                )
                # Lapsed vs New trend
                lapsed_trend = []
                for mo in months_12:
                    ids = _lapsed_new_ids(df_sm, mo, _prev_month_str(mo), uni_ids[card_sm])
                    lapsed_trend.append({
                        "month_label": _fmt_month(mo),
                        "Lapsed": len(ids["lapsed"]),
                        "New":    len(ids["new"]),
                    })
                lt_df = pd.DataFrame(lapsed_trend)
                fig_lt = px.line(
                    lt_df.melt("month_label", var_name="Type", value_name="Outlets"),
                    x="month_label", y="Outlets", color="Type",
                    title="Lapsed vs New Outlets Trend",
                    color_discrete_map={"Lapsed": "#e74c3c", "New": "#2ecc71"},
                )
                fig_lt.update_layout(**_LAYOUT,
                                     xaxis=dict(tickangle=-30, **_GRID),
                                     yaxis=dict(**_GRID))
                st.plotly_chart(fig_lt, use_container_width=True)

            st.divider()

            # Top 10 outlets by revenue this month
            st.markdown(f"#### Top 10 Outlets — {_fmt_month(sel_month_str)}")
            top10_df = _party_month_agg(df_sm, sel_month_str).nlargest(10, "Revenue").copy()
            if not top10_df.empty:
                top10_df.insert(0, "Rank", range(1, len(top10_df) + 1))
                top10_df["Channel"] = top10_df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
                top10_df["Revenue"] = top10_df["Revenue"].apply(format_inr)
                st.dataframe(
                    top10_df[["Rank", "PartyName", "Channel", "InvoiceCount", "Cases", "Revenue"]]
                    .rename(columns={"PartyName": "Party", "InvoiceCount": "Invoices"}),
                    use_container_width=True, hide_index=True,
                )

            st.divider()

            # Bottom 10 — low frequency outlets (billed only once in last 3 months)
            st.markdown("#### ⚠️ Low-Frequency Outlets — Billed Once in Last 3 Months")
            last_3 = months_12[-3:]
            lo_df = (
                df_sm[df_sm["BillMonth"].isin(last_3)]
                .groupby(["PartyID", "PartyName", "LicenseTypeID"])
                .agg(months_active=("BillMonth", "nunique"), Revenue=("Revenue", "sum"))
                .reset_index()
            )
            lo_df = lo_df[lo_df["months_active"] == 1].copy()
            if lo_df.empty:
                st.success("All outlets billed in 2+ of the last 3 months!")
            else:
                today = date.today()
                lo_df["Last Bill Date"] = lo_df["PartyID"].apply(
                    lambda pid: (
                        df_sm[df_sm["PartyID"] == pid]["LastBillDate"].max()
                    )
                )
                lo_df["Days Since"] = lo_df["Last Bill Date"].apply(
                    lambda d: (today - pd.Timestamp(d).date()).days
                    if not pd.isna(d) else 9999
                )
                lo_df["Risk"] = lo_df["Days Since"].apply(
                    lambda d: "🔴 High" if d > 60 else ("🟡 Medium" if d > 30 else "🟢 Low")
                )
                lo_df["Last Bill Date"] = lo_df["Last Bill Date"].apply(
                    lambda d: pd.Timestamp(d).strftime("%d %b %Y") if not pd.isna(d) else "—"
                )
                lo_df["Channel"] = lo_df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
                lo_df["Revenue"] = lo_df["Revenue"].apply(format_inr)
                st.dataframe(
                    lo_df[["PartyName", "Channel", "Last Bill Date", "Days Since", "Revenue", "Risk"]]
                    .rename(columns={"PartyName": "Party"})
                    .sort_values("Days Since", ascending=False),
                    use_container_width=True, hide_index=True,
                )
