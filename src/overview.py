"""src/overview.py — single-screen morning briefing for the owner.

Pulls hero KPIs from every other module using their already-cached
loaders, so this tab opens fast (instant if the user has already
visited Sales / Debtors / Inventory in the session). The goal is
"what do I need to know in 30 seconds" — not a deep drill-down.

Layout (top to bottom):
  0. Attention Today — 5 severity-coloured counter cards for the
     five things that require action: banned outlets, >90d debtors,
     ghosted outlets, stockouts, cheque bounces in last 30d. One
     expander below to drill into the top-5 offenders per category.
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
    _load_cheque_returns_per_party,
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


# ═══════════════════════════════════════════════════════════════════════════
# ATTENTION TODAY — 5 severity-coloured counters + drill-down expander
# ═══════════════════════════════════════════════════════════════════════════

def _compute_attention() -> dict:
    """Compute the 5 attention counters + top-5 offenders for each.

    Uses only already-cached loaders — the whole strip adds ~0 ms on a
    warm session and ~1.5 s cold (dominated by _load_ledger).
    """
    ledger    = _load_ledger()
    unpaid    = _fifo_unpaid(ledger, pd.Timestamp(date.today()))
    last_bill = _load_last_bill_per_party()
    stock     = _load_current_stock()
    cheques   = _load_cheque_returns_per_party()

    # Party-id → name lookup from the ledger (dedupe first row per party)
    party_names = (ledger.drop_duplicates("PartyID")
                         .set_index("PartyID")["PartyName"].to_dict())

    # 1. Banned outlets that still owe us money
    banned = ledger[
        (ledger.get("BannedByAssoc", "") == "Y")
        & (ledger["DrCrIndicator"] == "D")
        & (ledger["BalanceAmount"] > 0.5)
    ]
    if not banned.empty:
        banned_top = (banned.groupby(["PartyID", "PartyName"], as_index=False)
                            ["BalanceAmount"].sum()
                            .sort_values("BalanceAmount", ascending=False))
    else:
        banned_top = pd.DataFrame(columns=["PartyID", "PartyName", "BalanceAmount"])
    banned_count = int(banned_top["PartyID"].nunique()) if not banned_top.empty else 0
    banned_amt   = float(banned_top["BalanceAmount"].sum()) if not banned_top.empty else 0.0

    # 2. >90 day overdue parties
    if not unpaid.empty:
        overdue = unpaid[unpaid["AgeDays"] >= 90]
        overdue_top = (overdue.groupby(["PartyID", "PartyName"], as_index=False)
                              .agg(Remaining=("Remaining", "sum"),
                                   MaxAge=("AgeDays", "max"))
                              .sort_values("Remaining", ascending=False))
    else:
        overdue_top = pd.DataFrame(columns=["PartyID", "PartyName", "Remaining", "MaxAge"])
    overdue_count = int(overdue_top["PartyID"].nunique()) if not overdue_top.empty else 0
    overdue_amt   = float(overdue_top["Remaining"].sum()) if not overdue_top.empty else 0.0

    # 3. Ghosted outlets (no bill in ≥ 90 days)
    if not last_bill.empty:
        ghosted = last_bill[last_bill["DaysSinceLastBill"] >= 90].copy()
        ghosted["PartyName"] = (ghosted["PartyID"].map(party_names)
                                                   .fillna("(unknown)"))
        ghosted = ghosted.sort_values("DaysSinceLastBill", ascending=False)
    else:
        ghosted = pd.DataFrame(columns=["PartyID", "PartyName", "DaysSinceLastBill"])
    ghosted_count = int(len(ghosted))

    # 4. Stockouts — SKUs where ClosingCases has fallen to zero
    if not stock.empty:
        oos = stock[stock["ClosingCases"] <= 0].copy()
        if "ValRateCase" in oos.columns:
            oos = oos.sort_values("ValRateCase", ascending=False)
    else:
        oos = pd.DataFrame()
    oos_count = int(len(oos))

    # 5. Cheque returns received in the last 30 days
    if not cheques.empty:
        recent = cheques[cheques["DaysSinceLastReturn"] <= 30].copy()
        recent["PartyName"] = (recent["PartyID"].map(party_names)
                                                 .fillna("(unknown)"))
        recent = recent.sort_values("TotalReturnedAmt", ascending=False)
    else:
        recent = pd.DataFrame(columns=[
            "PartyID","PartyName","TotalReturnedAmt","DaysSinceLastReturn"])
    bounce_count = int(len(recent))
    bounce_amt   = float(recent["TotalReturnedAmt"].sum()) if not recent.empty else 0.0

    return {
        "banned":  {"count": banned_count, "amt": banned_amt, "top": banned_top.head(5)},
        "overdue": {"count": overdue_count, "amt": overdue_amt, "top": overdue_top.head(5)},
        "ghosted": {"count": ghosted_count, "top": ghosted.head(5)},
        "oos":     {"count": oos_count, "top": oos.head(5)},
        "bounces": {"count": bounce_count, "amt": bounce_amt, "top": recent.head(5)},
    }


def _severity_bg(count: int, high: int, mid: int) -> str:
    """Choose a background colour by severity — red / amber / green."""
    if count >= high:
        return "#fee2e2"   # red-100 — needs attention today
    if count >= mid:
        return "#fef3c7"   # amber-100 — watch
    return "#dcfce7"       # green-100 — clear


def _attn_card(col, icon: str, title: str, count: int,
               subtitle: str, high: int, mid: int) -> None:
    bg = _severity_bg(count, high, mid)
    dim = "#6b7280" if count == 0 else "#111827"
    col.markdown(f"""
    <div style='background:{bg}; padding:10px 14px; border-radius:8px;
                border:1px solid rgba(0,0,0,0.05); min-height:88px'>
      <div style='font-size:0.72rem; color:#374151; font-weight:600;
                  text-transform:uppercase; letter-spacing:0.04em'>
        {icon}&nbsp;{title}
      </div>
      <div style='font-size:1.8rem; font-weight:800; color:{dim};
                  margin-top:2px; line-height:1.1'>
        {count:,}
      </div>
      <div style='font-size:0.72rem; color:#4b5563; margin-top:2px'>
        {subtitle}
      </div>
    </div>
    """, unsafe_allow_html=True)


def _render_attention_today() -> None:
    st.markdown("### ⚡ Attention today")
    try:
        a = _compute_attention()
    except Exception as e:
        st.warning(f"Attention strip unavailable: `{e}`")
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    _attn_card(c1, "🚫", "Banned & owe",     a["banned"]["count"],
               f"₹{a['banned']['amt']/1e5:.1f}L exposure" if a["banned"]["count"]
               else "no exposure",
               high=1, mid=1)
    _attn_card(c2, "⏰", ">90d overdue",      a["overdue"]["count"],
               f"₹{a['overdue']['amt']/1e5:.1f}L stuck" if a["overdue"]["count"]
               else "clear",
               high=10, mid=1)
    _attn_card(c3, "👻", "Ghosted (90d)",    a["ghosted"]["count"],
               "no bill 90d+" if a["ghosted"]["count"] else "all active",
               high=25, mid=5)
    _attn_card(c4, "📦", "Stockouts",         a["oos"]["count"],
               "SKUs at zero" if a["oos"]["count"] else "in stock",
               high=20, mid=5)
    _attn_card(c5, "📉", "Cheque bounces",   a["bounces"]["count"],
               f"₹{a['bounces']['amt']/1e5:.1f}L in 30d" if a["bounces"]["count"]
               else "no bounces 30d",
               high=1, mid=1)

    with st.expander("🔍 Show top-5 offenders in each category",
                     expanded=False):
        # Banned
        st.markdown("**🚫 Banned outlets that still owe us money**")
        if a["banned"]["top"].empty:
            st.caption("No banned parties with outstanding balance.")
        else:
            b = a["banned"]["top"].rename(columns={
                "PartyName": "Party", "BalanceAmount": "Owed"})[["Party", "Owed"]]
            st.dataframe(b.style.format({"Owed": format_inr}),
                         use_container_width=True, hide_index=True)

        # 90+ overdue
        st.markdown("**⏰ Parties >90 days overdue** (drill in Debtors Ageing)")
        if a["overdue"]["top"].empty:
            st.caption("No 90+ overdue parties.")
        else:
            o = a["overdue"]["top"].rename(columns={
                "PartyName": "Party", "Remaining": "Overdue ₹",
                "MaxAge": "Oldest bill (days)"})[
                ["Party", "Overdue ₹", "Oldest bill (days)"]]
            st.dataframe(o.style.format({"Overdue ₹": format_inr,
                                          "Oldest bill (days)": "{:.0f}"}),
                         use_container_width=True, hide_index=True)

        # Ghosted
        st.markdown("**👻 Ghosted outlets** (no sales bill in 90+ days)")
        if a["ghosted"]["top"].empty:
            st.caption("No ghosted outlets.")
        else:
            g = a["ghosted"]["top"].rename(columns={
                "PartyName": "Party", "DaysSinceLastBill": "Days silent",
                "LastBillDate": "Last bill"})[
                ["Party", "Last bill", "Days silent"]]
            st.dataframe(g, use_container_width=True, hide_index=True)

        # Stockouts
        st.markdown("**📦 SKUs currently out of stock** (highest-value first)")
        if a["oos"]["top"].empty:
            st.caption("No stockouts.")
        else:
            keep_cols = [c for c in ("BrandName", "Principal", "ValRateCase",
                                     "ItemID") if c in a["oos"]["top"].columns]
            oos_show = a["oos"]["top"][keep_cols].rename(columns={
                "BrandName": "Brand", "ValRateCase": "Rate/case (₹)",
                "ItemID": "SKU"})
            st.dataframe(
                oos_show.style.format({"Rate/case (₹)": format_inr}),
                use_container_width=True, hide_index=True)

        # Bounces
        st.markdown("**📉 Cheque returns in the last 30 days**")
        if a["bounces"]["top"].empty:
            st.caption("No cheque bounces in the last 30 days.")
        else:
            b = a["bounces"]["top"].rename(columns={
                "PartyName": "Party", "TotalReturnedAmt": "Bounced ₹",
                "ReturnCount": "# returns",
                "DaysSinceLastReturn": "Days ago"})[
                ["Party", "Bounced ₹", "# returns", "Days ago"]]
            st.dataframe(b.style.format({"Bounced ₹": format_inr}),
                         use_container_width=True, hide_index=True)


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

    _render_attention_today()
    st.divider()
    _render_hero_strip(today)
    st.divider()
    _render_principals(today)
    st.divider()
    _render_cash_and_stock()
