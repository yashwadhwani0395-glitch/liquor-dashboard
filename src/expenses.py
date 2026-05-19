"""src/expenses.py — Operating Expenses page.

P&L-style view of the indirect-expense bucket. Pulls every GL row
under `MsAccountHead.MainHeadID = '010004'` (the indirect-expense
classification in this Teknik ERP) joined with TrVocHead for
VoucherDate / Cancelled status.

Why MainHeadID='010004' (and not MainHeadName)
----------------------------------------------
The Teknik ERP's MsAccountHead has only ID columns — there is NO
MainHeadName or GroupName column. Confirmed in
`_discover_expenses.out`. Account heads carry MainHeadID and
SubHeadID, both opaque char codes. From sample-name inspection:

    010001  → Assets (Sundry Debtors, banks, fixed assets …)
    010004  → Indirect Expenses    ← THIS module's scope
    010005  → Other Income
    010007  → Liabilities
    010008  → System (Cancelled Vouchers)
    010010  → Sales-side / Direct movements
    010011  → Direct Cost (Excise Duty, Opening Stock)
    010012  → Purchases - Trading (COGS)

The "Expenses" tab focuses on 010004 = operating overhead. Excise
duty (010011) and purchases (010012) live in their own tabs.

Categories
----------
SubHeadID is too coarse — 36 of 72 expense accounts collapse into
the single sub-head 020011. We hand-roll a CATEGORY_MAP that
rolls AccName patterns into P&L-style categories. Inspect this
mapping when KWPL adds new GL accounts.

Cache TTL is 3600s and the loader RAISES on empty (same defense-
in-depth as src/debtors.py post-30e39ac) so transient DB blips
don't poison the cache for an hour.
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import safe_section

try:
    import plotly.express as px
    _HAS_PLOTLY = True
except Exception:
    _HAS_PLOTLY = False


# ── Constants (from ERP discovery) ──────────────────────────────────────────
EXPENSE_MAIN_HEAD_ID = "010004"

# Sales TransTypes (same list as src/sales.py / sales_plan.py) for the
# "% of revenue" denominator.
SALES_TT: tuple[int, ...] = (
    18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53,
)

# Hand-rolled category mapper. Tested against every AccHeadID currently
# present under MainHeadID=010004 in `_discover_expenses.out`. New
# accounts will fall through to "Miscellaneous" — periodically audit
# that bucket and promote frequent items to a real category.
CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "Salaries & Wages":      (
        "salary", "wages", "bonus", "pf", "esic", "gratuity",
        "incentive", "staff welfare", "director's rem", "director rem",
    ),
    "Rent & Lease":          ("rent", "lease"),
    "Marketing & BTL":       (
        "scheme", "promotion", "promo",
        "pramotion",          # known KWPL spelling on AccHeadID 000507
        "advertis", "marketing",
        "btl", "brand", "cards", "display", "sales commiss",
        "trade discount",     # AccHeadID 000125 — discounts given to trade
    ),
    "Repairs & Maintenance": ("repair", "maintain", "maint", "amc"),
    "Travel & Conveyance":   (
        "travel", "conveyance", "petrol", "diesel", "ev charg",
        "fuel", "tour",
    ),
    "Bank & Finance":        (
        "bank interest", "bank charges", "interest", "intrest",
        "processing charges", "loan processing", "bank commission",
    ),
    "Excise & Statutory":    (
        "excise", "licen", "tcs", "tds", "permit", "duty",
        "warai", "p.m.c.tax", "pmc tax", "professional tax",
        "prof tax", "gst", "fine to", "court fee", "filing",
    ),
    "Godown Operations":     (
        "godown exp", "godown rent",  # rent handled above; non-rent godown here
        "storage", "warehouse", "loading", "unloading", "hamali",
        "breakage", "brakage", "checking", "restak", "transport ",
        "labour", "security",
    ),
    "Office & Admin":        (
        "printing", "stationery", "postage", "telephone", "courier",
        "office exp", "office rent",  # caught by Rent but listed for clarity
        "misc", "membership", "entert", "visitors", "tea",
    ),
    "Power & Fuel":          ("electricity", "power"),
    "Insurance":             ("insurance", "premium"),
    "Depreciation":          ("depreciation", "amortization"),
    "Professional Fees":     (
        "professional fee", "audit", "consultancy", "legal", " ca ",
    ),
    "Donations":             ("donation",),
}

# Sub-bucket order for chart legibility (otherwise plotly picks
# alphabetical, which scrambles the visual story).
CATEGORY_ORDER = (
    "Salaries & Wages",
    "Marketing & BTL",
    "Repairs & Maintenance",
    "Rent & Lease",
    "Excise & Statutory",
    "Travel & Conveyance",
    "Bank & Finance",
    "Godown Operations",
    "Office & Admin",
    "Power & Fuel",
    "Insurance",
    "Professional Fees",
    "Depreciation",
    "Donations",
    "Miscellaneous",
)


def _map_to_category(acc_name: str) -> str:
    """Find the P&L category for a single AccName (case-insensitive)."""
    if not acc_name:
        return "Miscellaneous"
    full = " " + str(acc_name).lower() + " "
    # Test in CATEGORY_ORDER so a more specific keyword wins over a
    # later catch-all (e.g. "office rent" → Rent, not Office & Admin)
    for category in CATEGORY_ORDER:
        keywords = CATEGORY_MAP.get(category, ())
        if any(kw in full for kw in keywords):
            return category
    return "Miscellaneous"


# ═══════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_expense_ledger() -> pd.DataFrame:
    """One row per voucher × account under MainHeadID='010004',
    pre-aggregated to (AccHeadID, AccName, VoucherDate, Voucher) to
    keep the in-memory DF compact (<100k rows expected)."""
    sql = f"""
        SELECT
            a.AccHeadID,
            a.AccName,
            a.MainHeadID,
            a.SubHeadID,
            CAST(h.VoucherDate AS date)                AS VoucherDate,
            h.TransTypeID,
            h.VoucherNo,
            SUM(CASE WHEN d.DrCrIndicator='D'
                     THEN CAST(d.Amount AS float)
                     ELSE -CAST(d.Amount AS float) END) AS Amount
        FROM TrVocDetail   d
        JOIN TrVocHead     h ON h.TransTypeID = d.TransTypeID
                            AND h.VoucherNo   = d.VoucherNo
        JOIN MsAccountHead a ON a.AccHeadID  = d.AccHeadID
        WHERE h.Cancelled = 'N'
          AND a.MainHeadID = '{EXPENSE_MAIN_HEAD_ID}'
          AND h.VoucherDate >= '2024-04-01'
          AND CAST(h.VoucherDate AS date) <= CAST(GETDATE() AS date)
        GROUP BY a.AccHeadID, a.AccName, a.MainHeadID, a.SubHeadID,
                 CAST(h.VoucherDate AS date), h.TransTypeID, h.VoucherNo
        HAVING SUM(CASE WHEN d.DrCrIndicator='D'
                        THEN CAST(d.Amount AS float)
                        ELSE -CAST(d.Amount AS float) END) != 0
    """
    df = run_query(sql)
    if df.empty:
        raise RuntimeError(
            "Empty expense ledger — likely transient DB error or wrong "
            f"MainHeadID. Expected MainHeadID='{EXPENSE_MAIN_HEAD_ID}' "
            "to contain operating expenses. Click 🔄 Refresh to retry."
        )
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["Amount"]      = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
    df["Category"]    = df["AccName"].map(_map_to_category)
    df["Month"]       = df["VoucherDate"].dt.to_period("M").dt.to_timestamp()
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_revenue_for_context() -> pd.DataFrame:
    """Per-day revenue from sales TTs — denominator for '% of revenue'
    KPIs. Uses the standard FY-CASE filter to drop FY-duplicate
    TrVocItem rows."""
    tt_csv = ",".join(str(t) for t in SALES_TT)
    sql = f"""
        SELECT
            CAST(h.VoucherDate AS date) AS VoucherDate,
            SUM(CAST(vi.TotalAmount AS float)) AS Revenue
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID  = h.TransTypeID
            AND vi.VoucherNo    = h.VoucherNo
            AND vi.FreeItemYN   = 'N'
            AND vi.ItemID       LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
            END
        WHERE h.TransTypeID IN ({tt_csv})
          AND h.Cancelled = 'N'
          AND h.VoucherDate >= '2024-04-01'
          AND CAST(h.VoucherDate AS date) <= CAST(GETDATE() AS date)
        GROUP BY CAST(h.VoucherDate AS date)
    """
    df = run_query(sql)
    if df.empty:
        return pd.DataFrame({"VoucherDate": [], "Revenue": [], "Month": []})
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["Revenue"]     = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
    df["Month"]       = df["VoucherDate"].dt.to_period("M").dt.to_timestamp()
    return df


# ═══════════════════════════════════════════════════════════════════════════
# DATE HELPERS — single source of truth for "current FY" + "LY same period"
# ═══════════════════════════════════════════════════════════════════════════

def _fy_bounds(today: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """Return (fy_start, ly_start, ly_same_period_end) for the FY
    that `today` falls in. Indian FY starts April 1."""
    yr = today.year if today.month >= 4 else today.year - 1
    fy_start = pd.Timestamp(yr, 4, 1)
    ly_start = fy_start - pd.DateOffset(years=1)
    ly_same_period_end = today - pd.DateOffset(years=1)
    return fy_start, ly_start, ly_same_period_end


def _fy_label(d: pd.Timestamp) -> str:
    yr = d.year if d.month >= 4 else d.year - 1
    return f"FY{yr % 100:02d}-{(yr + 1) % 100:02d}"


# ═══════════════════════════════════════════════════════════════════════════
# UI SECTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _section_hero(exp_df: pd.DataFrame, rev_df: pd.DataFrame) -> None:
    today = pd.Timestamp.today().normalize()
    fy_start, ly_start, ly_end = _fy_bounds(today)

    ytd_exp = float(exp_df.loc[exp_df["VoucherDate"] >= fy_start, "Amount"].sum())
    ly_exp  = float(exp_df.loc[
        (exp_df["VoucherDate"] >= ly_start)
        & (exp_df["VoucherDate"] <= ly_end), "Amount"].sum())
    last30  = float(exp_df.loc[
        exp_df["VoucherDate"] >= (today - pd.Timedelta(days=30)),
        "Amount"].sum())

    ytd_rev = float(rev_df.loc[rev_df["VoucherDate"] >= fy_start, "Revenue"].sum())
    pct_rev = (ytd_exp / ytd_rev * 100) if ytd_rev > 0 else 0.0
    yoy     = ((ytd_exp - ly_exp) / ly_exp * 100) if ly_exp > 0 else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"{_fy_label(today)} YTD", f"₹{ytd_exp/1e7:.2f} Cr",
              f"Since {fy_start.strftime('%d %b %Y')}")
    c2.metric(f"vs {_fy_label(today - pd.DateOffset(years=1))} same period",
              f"₹{ly_exp/1e7:.2f} Cr",
              f"{yoy:+.1f}%",
              delta_color="inverse")  # higher expense = worse
    c3.metric("% of revenue (YTD)", f"{pct_rev:.2f}%",
              help="YTD expenses ÷ YTD sales revenue")
    c4.metric("Last 30 days", f"₹{last30/1e7:.2f} Cr",
              "Rolling 30-day spend")


def _section_forecast(exp_df: pd.DataFrame, rev_df: pd.DataFrame) -> None:
    st.subheader("🔮 Full-year forecast (linear projection)")
    today = pd.Timestamp.today().normalize()
    fy_start, ly_start, _ = _fy_bounds(today)
    fy_end = fy_start + pd.DateOffset(years=1) - pd.Timedelta(days=1)
    days_elapsed = max((today - fy_start).days, 1)
    days_total   = (fy_end - fy_start).days + 1
    pct_elapsed  = days_elapsed / days_total * 100

    ytd_exp     = float(exp_df.loc[exp_df["VoucherDate"] >= fy_start, "Amount"].sum())
    annual_exp  = ytd_exp / days_elapsed * days_total

    ly_total = float(exp_df.loc[
        (exp_df["VoucherDate"] >= ly_start)
        & (exp_df["VoucherDate"] < fy_start),
        "Amount"].sum())

    ytd_rev    = float(rev_df.loc[rev_df["VoucherDate"] >= fy_start, "Revenue"].sum())
    annual_rev = ytd_rev / days_elapsed * days_total if days_elapsed else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("YTD spent", f"₹{ytd_exp/1e7:.2f} Cr",
              f"{pct_elapsed:.0f}% of year done")
    c2.metric("Annualized", f"₹{annual_exp/1e7:.2f} Cr",
              help="ytd × 365 ÷ days_elapsed. Simple linear projection. "
                   "Doesn't account for year-end booking spikes — Mar of "
                   "the prior FY often sees 3-4× the monthly average.")
    if ly_total > 0:
        c3.metric(f"vs {_fy_label(today - pd.DateOffset(years=1))} actual",
                  f"₹{ly_total/1e7:.2f} Cr",
                  f"{((annual_exp - ly_total)/ly_total*100):+.1f}%",
                  delta_color="inverse")
    else:
        c3.metric("vs LY actual", "—", "(no LY data)")
    fwd_pct_rev = (annual_exp / annual_rev * 100) if annual_rev > 0 else 0.0
    c4.metric("Fwd % of revenue", f"{fwd_pct_rev:.2f}%",
              "annualized expense ÷ annualized revenue")


def _section_monthly_trend(exp_df: pd.DataFrame) -> None:
    st.subheader("📈 Monthly trend — last 12 months by category")
    today    = pd.Timestamp.today().normalize()
    start    = (today.to_period("M") - 11).to_timestamp()
    recent   = exp_df.loc[exp_df["Month"] >= start]
    if recent.empty:
        st.info("No data in the last 12 months."); return

    monthly = (
        recent.groupby(["Month", "Category"], observed=True)["Amount"]
              .sum().reset_index()
    )
    monthly["Amount_L"]    = monthly["Amount"] / 1e5
    monthly["MonthLabel"]  = monthly["Month"].dt.strftime("%b %y")

    if _HAS_PLOTLY:
        # Preserve month ordering on x-axis
        month_order = (
            monthly.sort_values("Month")["MonthLabel"].drop_duplicates().tolist()
        )
        fig = px.bar(
            monthly, x="MonthLabel", y="Amount_L", color="Category",
            category_orders={
                "MonthLabel": month_order,
                "Category":   list(CATEGORY_ORDER),
            },
            labels={"Amount_L": "₹ Lakhs", "MonthLabel": "Month"},
            template="plotly_white",
        )
        fig.update_layout(height=420, margin=dict(t=10, b=10, l=10, r=10),
                          legend=dict(orientation="h", yanchor="bottom",
                                      y=-0.45, xanchor="center", x=0.5))
        st.plotly_chart(fig, use_container_width=True)
    else:
        pivot = (monthly.pivot(index="MonthLabel", columns="Category",
                               values="Amount_L").fillna(0))
        st.bar_chart(pivot)


def _section_category_breakdown(exp_df: pd.DataFrame, rev_df: pd.DataFrame) -> None:
    st.subheader("🗂️ Category breakdown — FY YTD vs LY same period")
    today = pd.Timestamp.today().normalize()
    fy_start, ly_start, ly_end = _fy_bounds(today)

    ytd_rev = float(rev_df.loc[rev_df["VoucherDate"] >= fy_start, "Revenue"].sum())

    ytd = (exp_df.loc[exp_df["VoucherDate"] >= fy_start]
                 .groupby("Category", observed=True)["Amount"].sum())
    ly  = (exp_df.loc[(exp_df["VoucherDate"] >= ly_start)
                     & (exp_df["VoucherDate"] <= ly_end)]
                 .groupby("Category", observed=True)["Amount"].sum())

    summary = pd.DataFrame({
        "YTD": ytd,
        "LY_YTD": ly.reindex(ytd.index, fill_value=0.0),
    }).reset_index()

    summary["YoY_pct"]    = ((summary["YTD"] - summary["LY_YTD"])
                             / summary["LY_YTD"].replace(0, pd.NA) * 100)
    summary["OfRev_pct"]  = (summary["YTD"] / ytd_rev * 100) if ytd_rev > 0 else 0.0
    summary["YTD_L"]      = summary["YTD"]    / 1e5
    summary["LY_YTD_L"]   = summary["LY_YTD"] / 1e5
    summary["Share_pct"]  = (summary["YTD"] / summary["YTD"].sum() * 100
                             if summary["YTD"].sum() > 0 else 0)
    summary = summary.sort_values("YTD", ascending=False).reset_index(drop=True)

    def _yoy_color(v):
        try: n = float(v)
        except (TypeError, ValueError): return ""
        if pd.isna(n): return "color:#9ca3af"
        if n > 25:  return "color:#dc2626; font-weight:600"
        if n > 10:  return "color:#b45309; font-weight:600"
        if n < -5:  return "color:#16a34a"
        return ""

    disp = summary[[
        "Category", "YTD_L", "LY_YTD_L", "Share_pct",
        "YoY_pct", "OfRev_pct",
    ]].rename(columns={
        "YTD_L":     "YTD (₹ L)",
        "LY_YTD_L":  "LY same (₹ L)",
        "Share_pct": "Share %",
        "YoY_pct":   "YoY %",
        "OfRev_pct": "% of Rev",
    })
    styled = (
        disp.style.format({
            "YTD (₹ L)":    "₹{:.1f} L",
            "LY same (₹ L)": "₹{:.1f} L",
            "Share %":      "{:.1f}%",
            "YoY %":        "{:+.1f}%",
            "% of Rev":     "{:.2f}%",
        })
        .map(_yoy_color, subset=["YoY %"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Treemap / pie for visual share
    if _HAS_PLOTLY:
        pos = summary[summary["YTD"] > 0]
        if not pos.empty:
            fig = px.treemap(
                pos, path=["Category"], values="YTD",
                color="YTD",
                color_continuous_scale="Blues",
                custom_data=["YTD_L", "Share_pct"],
            )
            fig.update_traces(
                texttemplate="<b>%{label}</b><br>"
                             "₹%{customdata[0]:.1f} L<br>"
                             "%{customdata[1]:.1f}%",
                hovertemplate="<b>%{label}</b><br>"
                              "₹%{customdata[0]:.1f} L<br>"
                              "%{customdata[1]:.1f}% of total<extra></extra>",
            )
            fig.update_layout(template="plotly_white", height=380,
                              margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)


def _section_anomalies(exp_df: pd.DataFrame) -> None:
    st.subheader("⚠️ Anomalies & watch items")
    today = pd.Timestamp.today().normalize()
    fy_start, ly_start, ly_end = _fy_bounds(today)

    ytd = (exp_df.loc[exp_df["VoucherDate"] >= fy_start]
                 .groupby("Category", observed=True)["Amount"].sum())
    ly  = (exp_df.loc[(exp_df["VoucherDate"] >= ly_start)
                     & (exp_df["VoucherDate"] <= ly_end)]
                 .groupby("Category", observed=True)["Amount"].sum()
                 .reindex(ytd.index, fill_value=0.0))

    # YoY jumps > 25% (requires LY base > Rs1 L to avoid noise on tiny lines)
    jumps = []
    for cat, cur in ytd.items():
        base = float(ly[cat])
        if base > 1e5:
            chg = (float(cur) - base) / base * 100
            if chg > 25:
                jumps.append({
                    "Category":     cat,
                    "FY_YTD_L":     float(cur) / 1e5,
                    "LY_YTD_L":     base / 1e5,
                    "Jump_pct":     chg,
                })
    if jumps:
        st.markdown("**🔴 Year-over-year jumps > 25%** "
                    "(spending significantly more than last year's same period)")
        jdf = pd.DataFrame(jumps).sort_values("Jump_pct", ascending=False)
        styled = (
            jdf.style.format({
                "FY_YTD_L": "₹{:.1f} L",
                "LY_YTD_L": "₹{:.1f} L",
                "Jump_pct": "+{:.1f}%",
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.success("No YoY jumps > 25% on categories with material LY base.")

    # Last-month spike vs trailing 3-month avg
    last_month = today.to_period("M").to_timestamp()
    last_month_end = (last_month + pd.offsets.MonthEnd(0)).normalize()
    prev3_start = (last_month - pd.DateOffset(months=3))
    last_m = (exp_df.loc[(exp_df["Month"] == last_month)]
                 .groupby("Category", observed=True)["Amount"].sum())
    prev3  = (exp_df.loc[(exp_df["Month"] >= prev3_start)
                        & (exp_df["Month"] <  last_month)]
                 .groupby("Category", observed=True)["Amount"].sum() / 3.0)

    spikes = []
    for cat, cur in last_m.items():
        base = float(prev3.get(cat, 0.0))
        if base > 50_000:
            spike = (float(cur) - base) / base * 100
            if spike > 50:
                spikes.append({
                    "Category":         cat,
                    "ThisMonth_L":      float(cur) / 1e5,
                    "Trail3moAvg_L":    base / 1e5,
                    "Spike_pct":        spike,
                })
    if spikes:
        st.markdown(f"**🟡 {last_month.strftime('%B %Y')} spike** "
                    "(spending > 50% above trailing 3-month average)")
        sdf = pd.DataFrame(spikes).sort_values("Spike_pct", ascending=False)
        styled = (
            sdf.style.format({
                "ThisMonth_L":   "₹{:.1f} L",
                "Trail3moAvg_L": "₹{:.1f} L",
                "Spike_pct":     "+{:.1f}%",
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info(f"No month-over-month spikes > 50% in {last_month.strftime('%B %Y')}.")


def _section_account_detail(exp_df: pd.DataFrame) -> None:
    st.subheader("🔍 Account drill-down")
    today = pd.Timestamp.today().normalize()
    fy_start, _, _ = _fy_bounds(today)

    cats_present = [c for c in CATEGORY_ORDER
                    if c in set(exp_df["Category"].unique())]
    if not cats_present:
        st.info("No categories present in current data."); return

    default_idx = (cats_present.index("Salaries & Wages")
                   if "Salaries & Wages" in cats_present else 0)
    selected = st.selectbox("Category", cats_present,
                            index=default_idx, key="exp_cat_pick")

    cat_df = exp_df.loc[exp_df["Category"] == selected]
    if cat_df.empty:
        st.info(f"No entries in {selected}."); return

    # Accounts table — FY YTD
    accounts = (
        cat_df.loc[cat_df["VoucherDate"] >= fy_start]
              .groupby("AccName", observed=True)
              .agg(FY_Amount=("Amount", "sum"),
                   Vouchers=("VoucherNo", "nunique"),
                   FirstEntry=("VoucherDate", "min"),
                   LastEntry=("VoucherDate", "max"))
              .reset_index()
              .sort_values("FY_Amount", ascending=False)
    )
    if accounts.empty:
        st.info(f"No FY-YTD entries in {selected}."); return

    accounts["FY_L"] = accounts["FY_Amount"] / 1e5
    accounts["FirstEntry"] = pd.to_datetime(accounts["FirstEntry"]).dt.strftime("%d %b %Y")
    accounts["LastEntry"]  = pd.to_datetime(accounts["LastEntry"]).dt.strftime("%d %b %Y")
    disp = accounts[["AccName", "FY_L", "Vouchers", "FirstEntry", "LastEntry"]] \
        .rename(columns={"AccName":"Account", "FY_L":"FY YTD (₹ L)"})
    styled = (
        disp.style.format({
            "FY YTD (₹ L)": "₹{:.1f} L",
            "Vouchers":     "{:,}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=320)

    # Trend for this category (last 12 months)
    start = (today.to_period("M") - 11).to_timestamp()
    monthly_cat = (
        cat_df.loc[cat_df["Month"] >= start]
              .groupby("Month", observed=True)["Amount"].sum()
              .reset_index()
              .sort_values("Month")
    )
    if not monthly_cat.empty:
        monthly_cat["Amount_L"]   = monthly_cat["Amount"] / 1e5
        monthly_cat["MonthLabel"] = monthly_cat["Month"].dt.strftime("%b %y")
        if _HAS_PLOTLY:
            month_order = monthly_cat["MonthLabel"].tolist()
            fig = px.bar(
                monthly_cat, x="MonthLabel", y="Amount_L",
                category_orders={"MonthLabel": month_order},
                labels={"Amount_L": "₹ Lakhs", "MonthLabel": "Month"},
                template="plotly_white",
            )
            fig.update_layout(height=300, margin=dict(t=10, b=10, l=10, r=10),
                              title=f"{selected} — last 12 months")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.bar_chart(monthly_cat.set_index("MonthLabel")["Amount_L"])


# ═══════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("💸 Expenses")
    st.caption(
        "Operating overhead from the indirect-expense ledger "
        f"(MsAccountHead.MainHeadID = '{EXPENSE_MAIN_HEAD_ID}'). "
        "COGS / purchases / excise duty live in their own tabs."
    )

    with st.spinner("Loading expense ledger + revenue context…"):
        exp_df = _load_expense_ledger()
        rev_df = _load_revenue_for_context()

    # Telemetry to Streamlit Cloud logs
    today = pd.Timestamp.today().normalize()
    fy_start, _, _ = _fy_bounds(today)
    ytd_total = float(exp_df.loc[exp_df["VoucherDate"] >= fy_start, "Amount"].sum())
    print(
        f"[expenses] rows={len(exp_df):,} accounts={exp_df['AccHeadID'].nunique()} "
        f"categories={exp_df['Category'].nunique()} "
        f"ytd_total_cr={ytd_total/1e7:.2f}",
        file=sys.stderr,
    )

    safe_section("Hero KPIs",         _section_hero,              exp_df, rev_df)
    st.divider()
    safe_section("Forecast",          _section_forecast,          exp_df, rev_df)
    st.divider()
    safe_section("Monthly trend",     _section_monthly_trend,     exp_df)
    st.divider()
    safe_section("Category breakdown", _section_category_breakdown, exp_df, rev_df)
    st.divider()
    safe_section("Anomalies",         _section_anomalies,         exp_df)
    st.divider()
    safe_section("Account drill-down", _section_account_detail,    exp_df)
