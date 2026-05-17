"""src/salesman.py — Team Performance (individual drill).

Pick one salesman and see their full picture: profile, benchmarks vs
last year and trailing averages, revenue + WOD trends, top brands,
top customers, and outlet status (active / lapsed / opportunity).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

from db import run_query
from utils.helpers import format_inr, CASES_SQL_EXPR as _CASES
from src.distribution import (
    SALESMAN_MAP,
    _load_master,
    _load_active_universe,
    _build_universes,
    _last_n_month_strs,
    _last_complete_month,
    _fmt_month,
    _prev_month_str,
)

SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Principal lookup (mirrors sales.py) ──────────────────────────────────────
_PRINCIPAL_NAMES: dict[str, str] = {
    "C00025": "United Spirits",
    "C00040": "Diageo",
    "C00039": "United Breweries",
    "C00056": "Brown-Forman",
}
_LT_LABEL: dict[str, str] = {
    "180001": "FL-II Wine Shop",
    "180002": "FL-III Permit Room",
    "180004": "FL-BR-II Beer Shopee",
    "180005": "FL-IV Club",
    "180007": "FL-IV One Day",
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def _load_brand_party_revenue(start: date, end: date,
                              principal_ids: tuple[str, ...]) -> pd.DataFrame:
    """Brand × Party revenue for date range; filter Python-side by universe."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
            d.PartyID,
            b.BrandName,
            b.CompanyID,
            SUM(CAST(vi.TotalAmount AS FLOAT))  AS Revenue,
            SUM({_CASES}) AS Cases
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
                  AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          {company_sql}
        GROUP BY d.PartyID, b.BrandName, b.CompanyID
    """
    df = run_query(sql, (str(start), str(end)) + company_params)
    if not df.empty:
        df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
        df["Cases"]   = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _shift_year(d: date, years: int) -> date:
    if relativedelta:
        return d - relativedelta(years=years)
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def _month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _yoy_delta(curr: float, prev: float) -> tuple[str, str]:
    if prev == 0:
        return ("→ new", "#6b7280")
    pct = (curr - prev) / prev * 100
    if pct > 0.05:
        return (f"↑ {pct:.1f}%", "#16a34a")
    if pct < -0.05:
        return (f"↓ {abs(pct):.1f}%", "#dc2626")
    return ("→ flat", "#6b7280")


def _kpi_card(label: str, value: str, delta_text: str,
              delta_color: str, accent: str) -> str:
    return f"""
    <div style='background:#fff;border:1px solid #e5e7eb;border-radius:8px;
                border-left:4px solid {accent};padding:12px 16px;
                box-shadow:0 1px 2px rgba(0,0,0,0.04);'>
        <div style='font-size:0.7rem;color:#6b7280;
                    text-transform:uppercase;letter-spacing:0.05em'>{label}</div>
        <div style='font-size:1.45rem;font-weight:700;color:#111827;
                    margin-top:4px;line-height:1.15'>{value}</div>
        <div style='font-size:0.78rem;color:{delta_color};margin-top:2px;
                    font-weight:600'>{delta_text}</div>
    </div>
    """


def _status_card(label: str, value: str, sub: str, accent: str) -> str:
    return f"""
    <div style='background:#fff;border:1px solid #e5e7eb;border-radius:8px;
                border-left:5px solid {accent};padding:14px 18px;'>
        <div style='font-size:0.78rem;color:#6b7280;text-transform:uppercase;
                    letter-spacing:0.04em;font-weight:600'>{label}</div>
        <div style='font-size:1.9rem;font-weight:700;color:#111827;
                    line-height:1.1;margin-top:4px'>{value}</div>
        <div style='font-size:0.78rem;color:#6b7280;margin-top:4px'>{sub}</div>
    </div>
    """


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _render_profile_header(salesman: str, cfg: dict,
                           universe_size: int, wod_pct: float,
                           revenue_period: float) -> None:
    principals = ", ".join(_PRINCIPAL_NAMES.get(p, p) for p in cfg["principals"])
    rev_cr = revenue_period / 1e7
    st.markdown(f"""
    <div style='background:#1B4F72;color:#fff;border-radius:10px;
                padding:20px 24px;display:flex;justify-content:space-between;
                align-items:center;margin-bottom:18px;
                border-bottom:3px solid #E8A838'>
        <div>
            <div style='font-size:1.5rem;font-weight:700'>{salesman}</div>
            <div style='font-size:0.78rem;opacity:0.72;margin-top:4px'>
                {cfg.get('team','')}  ·  {principals}
            </div>
        </div>
        <div style='display:flex;gap:32px;text-align:right'>
            <div>
                <div style='font-size:1.5rem;font-weight:700'>{universe_size:,}</div>
                <div style='font-size:0.66rem;opacity:0.72;
                            text-transform:uppercase;letter-spacing:0.06em'>
                    Universe
                </div>
            </div>
            <div>
                <div style='font-size:1.5rem;font-weight:700'>{wod_pct:.1f}%</div>
                <div style='font-size:0.66rem;opacity:0.72;
                            text-transform:uppercase;letter-spacing:0.06em'>
                    WOD This Month
                </div>
            </div>
            <div>
                <div style='font-size:1.5rem;font-weight:700'>₹{rev_cr:.2f} Cr</div>
                <div style='font-size:0.66rem;opacity:0.72;
                            text-transform:uppercase;letter-spacing:0.06em'>
                    Revenue (Period)
                </div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def _render_benchmarks(sm_master: pd.DataFrame, universe: frozenset,
                       universe_size: int,
                       all_sm_metrics: dict[str, dict],
                       salesman: str, cfg: dict) -> None:
    st.markdown("##### Performance vs benchmarks")

    current_month = _last_complete_month()
    monthly_rev = (
        sm_master.groupby("BillMonth")["Revenue"].sum().to_dict()
    )
    curr_rev = float(monthly_rev.get(current_month, 0.0))

    # vs same month LY
    yr, mo = int(current_month[:4]), int(current_month[5:])
    ly_key = f"{yr-1:04d}-{mo:02d}"
    ly_rev = float(monthly_rev.get(ly_key, 0.0))
    rev_delta, rev_color = _yoy_delta(curr_rev, ly_rev)

    # vs 3 / 6 month averages
    prev_3 = _last_n_month_strs(4)[:-1]
    prev_6 = _last_n_month_strs(7)[:-1]
    avg_3  = sum(monthly_rev.get(m, 0) for m in prev_3) / max(len(prev_3), 1)
    avg_6  = sum(monthly_rev.get(m, 0) for m in prev_6) / max(len(prev_6), 1)
    d3_text, d3_color = _yoy_delta(curr_rev, avg_3)
    d6_text, d6_color = _yoy_delta(curr_rev, avg_6)

    # Team rank
    team_name = cfg.get("team", "")
    team_members = [k for k, v in SALESMAN_MAP.items() if v.get("team") == team_name]
    team_revenues = [
        (sm, all_sm_metrics.get(sm, {}).get("revenue_month", 0))
        for sm in team_members
    ]
    team_revenues.sort(key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (s, _) in enumerate(team_revenues) if s == salesman), len(team_revenues))
    top_sm = team_revenues[0][0] if team_revenues else "—"

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(_kpi_card(
            "vs Same Month LY",
            f"₹{curr_rev/1e7:.2f} Cr",
            f"{rev_delta} (LY: ₹{ly_rev/1e7:.2f} Cr)",
            rev_color, "#1B4F72",
        ), unsafe_allow_html=True)
    with c2:
        st.markdown(_kpi_card(
            "vs 3-month avg",
            d3_text,
            f"Avg: ₹{avg_3/1e7:.2f} Cr",
            d3_color, "#378ADD",
        ), unsafe_allow_html=True)
    with c3:
        st.markdown(_kpi_card(
            "vs 6-month avg",
            d6_text,
            f"Avg: ₹{avg_6/1e7:.2f} Cr",
            d6_color, "#1D9E75",
        ), unsafe_allow_html=True)
    with c4:
        st.markdown(_kpi_card(
            "Team Rank",
            f"{rank} / {max(len(team_revenues), 1)}",
            f"Top: {top_sm}",
            "#6b7280", "#EF9F27",
        ), unsafe_allow_html=True)


