"""src/principal.py — Principal Meeting Pack.

A focused, principal-wise dashboard the owner can open right before a
company review meeting (USL / Diageo / Brown-Forman / United Breweries).
Reuses the billing-active universe machinery from distribution.py, so
WOD numbers match the ERP report logic.
"""
from __future__ import annotations

from datetime import date
import io
import re

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, CASES_SQL_EXPR as _CASES, _apply_date_preset
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
from src.inventory import _normalize_brand_display

# ── Sales transaction type IDs (mirrors distribution.py) ─────────────────────
SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Principal config (keys are SALESMAN_MAP short names) ─────────────────────
PRINCIPAL_CONFIG: dict[str, dict] = {
    "United Spirits": {
        "company_id": "C00025",
        "color":      "#1B4F72",
        "salesmen":   ["Shashank", "Sachin",
                       "Tulsiram", "Saurabh", "Miran", "Prashant", "Atish"],
    },
    "Diageo": {
        "company_id": "C00040",
        "color":      "#378ADD",
        "salesmen":   ["Ajay", "Deepak Patil",
                       "Tulsiram", "Saurabh", "Miran", "Prashant", "Atish"],
    },
    "Brown-Forman": {
        "company_id": "C00056",
        "color":      "#EF9F27",
        # Wine Shops (Ajay/Deepak Patil) + KW Institution
        "salesmen":   ["Ajay", "Deepak Patil",
                       "Rohit Lakhan", "Shashank Desai",
                       "Pranav", "Rahul Ghone"],
    },
    "United Breweries": {
        "company_id": "C00039",
        "color":      "#1D9E75",
        # KW Beer (Aabid/Omkar) + KW Institution + PCMC Institution
        "salesmen":   ["Aabid", "Omkar",
                       "Rohit Lakhan", "Shashank Desai", "Pranav", "Rahul Ghone",
                       "Gajendra Das", "Amol Sathe", "Piyush Arora"],
    },
}

# ── Light chart theme ────────────────────────────────────────────────────────
_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#1A1A1A"),
    template="plotly_white",
    margin=dict(l=16, r=16, t=36, b=16),
)
_GRID = dict(gridcolor="#E8E8E8")

# ── Channel labels (mirror distribution.py) ──────────────────────────────────
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

@st.cache_data(ttl=3600, show_spinner=False)
def _load_principal_brands(company_id: str, start: date, end: date) -> pd.DataFrame:
    """Brand-level revenue + cases for a principal across a date range."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT b.BrandName,
               SUM(CAST(vi.TotalAmount AS FLOAT))  AS Revenue,
               SUM({_CASES}) AS Cases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            -- free goods INCLUDED (stock sent to outlet; Rs0 revenue)
            AND vi.ItemID      LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
            END
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND b.CompanyID   = ?
          AND CAST(h.VoucherDate AS date) BETWEEN ? AND ?
        GROUP BY b.BrandName
        ORDER BY Revenue DESC
    """
    df = run_query(sql, (company_id, str(start), str(end)))
    if not df.empty:
        df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
        df["Cases"]   = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _wod_color(wod_pct: float) -> tuple[str, str]:
    """Return (background, text) hex colours for a WOD% value."""
    if wod_pct >= 70:
        return "#dcfce7", "#166534"   # green
    if wod_pct >= 50:
        return "#fef3c7", "#854d0e"   # amber
    return "#fee2e2", "#991b1b"       # red


def _months_in_range(start: date, end: date) -> list[str]:
    """Return YYYY-MM strings spanning start..end inclusive."""
    months: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _trend_arrow(wods: list[float]) -> tuple[str, str]:
    """Compare last value to first (last 3 months WOD%) → (arrow, color)."""
    if len(wods) < 2:
        return "→ Stable", "#6b7280"
    diff = wods[-1] - wods[0]
    if diff > 5:
        return "↑ Improving", "#16a34a"
    if diff < -5:
        return "↓ Declining", "#dc2626"
    return "→ Stable", "#6b7280"


def _wod_for_month(principal_master: pd.DataFrame,
                   month_str: str,
                   universe: frozenset) -> tuple[int, float]:
    """Outlets in universe that billed this principal in `month_str`."""
    mo = principal_master[principal_master["BillMonth"] == month_str]
    billed = frozenset(mo["PartyID"]) & universe
    pct = len(billed) / len(universe) * 100 if universe else 0.0
    return len(billed), pct


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _render_kpis(principal_master: pd.DataFrame,
                 principal_uni: frozenset,
                 start: date, end: date,
                 current_month: str,
                 cfg: dict) -> None:
    """Section 1 — 5 KPI cards."""
    st.markdown("### Key Metrics")
    st.caption(f"Period: {start.strftime('%d %b %Y')} → {end.strftime('%d %b %Y')}")

    months = _months_in_range(start, end)
    period_df = principal_master[principal_master["BillMonth"].isin(months)]

    total_rev   = float(period_df["Revenue"].sum())
    total_cases = float(period_df["Cases"].sum())
    uni_size    = len(principal_uni)
    cm_df       = principal_master[principal_master["BillMonth"] == current_month]
    cm_billed   = len(frozenset(cm_df["PartyID"]) & principal_uni)
    wod_pct     = (cm_billed / uni_size * 100) if uni_size else 0.0

    # Accent bar via column border using CSS injection
    color = cfg["color"]
    st.markdown(
        f"""<style>
        div[data-testid="stMetric"] {{
            border-left: 4px solid {color};
            padding-left: 0.75rem;
            background: rgba(255,255,255,0.55);
        }}
        </style>""",
        unsafe_allow_html=True,
    )

    k1, k2, k3, k4, k5 = st.columns(5)
    with k1:
        st.metric("Total Revenue",     format_inr(total_rev))
    with k2:
        st.metric("Total Cases",       f"{total_cases:,.2f}")
    with k3:
        st.metric("Active Universe",   f"{uni_size:,}")
    with k4:
        st.metric(f"Billed {_fmt_month(current_month)}", f"{cm_billed:,}")
    with k5:
        st.metric("WOD %",             f"{wod_pct:.1f}%")


