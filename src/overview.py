"""src/overview.py — single-screen morning briefing for the owner.

Pulls hero KPIs from every other module using their already-cached
loaders, so this tab opens fast (instant if the user has already
visited Sales / Debtors / Inventory in the session). The goal is
"what do I need to know in 30 seconds" — not a deep drill-down.

Layout (top to bottom):
  1. Hero strip — Revenue / Cases / Bills / Active outlets MTD,
     each with ∆ vs same month last year.
  2. Principal performance — 4 cards (UBL/USL/Diageo/BF) with
     MTD cases + LY growth %.
  3. Cash & Stock — two-column snapshot:
     · Debtors outstanding + 90+ overdue
     · Stock value + out-of-stock SKUs + ghosted outlets
"""
from datetime import date
import pandas as pd
import streamlit as st

from utils.helpers import (
    current_month_range, same_mtd_window, format_inr, mtd_label,
)
from src.sales import _load_period_kpis, _load_principal_growth
from src.debtors import (
    _load_ledger, _fifo_unpaid, _load_last_bill_per_party,
)
from src.inventory import _load_current_stock


PRINCIPAL_ORDER = [
    ("United Breweries", "C00039", "#1B4F72"),
    ("United Spirits",   "C00025", "#378ADD"),
    ("Diageo",           "C00040", "#1D9E75"),
    ("Brown-Forman",     "C00056", "#EF9F27"),
]


def _delta_pct(cur: float, prev: float) -> str:
    if prev <= 0:
        return ""
    return f"{(cur - prev) / prev * 100:+.1f}% vs LY"


def _render_hero_strip(today: date) -> None:
    start, end = current_month_range()
    ly_start_ts, ly_end_ts = same_mtd_window(today, 12)
    kpis = _load_period_kpis(start, end,
                             ly_start_ts.date(), ly_end_ts.date(), ())

    st.markdown(
        f"### {today.strftime('%B %Y')} — month so far · "
        f"{mtd_label(today)}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Revenue", format_inr(kpis["rev"]),
              delta=_delta_pct(kpis["rev"], kpis["ly_rev"]) or None)
    c2.metric("Cases", f"{kpis['cases']:,.0f}",
              delta=_delta_pct(kpis["cases"], kpis["ly_cases"]) or None)
    c3.metric("Bills issued", f"{kpis['invoices']:,}",
              delta=_delta_pct(kpis["invoices"], kpis["ly_invoices"]) or None)
    c4.metric("Outlets billed", f"{kpis['cust']:,}",
              delta=_delta_pct(kpis["cust"], kpis["ly_cust"]) or None)


def _render_principals(today: date) -> None:
    start, end = current_month_range()
    ly_start_ts, ly_end_ts = same_mtd_window(today, 12)
    df = _load_principal_growth(start, end,
                                ly_start_ts.date(), ly_end_ts.date())
    st.markdown("### Principal performance — MTD")
    if df.empty:
        st.info("No principal sales recorded for the current month yet.")
        return
    df_idx = df.set_index("CompanyID")
    cols = st.columns(len(PRINCIPAL_ORDER))
    for col, (name, cid, color) in zip(cols, PRINCIPAL_ORDER):
        if cid not in df_idx.index:
            cs, ly = 0.0, 0.0
        else:
            r = df_idx.loc[cid]
            cs, ly = float(r["Cases"]), float(r["LyCases"])
        gr_pct = ((cs - ly) / ly * 100) if ly > 0 else None
        gr_str = (f"{gr_pct:+.1f}% vs LY" if gr_pct is not None
                  else "no LY baseline")
        gr_color = ("#16a34a" if (gr_pct is not None and gr_pct >= 0)
                    else "#dc2626" if gr_pct is not None
                    else "#888")
        col.markdown(f"""
        <div style='border-left: 4px solid {color}; padding: 0.5rem 0.9rem;
                    background: rgba(255,255,255,0.55); border-radius: 4px;
                    min-height: 95px'>
            <div style='font-size: 0.78rem; color: #555; font-weight: 600;
                        text-transform: uppercase; letter-spacing: 0.04em'>
                {name}</div>
            <div style='font-size: 1.55rem; font-weight: 800; color: #1a1a1a;
                        margin-top: 4px'>
                {cs:,.0f} <span style='font-size: 0.85rem; font-weight: 500;
                                       color: #666'>cs</span></div>
            <div style='font-size: 0.78rem; color: {gr_color};
                        font-weight: 600; margin-top: 2px'>{gr_str}</div>
        </div>
        """, unsafe_allow_html=True)


def _render_cash_and_stock() -> None:
    left, right = st.columns(2)
    today_ts = pd.Timestamp(date.today())

    with left:
        st.markdown("### 💰 Cash & debtors")
        try:
            ledger = _load_ledger()
            unpaid = _fifo_unpaid(ledger, today_ts)
        except Exception as e:
            st.warning(f"Debtors data unavailable: `{e}`")
            unpaid = pd.DataFrame()

        if unpaid.empty:
            st.caption("No unpaid invoices.")
        else:
            outstanding = float(unpaid["Remaining"].sum())
            overdue = unpaid[unpaid["AgeDays"] >= 90]
            overdue_amt = float(overdue["Remaining"].sum())
            overdue_parties = int(overdue["PartyID"].nunique())
            a, b = st.columns(2)
            a.metric("Total outstanding", format_inr(outstanding))
            b.metric(">90 day overdue", format_inr(overdue_amt))
            if outstanding > 0:
                pct = overdue_amt / outstanding * 100
                st.caption(
                    f"**{overdue_parties:,} parties** in the 90+ bucket "
                    f"(**{pct:.1f}%** of total outstanding). Open the "
                    f"**Debtors Ageing** tab for the party-wise breakdown.")

        try:
            lb = _load_last_bill_per_party()
            ghosted = int((lb["DaysSinceLastBill"] >= 90).sum())
        except Exception:
            ghosted = None
        if ghosted is not None:
            st.metric("Ghosted outlets",
                      f"{ghosted:,}",
                      help="Active parties with no sales bill in the last "
                           "90 days. Drill into the Debtors Ageing tab → "
                           "Ghosted section for the list.")

    with right:
        st.markdown("### 📦 Stock & operations")
        try:
            stock = _load_current_stock()
        except Exception as e:
            st.warning(f"Stock data unavailable: `{e}`")
            return
        if stock.empty:
            st.caption("No stock data.")
            return
        stock["StockValue"] = stock["ClosingCases"] * stock["ValRateCase"]
        total_val = float(stock["StockValue"].sum())
        oos_count = int((stock["ClosingCases"] <= 0).sum())
        a, b = st.columns(2)
        a.metric("Stock value", format_inr(total_val))
        b.metric("Out of stock", f"{oos_count:,} SKUs")
        st.caption(f"**{len(stock):,} total SKUs** in inventory "
                   f"(of which **{oos_count:,}** are out of stock).")
        if "Principal" in stock.columns:
            by_p = (stock.groupby("Principal")["StockValue"]
                         .sum().sort_values(ascending=False))
            lines = "  ·  ".join(f"**{p}** {format_inr(v)}"
                                 for p, v in by_p.head(4).items())
            st.caption(f"By principal:  {lines}")


def render() -> None:
    st.title("📊 Overview")
    st.caption(
        "One-screen morning briefing — month-to-date KPIs, principal "
        "growth, cash health, stock snapshot. Drill into individual "
        "tabs above for the deeper views.")
    today = date.today()

    _render_hero_strip(today)
    st.divider()
    _render_principals(today)
    st.divider()
    _render_cash_and_stock()