def _render_trends(sm_master: pd.DataFrame, universe: frozenset,
                   universe_size: int) -> None:
    months_12 = _last_n_month_strs(12)
    monthly_rev = sm_master.groupby("BillMonth")["Revenue"].sum().to_dict()

    # Build per-month dataframes
    rows = []
    for i, m in enumerate(months_12):
        rev = float(monthly_rev.get(m, 0))
        prev_rev = float(monthly_rev.get(months_12[i - 1], 0)) if i > 0 else rev
        # WOD% for this month
        billed = frozenset(
            sm_master[sm_master["BillMonth"] == m]["PartyID"]
        ) & universe
        wod = (len(billed) / universe_size * 100) if universe_size else 0.0
        # Direction vs prev
        if i == 0:
            direction = "flat"
        elif rev > prev_rev * 1.02:
            direction = "up"
        elif rev < prev_rev * 0.98:
            direction = "down"
        else:
            direction = "flat"
        rows.append({
            "Month":     m,
            "Label":     _fmt_month(m),
            "RevCr":     rev / 1e7,
            "Wod":       round(wod, 1),
            "Direction": direction,
        })
    df = pd.DataFrame(rows)

    col_l, col_r = st.columns(2)

    # ── LEFT: Revenue trend ──
    with col_l:
        st.markdown("##### Revenue trend — 12 months")
        if df["RevCr"].sum() == 0:
            st.info("No revenue in the last 12 months.")
        else:
            colors_dir = {"up": "#1B4F72", "flat": "#9CA3AF", "down": "#EF9F27"}
            colors = [colors_dir[d] for d in df["Direction"]]
            fig = go.Figure(go.Bar(
                x=df["Label"], y=df["RevCr"],
                marker_color=colors,
                text=[f"₹{v:.2f}" for v in df["RevCr"]],
                textposition="outside",
                hovertemplate="<b>%{x}</b><br>₹%{y:.2f} Cr<extra></extra>",
            ))
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                template="plotly_white",
                margin=dict(l=16, r=16, t=16, b=16),
                height=320,
                xaxis=dict(gridcolor="#E8E8E8"),
                yaxis=dict(gridcolor="#E8E8E8", ticksuffix=" Cr",
                           title="Revenue (₹ Cr)"),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── RIGHT: WOD% trend ──
    with col_r:
        st.markdown("##### WOD % trend — 12 months")
        if universe_size == 0:
            st.info("No active universe for this salesman.")
        else:
            def _bar_color(w):
                if w >= 70: return "#1D9E75"
                if w >= 50: return "#EF9F27"
                return "#B4B2A9"
            colors = [_bar_color(w) for w in df["Wod"]]
            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=df["Label"], y=df["Wod"],
                marker_color=colors,
                text=[f"{w:.0f}%" for w in df["Wod"]],
                textposition="outside",
                hovertemplate="<b>%{x}</b><br>WOD %{y:.1f}%<extra></extra>",
            ))
            # Dashed target line at 70%
            fig.add_hline(y=70, line_width=1.5, line_dash="dash",
                          line_color="#1B4F72",
                          annotation_text="70% target",
                          annotation_position="top right",
                          annotation_font_color="#1B4F72")
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                template="plotly_white",
                margin=dict(l=16, r=16, t=16, b=16),
                height=320,
                xaxis=dict(gridcolor="#E8E8E8"),
                yaxis=dict(gridcolor="#E8E8E8", ticksuffix="%",
                           range=[0, max(100, df["Wod"].max() * 1.15)]),
            )
            st.plotly_chart(fig, use_container_width=True)