def _render_wod_grid(principal_master: pd.DataFrame,
                     principal_uni: frozenset,
                     months_12: list[str],
                     cfg: dict) -> None:
    """Section 2 — 12-month WOD% grid (2 rows × 6 cards)."""
    st.markdown("### WOD Trend — Last 12 Months")
    st.caption("Width of Distribution: outlets billed vs active universe")

    uni_size = len(principal_uni)

    def _card(month_str: str) -> str:
        billed, pct = _wod_for_month(principal_master, month_str, principal_uni)
        bg, fg = _wod_color(pct)
        return (
            f"<div style='background:{bg}; padding:0.7rem 0.8rem; "
            f"border-radius:8px; text-align:center;'>"
            f"<div style='font-size:0.72rem; color:#374151; "
            f"text-transform:uppercase; letter-spacing:0.04em;'>"
            f"{_fmt_month(month_str)}</div>"
            f"<div style='font-size:1.8rem; font-weight:700; "
            f"color:{fg}; line-height:1.1; margin-top:2px'>{pct:.0f}%</div>"
            f"<div style='font-size:0.72rem; color:#4b5563'>"
            f"{billed} / {uni_size}</div></div>"
        )

    # Two rows of 6 months each
    row1 = st.columns(6)
    row2 = st.columns(6)
    for i, mo in enumerate(months_12[-12:]):
        col = row1[i] if i < 6 else row2[i - 6]
        with col:
            st.markdown(_card(mo), unsafe_allow_html=True)


def _render_salesman_table(master: pd.DataFrame,
                           by_principal: dict[str, dict[str, frozenset]],
                           current_month: str,
                           months_12: list[str],
                           cfg: dict) -> pd.DataFrame:
    """Section 3 — Salesman performance table for this principal."""
    st.markdown("### Salesman Performance")
    st.caption(f"Performance for {_fmt_month(current_month)} · sorted by revenue")

    company_id = cfg["company_id"]
    rows = []
    last_3 = months_12[-3:]

    for sm in cfg["salesmen"]:
        if sm not in by_principal:
            continue
        uni = by_principal[sm].get(company_id, frozenset())
        uni_size = len(uni)

        # Restrict master to this salesman's universe ∩ principal billing
        sm_df = master[
            (master["CompanyID"] == company_id) &
            (master["PartyID"].isin(uni))
        ]

        # Current month metrics
        mo = sm_df[sm_df["BillMonth"] == current_month]
        billed_set = frozenset(mo["PartyID"])
        billed_cnt = len(billed_set)
        wod_pct    = (billed_cnt / uni_size * 100) if uni_size else 0.0
        cases_mo   = int(mo["Cases"].sum())
        rev_mo     = float(mo["Revenue"].sum())
        avg_cases  = (cases_mo / billed_cnt) if billed_cnt else 0.0

        # Trend over last 3 months
        wods_3 = [
            _wod_for_month(sm_df, m, uni)[1] for m in last_3
        ]
        trend_text, trend_color = _trend_arrow(wods_3)

        rows.append({
            "Salesman":         sm,
            "Universe":         uni_size,
            "Billed":           billed_cnt,
            "WOD %":            round(wod_pct, 1),
            "Cases":            cases_mo,
            "Revenue":          rev_mo,
            "Avg Cases/Outlet": round(avg_cases, 1),
            "Trend":            trend_text,
            "_trend_color":     trend_color,
        })

    if not rows:
        st.info("No salesmen mapped to this principal.")
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("Revenue", ascending=False).reset_index(drop=True)

    # Style: WOD% colour + Trend colour
    def _style_wod(val):
        bg, fg = _wod_color(float(val))
        return f"background-color: {bg}; color: {fg}; font-weight:600;"

    def _style_trend(row):
        # apply colour only to Trend cell
        return [
            f"color: {row['_trend_color']}; font-weight:600;" if col == "Trend" else ""
            for col in df.columns
        ]

    display_df = df.drop(columns=["_trend_color"])
    styled = (
        display_df.style
        .format({
            "Revenue":  format_inr,
            "WOD %":    "{:.1f}%",
            "Universe": "{:,}",
            "Billed":   "{:,}",
            "Cases":    "{:,.2f}",
        })
        .map(_style_wod, subset=["WOD %"])
        .apply(
            lambda r: [
                f"color: {df.loc[r.name, '_trend_color']}; font-weight:600;"
                if c == "Trend" else "" for c in display_df.columns
            ],
            axis=1,
        )
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)
    return display_df