def _render_portfolio_and_customers(sm_master: pd.DataFrame,
                                    universe: frozenset,
                                    salesman: str, cfg: dict,
                                    start: date, end: date,
                                    current_month: str, prev_month: str) -> None:
    col_l, col_r = st.columns(2)

    # ── LEFT: Top brands ──
    with col_l:
        st.markdown("##### Top brands in portfolio")
        principal_ids = tuple(cfg["principals"])
        brand_party = _load_brand_party_revenue(start, end, principal_ids)
        if brand_party.empty:
            st.info("No brand data for this salesman in the period.")
        else:
            uni_set = set(universe)
            brand_party = brand_party[brand_party["PartyID"].isin(uni_set)]
            if brand_party.empty:
                st.info("No billing inside this salesman's universe.")
            else:
                brand_tbl = (
                    brand_party.groupby("BrandName", as_index=False)
                    .agg(Cases=("Cases", "sum"), Revenue=("Revenue", "sum"))
                    .sort_values("Revenue", ascending=False)
                    .head(10)
                    .rename(columns={"BrandName": "Brand"})
                )
                st.dataframe(
                    brand_tbl.style.format({"Revenue": format_inr, "Cases": "{:,.2f}"}),
                    use_container_width=True, hide_index=True,
                )

    # ── RIGHT: Top customers ──
    with col_r:
        st.markdown("##### Top customers this period")
        months_in_range = sm_master["BillMonth"].unique()
        in_period = sm_master[
            sm_master["PartyID"].isin(universe)
        ]
        if in_period.empty:
            st.info("No customer billing in this period.")
        else:
            cust = (
                in_period.groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
                .agg(Revenue=("Revenue", "sum"))
                .sort_values("Revenue", ascending=False)
                .head(10)
            )
            # Status: Active this month / Lapsed (billed last month not this) / Slow
            cm_set = frozenset(
                sm_master[sm_master["BillMonth"] == current_month]["PartyID"]
            )
            pm_set = frozenset(
                sm_master[sm_master["BillMonth"] == prev_month]["PartyID"]
            )

            def _status(pid: str) -> str:
                if pid in cm_set:
                    return "🟢 Active"
                if pid in pm_set:
                    return "🔴 Lapsed"
                return "🟡 Slow"

            cust["Status"]  = cust["PartyID"].apply(_status)
            cust["Channel"] = cust["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
            display = cust[["PartyName", "Channel", "Status", "Revenue"]].rename(
                columns={"PartyName": "Party"}
            )
            st.dataframe(
                display.style.format({"Revenue": format_inr}),
                use_container_width=True, hide_index=True,
            )


def _render_outlet_status(sm_master: pd.DataFrame, universe: frozenset,
                          universe_size: int,
                          current_month: str, prev_month: str) -> None:
    st.markdown("##### Outlet activity status")

    cm_set = frozenset(
        sm_master[sm_master["BillMonth"] == current_month]["PartyID"]
    ) & universe
    pm_set = frozenset(
        sm_master[sm_master["BillMonth"] == prev_month]["PartyID"]
    ) & universe

    # Lapsed: billed previous month but not current
    lapsed_set = pm_set - cm_set

    # Opportunity: <=1 bills in last 6 months
    months_6 = _last_n_month_strs(6)
    bills_per_party = (
        sm_master[sm_master["BillMonth"].isin(months_6)]
        .groupby("PartyID")["BillMonth"].nunique()
    )
    active_2plus = set(bills_per_party[bills_per_party > 1].index)
    opp_set = universe - active_2plus

    active_pct = (len(cm_set) / universe_size * 100) if universe_size else 0.0

    col_a, col_l, col_o = st.columns(3)
    with col_a:
        st.markdown(_status_card(
            "Active this month",
            f"{len(cm_set):,}",
            f"{active_pct:.1f}% of {universe_size:,} universe",
            "#1D9E75",
        ), unsafe_allow_html=True)
    with col_l:
        st.markdown(_status_card(
            "Lapsed (action needed)",
            f"{len(lapsed_set):,}",
            "Billed last month, not this",
            "#DC2626",
        ), unsafe_allow_html=True)
    with col_o:
        st.markdown(_status_card(
            "Opportunity",
            f"{len(opp_set):,}",
            "≤1 bills in last 6 months",
            "#EF9F27",
        ), unsafe_allow_html=True)

    # ── Expandable lists ──
    party_info = (
        sm_master.groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
        .agg(LastBill=("LastBillDate", "max"),
             Cases=("Cases", "sum"),
             Revenue=("Revenue", "sum"))
    )
    party_info["Channel"] = party_info["LicenseTypeID"].map(_LT_LABEL).fillna("Other")

    def _show_subset(ids: set, title: str) -> None:
        sub = party_info[party_info["PartyID"].isin(ids)].copy()
        if sub.empty:
            st.info(f"No outlets matched.")
            return
        sub["LastBill"] = pd.to_datetime(sub["LastBill"], errors="coerce") \
            .dt.strftime("%d %b %Y").fillna("Never")
        sub = sub.sort_values("Revenue", ascending=False).head(50)
        st.dataframe(
            sub[["PartyName", "Channel", "LastBill", "Revenue"]].rename(
                columns={"PartyName": "Party", "LastBill": "Last Bill"}
            ).style.format({"Revenue": format_inr}),
            use_container_width=True, hide_index=True,
        )

    with st.expander(f"Show {len(cm_set)} active outlets"):
        _show_subset(set(cm_set), "Active")
    with st.expander(f"Show {len(lapsed_set)} lapsed outlets"):
        _show_subset(set(lapsed_set), "Lapsed")
    with st.expander(f"Show {len(opp_set)} opportunity outlets"):
        _show_subset(set(opp_set), "Opportunity")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("Team Performance")
    st.caption("Individual salesman deep dive — performance and trends")
    st.divider()

    # ── Salesman selector ──
    all_sm = list(SALESMAN_MAP.keys())
    salesman = st.selectbox(
        "Select Salesman",
        options=all_sm,
        format_func=lambda x: f"{x} — {SALESMAN_MAP[x].get('team', '')}",
        key="sm_select",
    )
    cfg = SALESMAN_MAP[salesman]

    # ── Date range ──
    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    c1, _ = st.columns([2, 1])
    with c1:
        date_range = st.date_input(
            "Analysis period",
            value=(fy_start, today),
            key="sm_dates",
        )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
    else:
        start, end = fy_start, today
    if start > end:
        st.warning("Start date must be before end date.")
        return

    # ── Load data ──
    with st.spinner("Loading team performance data…"):
        master = _load_master(24)
        uni_df = _load_active_universe()

    if master.empty or uni_df.empty:
        st.error("No data available. Check DB connection.")
        return

    universes, _ = _build_universes(uni_df)
    universe      = universes.get(salesman, frozenset())
    universe_size = len(universe)

    # Restrict master to this salesman's universe + principals
    sm_master = master[
        (master["CompanyID"].isin(cfg["principals"]))
        & (master["PartyID"].isin(universe))
    ].copy()

    current_month = _last_complete_month()
    prev_month    = _prev_month_str(current_month)

    # Profile header metrics
    cm_set = frozenset(
        sm_master[sm_master["BillMonth"] == current_month]["PartyID"]
    ) & universe
    wod_pct = (len(cm_set) / universe_size * 100) if universe_size else 0.0

    # Revenue across the selected analysis period — translate to billing months
    s_month, e_month = _month_str(start), _month_str(end)
    period_months = [
        m for m in sorted(sm_master["BillMonth"].unique())
        if s_month <= m <= e_month
    ]
    revenue_period = float(
        sm_master[sm_master["BillMonth"].isin(period_months)]["Revenue"].sum()
    )

    # ── Profile header ──
    _render_profile_header(salesman, cfg, universe_size, wod_pct, revenue_period)

    # Build all-salesmen metrics dict for ranking
    all_sm_metrics: dict[str, dict] = {}
    for sm_key, sm_cfg in SALESMAN_MAP.items():
        sm_uni = universes.get(sm_key, frozenset())
        if not sm_uni:
            all_sm_metrics[sm_key] = {"revenue_month": 0.0}
            continue
        sub = master[
            (master["CompanyID"].isin(sm_cfg["principals"]))
            & (master["PartyID"].isin(sm_uni))
            & (master["BillMonth"] == current_month)
        ]
        all_sm_metrics[sm_key] = {"revenue_month": float(sub["Revenue"].sum())}

    # ── Benchmarks ──
    _render_benchmarks(sm_master, universe, universe_size,
                       all_sm_metrics, salesman, cfg)
    st.divider()

    # ── Trends ──
    _render_trends(sm_master, universe, universe_size)
    st.divider()

    # ── Portfolio + Customers ──
    _render_portfolio_and_customers(
        sm_master, universe, salesman, cfg, start, end,
        current_month, prev_month,
    )
    st.divider()

    # ── Outlet status ──
    _render_outlet_status(sm_master, universe, universe_size,
                          current_month, prev_month)