def _render_top_brands(start: date, end: date, cfg: dict) -> pd.DataFrame:
    """Section 4 — Top brands by revenue for this principal."""
    st.markdown("### Top Brands")
    st.caption(f"{cfg['color']!s} · revenue across selected period")

    brands_df = _load_principal_brands(cfg["company_id"], start, end)
    if brands_df.empty:
        st.info("No brand data for this principal in the selected period.")
        return brands_df

    total = brands_df["Revenue"].sum()
    top = brands_df.head(15).copy()
    top["RevCr"] = top["Revenue"] / 1e7
    top["Pct"]   = (top["Revenue"] / total * 100).round(1)

    col_chart, col_table = st.columns([1.4, 1])

    with col_chart:
        fig = go.Figure(go.Bar(
            y=top["BrandName"][::-1],
            x=top["RevCr"][::-1],
            orientation="h",
            marker_color=cfg["color"],
            text=[f"₹{v:.2f} Cr" for v in top["RevCr"][::-1]],
            textposition="outside",
            customdata=top["Revenue"][::-1].apply(format_inr),
            hovertemplate="<b>%{y}</b><br>%{customdata}<extra></extra>",
        ))
        fig.update_layout(
            **_LAYOUT,
            height=max(360, len(top) * 28),
            xaxis=dict(title="Revenue (₹ Cr)", ticksuffix=" Cr", **_GRID),
            yaxis=dict(**_GRID),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_table:
        tbl = top[["BrandName", "Cases", "Revenue", "Pct"]].rename(columns={
            "BrandName": "Brand",
            "Pct":       "% of Principal",
        })
        styled = tbl.style.format({
            "Revenue":         format_inr,
            "Cases":           "{:,.2f}",
            "% of Principal":  "{:.1f}%",
        })
        st.dataframe(styled, use_container_width=True, hide_index=True)

    return top


def _render_outlet_intel(principal_master: pd.DataFrame,
                         principal_uni: frozenset,
                         current_month: str,
                         previous_month: str,
                         cfg: dict) -> dict[str, pd.DataFrame]:
    """Section 5 — Loyal / Lapsed / Opportunity outlet tables."""
    st.markdown("### Outlet Intelligence")

    months_3 = _last_n_month_strs(3)
    months_6 = _last_n_month_strs(6)

    # Pre-compute per-month party sets (within universe)
    parties_by_month = {
        m: frozenset(
            principal_master[principal_master["BillMonth"] == m]["PartyID"]
        ) & principal_uni
        for m in months_6
    }

    # ── Loyal: billed in ALL of last 3 months ──
    loyal_ids = parties_by_month[months_3[0]] \
                & parties_by_month[months_3[1]] \
                & parties_by_month[months_3[2]]
    loyal_df = (
        principal_master[
            principal_master["BillMonth"].isin(months_3)
            & principal_master["PartyID"].isin(loyal_ids)
        ]
        .groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
        .agg(Cases=("Cases", "sum"), Revenue=("Revenue", "sum"))
    )
    loyal_df["Channel"] = loyal_df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")

    # ── Lapsed: billed previous month but NOT current month ──
    lapsed_ids = parties_by_month[previous_month] - parties_by_month[current_month]
    prev_mo_df = (
        principal_master[
            (principal_master["BillMonth"] == previous_month)
            & principal_master["PartyID"].isin(lapsed_ids)
        ]
        .groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
        .agg(LastBillDate=("LastBillDate", "max"),
             LastRevenue=("Revenue", "sum"))
    )
    prev_mo_df["Channel"]    = prev_mo_df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    today_ts = pd.Timestamp.today().normalize()
    prev_mo_df["Days Since"] = (today_ts - prev_mo_df["LastBillDate"]).dt.days.fillna(0).astype(int)

    # ── Opportunity: 0 or 1 bills in last 6 months ──
    bills_per_party = (
        principal_master[principal_master["BillMonth"].isin(months_6)]
        .groupby("PartyID")["BillMonth"].nunique()
    )
    active_parties = set(bills_per_party[bills_per_party > 1].index)
    opp_ids = principal_uni - active_parties
    # build display rows using whatever master rows exist
    opp_master = principal_master[principal_master["PartyID"].isin(opp_ids)]
    opp_grp = (
        opp_master.groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
        .agg(LastBillDate=("LastBillDate", "max"),
             LastAmount=("Revenue", "max"))
    )
    opp_grp["Channel"] = opp_grp["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    # outlets in universe with zero billing won't appear in opp_grp — add them
    missing_ids = opp_ids - set(opp_grp["PartyID"])
    if missing_ids:
        opp_extra = pd.DataFrame([
            {"PartyID": pid, "PartyName": "—", "LicenseTypeID": "",
             "LastBillDate": pd.NaT, "LastAmount": 0.0, "Channel": "—"}
            for pid in missing_ids
        ])
        opp_grp = pd.concat([opp_grp, opp_extra], ignore_index=True)

    # ── Render three columns ──
    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.markdown(f"#### 🟢 Loyal Outlets  ·  {len(loyal_ids)}")
        st.caption("Billed in all of last 3 months — your strongholds. Protect these.")
        if loyal_df.empty:
            st.info("No outlets matched the loyalty criterion.")
        else:
            tbl = loyal_df.sort_values("Revenue", ascending=False).head(10)[
                ["PartyName", "Channel", "Cases", "Revenue"]
            ].rename(columns={"PartyName": "Party", "Revenue": "Revenue (3mo)",
                              "Cases": "Cases (3mo)"})
            st.dataframe(
                tbl.style.format({"Revenue (3mo)": format_inr, "Cases (3mo)": "{:,.2f}"}),
                use_container_width=True, hide_index=True,
            )

    with col_b:
        st.markdown(f"#### 🔴 Lapsed Outlets  ·  {len(lapsed_ids)}")
        st.caption("Billed previous month, not this month — immediate visit needed.")
        if prev_mo_df.empty:
            st.success("No lapsed outlets this month.")
        else:
            tbl = prev_mo_df.sort_values("LastRevenue", ascending=False).head(15)[
                ["PartyName", "Channel", "LastBillDate", "Days Since", "LastRevenue"]
            ].rename(columns={
                "PartyName":     "Party",
                "LastBillDate":  "Last Bill",
                "LastRevenue":   "Last Month Rev",
            })
            tbl["Last Bill"] = pd.to_datetime(tbl["Last Bill"]).dt.strftime("%d %b %Y")
            st.dataframe(
                tbl.style.format({"Last Month Rev": format_inr}),
                use_container_width=True, hide_index=True,
            )
            csv_bytes = tbl.to_csv(index=False).encode()
            st.download_button(
                "Download lapsed list (CSV)",
                csv_bytes,
                file_name=f"lapsed_{cfg['company_id']}_{current_month}.csv",
                mime="text/csv",
            )

    with col_c:
        st.markdown(f"#### 🟡 Opportunity Outlets  ·  {len(opp_ids)}")
        st.caption("0–1 bills in last 6 months — win these back.")
        if opp_grp.empty:
            st.info("No opportunity outlets in this universe.")
        else:
            tbl = opp_grp.sort_values("LastAmount", ascending=False).head(15)[
                ["PartyName", "Channel", "LastBillDate", "LastAmount"]
            ].rename(columns={
                "PartyName":    "Party",
                "LastBillDate": "Last Bill",
                "LastAmount":   "Last Amount",
            })
            tbl["Last Bill"] = pd.to_datetime(tbl["Last Bill"], errors="coerce").dt.strftime("%d %b %Y").fillna("Never")
            st.dataframe(
                tbl.style.format({"Last Amount": format_inr}),
                use_container_width=True, hide_index=True,
            )

    return {"loyal": loyal_df, "lapsed": prev_mo_df, "opportunity": opp_grp}


def _render_trends(principal_master: pd.DataFrame,
                   months_12: list[str],
                   cfg: dict) -> None:
    """Section 6 — Two charts: 12-month revenue trend by team + channel mix donut."""
    st.markdown("### Revenue Trend & Channel Mix")

    # ── LEFT: Monthly revenue by team ──
    sm_to_team = {sm: c["team"] for sm, c in SALESMAN_MAP.items()}

    # We need per-party→salesman→team mapping. Since master is at
    # (BillMonth, PartyID, CompanyID) level, attribute each party to whichever
    # salesman in this principal's roster has it in their universe. For the
    # team-trend chart, pick the FIRST salesman (in cfg ordering) that claims
    # the party — keeps revenue counted once per outlet/month.
    company_id = cfg["company_id"]

    # Lazy: re-build per-principal universes locally so we can attribute
    uni_df = _load_active_universe()
    _, by_p = _build_universes(uni_df)

    # PartyID → team (first salesman in cfg["salesmen"] who owns it)
    party_team: dict[str, str] = {}
    for sm in cfg["salesmen"]:
        uni = by_p.get(sm, {}).get(company_id, frozenset())
        team = sm_to_team.get(sm, "Other")
        for pid in uni:
            party_team.setdefault(pid, team)

    pm = principal_master.copy()
    pm["Team"] = pm["PartyID"].map(party_team).fillna("Other")
    pm = pm[pm["BillMonth"].isin(months_12)]

    trend = pm.groupby(["BillMonth", "Team"], as_index=False)["Revenue"].sum()
    trend["RevCr"]    = trend["Revenue"] / 1e7
    trend["MonthLbl"] = trend["BillMonth"].map(_fmt_month)

    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown("#### 12-Month Revenue by Team")
        if trend.empty:
            st.info("No revenue rows in last 12 months.")
        else:
            fig = px.line(
                trend, x="MonthLbl", y="RevCr", color="Team",
                markers=True, labels={"RevCr": "Revenue (₹ Cr)", "MonthLbl": ""},
            )
            fig.update_layout(
                **_LAYOUT,
                xaxis=dict(**_GRID),
                yaxis=dict(ticksuffix=" Cr", **_GRID),
                legend=dict(font=dict(size=10)),
                height=380,
            )
            fig.update_traces(line_width=2.5, marker_size=4)
            st.plotly_chart(fig, use_container_width=True)

    with col_r:
        st.markdown("#### Channel Mix")
        ch_df = (
            principal_master.assign(
                Channel=principal_master["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
            )
            .groupby("Channel", as_index=False)["Revenue"].sum()
            .sort_values("Revenue", ascending=False)
        )
        if ch_df.empty:
            st.info("No revenue rows for channel mix.")
        else:
            total = ch_df["Revenue"].sum()
            labels = [
                f"{r.Channel}  ({r.Revenue / total * 100:.1f}%)"
                for r in ch_df.itertuples()
            ]
            fig = go.Figure(go.Pie(
                labels=labels,
                values=ch_df["Revenue"],
                hole=0.55,
                textinfo="none",
                marker=dict(colors=px.colors.qualitative.Bold[:len(ch_df)]),
                hovertemplate="<b>%{label}</b><br>₹%{value:,.0f}<extra></extra>",
            ))
            fig.update_layout(
                **{k: v for k, v in _LAYOUT.items() if k != "margin"},
                margin=dict(l=16, r=16, t=36, b=16),
                legend=dict(orientation="v", font=dict(size=11), bgcolor="rgba(0,0,0,0)"),
                annotations=[dict(
                    text="Channel<br>Mix",
                    x=0.5, y=0.5, font=dict(size=13, color="#1a1a1a"),
                    showarrow=False,
                )],
                height=380,
            )
            st.plotly_chart(fig, use_container_width=True)


def _render_export(salesman_df: pd.DataFrame,
                   brands_df: pd.DataFrame,
                   outlet_tables: dict[str, pd.DataFrame],
                   selected_principal: str) -> None:
    """Section 7 — Excel pack download with all tables."""
    st.markdown("### Export")
    st.caption("Download every table on this page as a multi-sheet Excel workbook.")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if salesman_df is not None and not salesman_df.empty:
            salesman_df.to_excel(writer, sheet_name="Salesman", index=False)
        if brands_df is not None and not brands_df.empty:
            brands_df.to_excel(writer, sheet_name="Top Brands", index=False)
        for label, df in (outlet_tables or {}).items():
            if df is not None and not df.empty:
                df.to_excel(writer, sheet_name=label.capitalize()[:31], index=False)
    buf.seek(0)

    safe_name = selected_principal.replace(" ", "_").replace("-", "_")
    st.download_button(
        "📥 Download Meeting Pack (Excel)",
        buf,
        file_name=f"meeting_pack_{safe_name}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.caption("Tip — take screenshots of the charts for slide decks.")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# YTD performance (July → June FY for USL / Diageo)
# ═══════════════════════════════════════════════════════════════════════════════

def _fy_jul_jun_bounds(today: date) -> tuple[date, date, date]:
    """USL / Diageo financial year starts 1 July.
    Returns (cur_fy_start, lytd_fy_start, lytd_today)."""
    if today.month >= 7:
        cur_fy_start = date(today.year, 7, 1)
    else:
        cur_fy_start = date(today.year - 1, 7, 1)
    lytd_fy_start = date(cur_fy_start.year - 1, 7, 1)
    try:
        lytd_today = date(today.year - 1, today.month, today.day)
    except ValueError:
        # Feb 29 last year → Feb 28
        lytd_today = date(today.year - 1, today.month, 28)
    return cur_fy_start, lytd_fy_start, lytd_today


def _normalize_size(s: str) -> str:
    """'650 ML' / '650ML' / '650' → '650ML' for clean size groupings."""
    if not isinstance(s, str):
        return "Other"
    s = re.sub(r"\s+", "", s.upper().strip())
    if not s:
        return "Other"
    if s.isdigit():
        return f"{s}ML"
    return s


def _channel_for_principal(cid: str, ac3: str, cls: str) -> str:
    """Mirror src/segments._channel_for so the YTD channel split matches
    the Sales Plan & Segment Analysis pages exactly."""
    ac3 = (ac3 or "").strip()
    cls = (cls or "").strip()
    if cid == "C00039":                       # United Breweries — Type III
        return {
            "130001": "KW Beer", "130004": "KW Institution",
            "130006": "KW Institution", "130002": "PCMC Institution",
            "130007": "Cross Supply",
        }.get(ac3, "Other")
    if cid == "C00056" and ac3 == "130004":   # BF KW Institution
        return "KW Institution"
    return {                                  # USL / Diageo / BF retail — Class
        "060021": "MOP", "060004": "POP",
        "060020": "Retail", "060022": "One Day",
    }.get(cls, "Other")


@st.cache_data(ttl=900, show_spinner=False)
def _load_ytd_brand_size_channel(company_id: str,
                                 months_back: int = 27) -> pd.DataFrame:
    """Per (BrandName, LiquorSize, ClassID, AcType3ID, yyyy-MM) cases for
    one principal. 27-month look-back covers current YTD + last YTD with
    a small buffer. Uses the same MIS-consistent basis as the rest of the
    dashboard (keg-aware cases, FY-dedup, free goods included)."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            b.BrandName,
            ISNULL(im.LiquorSize, '')        AS LiquorSize,
            ISNULL(p.ClassID,    '')         AS ClassID,
            ISNULL(p.AcType3ID,  '')         AS AcType3ID,
            FORMAT(h.VoucherDate, 'yyyy-MM') AS Mon,
            SUM({_CASES})                    AS Cases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID AND vi.VoucherNo = h.VoucherNo
            AND vi.ItemID LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN MsItemMaster  im ON im.ItemID  = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
        JOIN (
            SELECT TransTypeID, VoucherNo, FinancialYear, PartyID FROM (
                SELECT TransTypeID, VoucherNo, FinancialYear, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo, FinancialYear
                                          ORDER BY id_key DESC) rn
                FROM TrVocDetail
                WHERE PartyID LIKE 'D%' AND DrCrIndicator = 'D'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
           AND d.FinancialYear = h.FinancialYear
        JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        WHERE b.CompanyID = ?
          AND h.TransTypeID IN ({type_ph}) AND h.Cancelled = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY b.BrandName, im.LiquorSize, p.ClassID, p.AcType3ID,
                 FORMAT(h.VoucherDate, 'yyyy-MM')
    """
    df = run_query(sql, (company_id,))
    if not df.empty:
        df["Cases"] = pd.to_numeric(df["Cases"], errors="coerce").fillna(0.0)
    return df


def _growth_color(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 10:  return "color:#16a34a;font-weight:700"
    if v >= 0:   return "color:#16a34a;font-weight:600"
    if v >= -10: return "color:#b45309;font-weight:600"
    return "color:#dc2626;font-weight:700"


def _build_compare_table(ytd_cases: pd.Series,
                         lytd_cases: pd.Series,
                         name_col: str) -> pd.DataFrame:
    """Combine YTD + LYTD series (indexed by the same key) into a single
    sortable table with Growth % and Delta columns."""
    tab = (pd.concat([ytd_cases.rename("YTD"), lytd_cases.rename("LYTD")],
                     axis=1).fillna(0.0))
    tab["Delta"]   = tab["YTD"] - tab["LYTD"]
    tab["Growth %"] = tab.apply(
        lambda r: (r["Delta"] / r["LYTD"] * 100) if r["LYTD"] > 0
                  else (100.0 if r["YTD"] > 0 else 0.0), axis=1)
    tab = tab[(tab["YTD"] > 0) | (tab["LYTD"] > 0)]
    tab = tab.sort_values("YTD", ascending=False).reset_index()
    if name_col not in tab.columns:
        tab = tab.rename(columns={tab.columns[0]: name_col})
    return tab


def _render_ytd_performance(principal_master: pd.DataFrame,
                            cfg: dict, selected_name: str,
                            today: date) -> None:
    st.markdown("### 📊 YTD performance — growth vs same period last year")
    cid = cfg["company_id"]
    cur_start, lytd_start, lytd_today = _fy_jul_jun_bounds(today)

    with st.spinner("Loading YTD data…"):
        raw = _load_ytd_brand_size_channel(cid)
    if raw.empty:
        st.info("No YTD data available for this principal.")
        return

    raw["Channel"] = raw.apply(
        lambda r: _channel_for_principal(cid, r["AcType3ID"], r["ClassID"]),
        axis=1)
    raw["Size"] = raw["LiquorSize"].apply(_normalize_size)
    # Collapse MRP-recoded brand variants — same brand gets new BrandID when
    # MRP changes (e.g. "JOHNNIE WALKER RED LABEL" + "JOHNNIE WALKER RED
    # LABEL NEW"). Without this, YTD/LYTD growth math is split across the
    # variants and the same consumer brand appears twice in the drill-down.
    raw["BrandDisplay"] = raw["BrandName"].apply(_normalize_brand_display)

    # ── Data-availability clipping ────────────────────────────────────────
    # Teknik's TrVocItem only retains the current + previous Indian FY
    # (Apr-Mar). At the start of an Indian FY (April), the prior calendar
    # year's data drops off. For the Diageo/USL July-Jun FY, this means
    # July-Mar of "LYTD" may simply not exist in the DB — comparing 12
    # months of current FY to 3 months of LYTD inflates growth into
    # nonsense (+200% to +900%).
    #
    # Fix: clip BOTH periods to the same calendar-month window using the
    # earliest data point that falls inside the LYTD window. Comparison
    # stays apples-to-apples; we just shrink the window honestly.
    earliest_mon = str(raw["Mon"].min())  # 'YYYY-MM'
    full_lytd_months = _months_in_range(lytd_start, lytd_today)
    avail_lytd_months = [m for m in full_lytd_months if m >= earliest_mon]
    n_avail = len(avail_lytd_months)
    full_n  = len(full_lytd_months)
    if n_avail == 0:
        st.warning(
            f"No data available for last FY ({lytd_start:%b-%Y} → "
            f"{lytd_today:%b-%Y}). The ERP only retains the current + "
            f"previous Indian FY (Apr-Mar), so an apples-to-apples July-June "
            f"comparison isn't possible right now. YTD section disabled.")
        return
    # Mirror the available LYTD window into current FY (same calendar months
    # one year later) so we compare like-for-like.
    cur_months  = [f"{int(m[:4])+1:04d}-{m[5:]}" for m in avail_lytd_months]
    lytd_months = avail_lytd_months
    # Cap current FY window to actual data window (no future months & no
    # months past today)
    cur_today_str = today.strftime("%Y-%m")
    cur_months = [m for m in cur_months if m <= cur_today_str]
    lytd_months = lytd_months[:len(cur_months)]

    clip_start = date(int(lytd_months[0][:4]), int(lytd_months[0][5:]), 1)
    clip_end_l = lytd_today
    clip_start_c = date(int(cur_months[0][:4]), int(cur_months[0][5:]), 1)
    clip_end_c = today

    if n_avail < full_n:
        st.caption(
            f"⚠️ **Comparison clipped to data available** — Teknik's ERP "
            f"only retains the current + previous Indian FY, so July-Mar "
            f"of last FY is unavailable. Showing "
            f"**{clip_start_c:%d-%b-%Y} → {clip_end_c:%d-%b-%Y}** vs "
            f"**{clip_start:%d-%b-%Y} → {clip_end_l:%d-%b-%Y}** "
            f"({len(cur_months)} months instead of 12) for a fair "
            f"apples-to-apples comparison.")
    else:
        st.caption(
            f"**FY July → June** (USL / Diageo standard). YTD = "
            f"**{clip_start_c:%d-%b-%Y}** → **{clip_end_c:%d-%b-%Y}**. "
            f"LYTD = **{clip_start:%d-%b-%Y}** → "
            f"**{clip_end_l:%d-%b-%Y}** for an apples-to-apples comparison.")

    ytd_df  = raw[raw["Mon"].isin(cur_months)]
    lytd_df = raw[raw["Mon"].isin(lytd_months)]

    ytd_total  = float(ytd_df["Cases"].sum())
    lytd_total = float(lytd_df["Cases"].sum())
    gr_total   = ((ytd_total - lytd_total) / lytd_total * 100) if lytd_total > 0 else 0.0

    # ── Overall summary ──
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"YTD ({clip_start_c.strftime('%b-%y')} → {today.strftime('%b-%y')})",
              f"{ytd_total:,.0f} cs")
    k2.metric(f"LYTD ({clip_start.strftime('%b-%y')} → {lytd_today.strftime('%b-%y')})",
              f"{lytd_total:,.0f} cs")
    k3.metric("Growth %", f"{gr_total:+.1f}%",
              delta=f"{ytd_total - lytd_total:+,.0f} cs")
    k4.metric("Months covered", f"{len(cur_months)} / {12 if n_avail == full_n else full_n} avail")

    # ── Monthly volume comparison chart ──
    # Render ONLY the months we have data for in both periods (the clipped
    # window). Drawing the full 12-month FY here would inflate "Current FY"
    # bars against empty "Last FY" months and recreate the same visual lie
    # that the totals fix above eliminates.
    by_mon = raw.groupby("Mon")["Cases"].sum().to_dict()
    chart_rows = []
    for cm, lm in zip(cur_months, lytd_months):
        label = date(int(cm[:4]), int(cm[5:]), 1).strftime("%b")
        cur_v = float(by_mon.get(cm, 0))
        ly_v  = float(by_mon.get(lm, 0))
        chart_rows.append({"Month": label, "Period": "Current FY", "Cases": cur_v})
        chart_rows.append({"Month": label, "Period": "Last FY",   "Cases": ly_v})
    chart_df = pd.DataFrame(chart_rows)
    pcolor = {"Current FY": cfg["color"], "Last FY": "#9CA3AF"}
    fig = px.bar(chart_df, x="Month", y="Cases", color="Period",
                 barmode="group", color_discrete_map=pcolor,
                 text="Cases")
    fig.update_traces(texttemplate="%{text:,.0f}",
                      textposition="outside", cliponaxis=False)
    fig.update_layout(
        **_LAYOUT, height=340,
        title=f"Monthly volume — Current FY vs Last FY ({selected_name})",
        xaxis=dict(**_GRID, categoryorder="array",
                   categoryarray=[date(int(m[:4]), int(m[5:]), 1).strftime("%b")
                                  for m in cur_months]),
        yaxis=dict(**_GRID, title="Cases"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        bargap=0.25,
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Brand-wise table ──
    st.markdown("##### Brand-wise YTD")
    brand_tab = _build_compare_table(
        ytd_df.groupby("BrandDisplay")["Cases"].sum(),
        lytd_df.groupby("BrandDisplay")["Cases"].sum(),
        "Brand")
    st.dataframe(
        brand_tab.style.format({
            "YTD": "{:,.0f}", "LYTD": "{:,.0f}",
            "Delta": "{:+,.0f}", "Growth %": "{:+.1f}%",
        }).map(_growth_color, subset=["Growth %"]),
        use_container_width=True, hide_index=True, height=380)

    # ── Channel-wise table ──
    st.markdown("##### Channel-wise YTD")
    chan_tab = _build_compare_table(
        ytd_df.groupby("Channel")["Cases"].sum(),
        lytd_df.groupby("Channel")["Cases"].sum(),
        "Channel")
    st.dataframe(
        chan_tab.style.format({
            "YTD": "{:,.0f}", "LYTD": "{:,.0f}",
            "Delta": "{:+,.0f}", "Growth %": "{:+.1f}%",
        }).map(_growth_color, subset=["Growth %"]),
        use_container_width=True, hide_index=True)

    # ── Brand drill-down — size split for a chosen brand ──
    st.markdown("##### Drill-down — pick a brand → see size split")
    options = brand_tab["Brand"].tolist()
    if not options:
        return
    pick = st.selectbox(
        "Brand", options, key=f"ytd_brand_pick_{cid}",
        help="Pick any brand to see which pack-size is driving its growth "
             "or degrowth.")
    bd_ytd  = ytd_df[ytd_df["BrandDisplay"]  == pick]
    bd_lytd = lytd_df[lytd_df["BrandDisplay"] == pick]
    size_tab = _build_compare_table(
        bd_ytd.groupby("Size")["Cases"].sum(),
        bd_lytd.groupby("Size")["Cases"].sum(),
        "Size")
    if size_tab.empty:
        st.caption(f"No size breakdown available for {pick}.")
        return
    st.dataframe(
        size_tab.style.format({
            "YTD": "{:,.0f}", "LYTD": "{:,.0f}",
            "Delta": "{:+,.0f}", "Growth %": "{:+.1f}%",
        }).map(_growth_color, subset=["Growth %"]),
        use_container_width=True, hide_index=True)
    # Also show channel split for the chosen brand
    chan_b = _build_compare_table(
        bd_ytd.groupby("Channel")["Cases"].sum(),
        bd_lytd.groupby("Channel")["Cases"].sum(),
        "Channel")
    if not chan_b.empty:
        st.markdown(f"**Channel split — {pick}**")
        st.dataframe(
            chan_b.style.format({
                "YTD": "{:,.0f}", "LYTD": "{:,.0f}",
                "Delta": "{:+,.0f}", "Growth %": "{:+.1f}%",
            }).map(_growth_color, subset=["Growth %"]),
            use_container_width=True, hide_index=True)


def render() -> None:
    st.title("Principal Meeting Pack")
    st.caption("Principal-wise performance for company review meetings")
    st.divider()

    # ── Principal selector ──
    selected = st.radio(
        "Principal",
        options=list(PRINCIPAL_CONFIG.keys()),
        horizontal=True,
        label_visibility="collapsed",
        key="principal_select",
    )
    cfg = PRINCIPAL_CONFIG[selected]

    # ── Date range ──
    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    # From / To + presets — see src/purchase.py for why we split range.
    _apply_date_preset("principal", today, fy_start)
    c_from, c_to, _ = st.columns([1, 1, 2])
    with c_from:
        start = st.date_input(
            "From",
            min_value=date(2020, 1, 1), max_value=today,
            format="DD-MM-YYYY", key="principal_start")
    with c_to:
        end = st.date_input(
            "To",
            min_value=date(2020, 1, 1), max_value=today,
            format="DD-MM-YYYY", key="principal_end")

    if start > end:
        st.warning("Start date must be before end date.")
        return

    # ── Load shared data ──
    with st.spinner("Loading principal data…"):
        master = _load_master(13)
        uni_df = _load_active_universe()

    if master.empty:
        st.error("No billing data returned. Check DB connection.")
        return
    if uni_df.empty:
        st.error("No active-universe data returned. Check DB connection.")
        return

    _, by_principal = _build_universes(uni_df)

    # Build principal-level universe (union of salesmen's per-principal universes)
    company_id = cfg["company_id"]
    p_uni_set: set = set()
    for sm in cfg["salesmen"]:
        if sm in by_principal and company_id in by_principal[sm]:
            p_uni_set |= by_principal[sm][company_id]
    principal_uni = frozenset(p_uni_set)

    if not principal_uni:
        st.warning(f"No active outlets in universe for {selected}.")
        return

    principal_master = master[
        (master["CompanyID"] == company_id)
        & (master["PartyID"].isin(principal_uni))
    ].copy()

    current_month  = _last_complete_month()
    previous_month = _prev_month_str(current_month)
    months_12      = _last_n_month_strs(12)

    # ── Sections ──
    _render_kpis(principal_master, principal_uni, start, end, current_month, cfg)
    st.divider()

    _render_wod_grid(principal_master, principal_uni, months_12, cfg)
    st.divider()

    salesman_df = _render_salesman_table(
        master, by_principal, current_month, months_12, cfg,
    )
    st.divider()

    brands_df = _render_top_brands(start, end, cfg)
    st.divider()

    outlet_tables = _render_outlet_intel(
        principal_master, principal_uni, current_month, previous_month, cfg,
    )
    st.divider()

    # Parties pulling us down — uses sales.py's reusable section,
    # pre-filtered to this principal's universe (we pass the
    # CompanyID into the data loader so the section sees only the
    # principal's billings).
    from src.sales import (
        _load_party_daily_revenue,
        _section_parties_pulling_down,
    )
    with st.spinner("Loading party-level history for pull-down analysis…"):
        party_daily = _load_party_daily_revenue(
            months=14, principal_ids=(company_id,),
        )
    _section_parties_pulling_down(
        party_daily, section_key=f"principal_{company_id}",
    )
    st.divider()

    _render_trends(principal_master, months_12, cfg)
    st.divider()

    _render_ytd_performance(principal_master, cfg, selected, today)
    st.divider()

    _render_export(salesman_df, brands_df, outlet_tables, selected)
