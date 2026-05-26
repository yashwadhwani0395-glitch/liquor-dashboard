"""src/cashflow.py — Business Cash Flow & Working Capital.

A single page that answers "where is our money?":

  1. Per-principal trading block — Opening stock → + Purchases → − Sales
     (at cost / COGS) → = Closing stock, in BOTH cases and ₹ value. Stock
     is valued at MsItemMaster.ValuationCaseRate (the ERP's landed-cost rate,
     the same basis the inventory page and the BS stock figure use).

  2. Working-capital snapshot — Debtors (what customers owe us) · Stock value ·
     Creditors (what we owe suppliers) → net working capital tied up.

  3. Money to / from the companies (principals) — per principal: purchases
     this FY, payments we've made this FY, and the live ledger balance
     (advance we've paid ahead vs payable we still owe). Sourced from the
     SUNDRY CREDITORS CONTROL account (000003) where every principal sits as
     a supplier PartyID (= their CompanyID), plus MsPartyOpening.CloseBalTmp
     for the live balance.

  4. Major cash movements — monthly customer collections (cash IN) vs supplier
     payments + excise/duty (cash OUT), with the net.

Data-model notes (verified live, May-2026):
  • Purchases credit, and payments debit, the SUNDRY CREDITORS CONTROL head
    000003 (head 000002 is the SUNDRY DEBTORS control, used elsewhere). The
    principal identity is the PartyID on that line (C00025 USL, C00039 UBL,
    C00056 BF, …).
  • Diageo (C00040) has NO supplier ledger — its brands are bought through
    United Spirits / import, so Diageo's money rolls into USL. (Diageo still
    appears in the stock/trading block via its own brand CompanyID.)
  • Supplier CloseBalTmp sign: negative = we OWE the supplier (payable);
    positive = advance / claim recoverable (they owe us).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import CASES_SQL_EXPR as _CASES, safe_section

# ── Transaction types (shared with purchase/inventory modules) ───────────────
PURCHASE_TYPES: tuple[int, ...] = (11, 20, 22, 30, 32, 33, 36, 42, 45, 46, 48, 54)
SALES_TYPES:    tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── GL account heads (the Balance-Sheet truth) ──────────────────────────────
_GL_DEBTORS   = "000002"   # SUNDRY DEBTORS CONTROL  (what customers owe us)
_GL_CREDITORS = "000003"   # SUNDRY CREDITORS CONTROL (what we owe suppliers)
_GL_EXCISE    = ("000403", "000555", "000035")   # excise/duty heads

_PRINCIPAL_NAMES: dict[str, str] = {
    "C00025": "United Spirits",
    "C00040": "Diageo",
    "C00039": "United Breweries",
    "C00056": "Brown-Forman",
}
_PRINCIPAL_COLOR: dict[str, str] = {
    "United Spirits":   "#1B4F72",
    "Diageo":           "#378ADD",
    "United Breweries": "#1D9E75",
    "Brown-Forman":     "#EF9F27",
    "Other":            "#9CA3AF",
}

# FY-CASE join fragment — kills TrVocItem duplicate FY-tagged rows.
_FY_JOIN = """
    AND vi.FinancialYear = CASE
        WHEN MONTH(h.VoucherDate) >= 4
        THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
             + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
        ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
             + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
    END
"""


def _fy_bounds(today: date) -> tuple[date, str]:
    """Return (FY-start-date, FY-string e.g. '2026-2027') for `today`."""
    start_year = today.year if today.month >= 4 else today.year - 1
    return date(start_year, 4, 1), f"{start_year}-{start_year + 1}"


def _cr(x: float) -> str:
    return f"₹{x / 1e7:,.2f} Cr"


# ═══════════════════════════════════════════════════════════════════════════
# LOADERS
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_stock_value_by_principal() -> pd.DataFrame:
    """Per-principal FY-opening AND live-closing stock — cases + ₹ at the
    ERP landed-cost rate (ValuationCaseRate)."""
    # Keg-aware case conversion (20LT=2.56, 30LT=3.85, 50LT=6.41) — matches
    # CASES_SQL_EXPR used on purchase/sales. Applied to the case COUNTS only;
    # VALUE stays on plain physical units × ValuationCaseRate (per-unit rate
    # that ties to the ERP stock-value report).
    def _kegcase(qty: str) -> str:
        return f"""(CASE
            WHEN im.ItemDescription LIKE '%50 LT%' OR im.ItemDescription LIKE '%50LT%' OR im.ItemDescription LIKE '%50LTR%' THEN {qty}*(50.0/7.8)
            WHEN im.ItemDescription LIKE '%30 LT%' OR im.ItemDescription LIKE '%30LT%' OR im.ItemDescription LIKE '%30LTR%' THEN {qty}*(30.0/7.8)
            WHEN im.ItemDescription LIKE '%20 LT%' OR im.ItemDescription LIKE '%20LT%' OR im.ItemDescription LIKE '%20LTR%' THEN {qty}*(20.0/7.8)
            ELSE {qty} / NULLIF(im.BottlesPerCase, 0) END)"""
    sql = f"""
        SELECT
            ISNULL(b.CompanyID, '')               AS CompanyID,
            SUM(ISNULL(bo.OpeningQty, 0))         AS OpenBottles,
            SUM(ISNULL(bo.ClosingQty, 0))         AS CloseBottles,
            SUM(CASE WHEN im.BottlesPerCase > 0
                     THEN ISNULL(bo.OpeningQty,0) / CAST(im.BottlesPerCase AS float)
                          * ISNULL(im.ValuationCaseRate,0) ELSE 0 END) AS OpenValue,
            SUM(CASE WHEN im.BottlesPerCase > 0
                     THEN ISNULL(bo.ClosingQty,0) / CAST(im.BottlesPerCase AS float)
                          * ISNULL(im.ValuationCaseRate,0) ELSE 0 END) AS CloseValue,
            SUM({_kegcase('ISNULL(bo.OpeningQty,0)')})  AS OpenCases,
            SUM({_kegcase('ISNULL(bo.ClosingQty,0)')})  AS CloseCases
        FROM MsItemBatchOpening bo
        JOIN MsItemMaster  im ON im.ItemID = bo.ItemID
        LEFT JOIN MsBrandMaster b ON b.BrandID = im.BrandID
        WHERE bo.ItemID LIKE 'I%'
        GROUP BY b.CompanyID
    """
    df = run_query(sql)
    if df.empty:
        return df
    for c in ("OpenValue", "CloseValue", "OpenCases", "CloseCases"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_movements_by_principal(start: date, end: date) -> pd.DataFrame:
    """Per-principal purchases (inward) and sales/COGS (outward) between
    start..end, in cases + ₹ at landed cost. Purchases = PURCHASE_TYPES,
    Sales = SALES_TYPES (so transfers/adjustments don't pollute either)."""
    pu = ",".join(str(t) for t in PURCHASE_TYPES)
    sa = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            ISNULL(b.CompanyID, '')   AS CompanyID,
            CASE WHEN h.TransTypeID IN ({pu}) THEN 'PUR'
                 WHEN h.TransTypeID IN ({sa}) THEN 'SAL' END AS Flow,
            SUM({_CASES})             AS Cases,
            SUM(({_CASES}) * ISNULL(im.ValuationCaseRate, 0)) AS Value
        FROM TrVocItem vi
        JOIN TrVocHead   h  ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
        JOIN MsItemMaster im ON im.ItemID    = vi.ItemID
        LEFT JOIN MsBrandMaster b ON b.BrandID = im.BrandID
        WHERE h.Cancelled  = 'N'
          AND vi.FreeItemYN = 'N'
          AND vi.ItemID     LIKE 'I%'
          AND h.TransTypeID IN ({pu},{sa})
          AND h.VoucherDate BETWEEN ? AND ?
          {_FY_JOIN}
        GROUP BY b.CompanyID,
                 CASE WHEN h.TransTypeID IN ({pu}) THEN 'PUR'
                      WHEN h.TransTypeID IN ({sa}) THEN 'SAL' END
    """
    df = run_query(sql, (str(start), str(end)))
    if df.empty:
        return df
    df["Cases"] = pd.to_numeric(df["Cases"], errors="coerce").fillna(0.0)
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce").fillna(0.0)
    df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_principal_money(fy: str) -> pd.DataFrame:
    """Per supplier-party on SUNDRY CREDITORS CONTROL (000003): purchases
    (Cr) and payments (Dr) booked THIS FY, plus the live ledger balance
    (MsPartyOpening.CloseBalTmp). Negative balance = payable (we owe);
    positive = advance / claim recoverable."""
    flow = run_query(f"""
        SELECT d.PartyID,
               SUM(CASE WHEN d.DrCrIndicator='C' THEN CAST(d.Amount AS float) ELSE 0 END) AS Purchases,
               SUM(CASE WHEN d.DrCrIndicator='D' THEN CAST(d.Amount AS float) ELSE 0 END) AS Payments
        FROM TrVocDetail d
        JOIN TrVocHead h ON h.TransTypeID=d.TransTypeID AND h.VoucherNo=d.VoucherNo
                        AND h.FinancialYear=d.FinancialYear
        WHERE d.AccHeadID='{_GL_CREDITORS}' AND h.Cancelled='N'
          AND h.FinancialYear='{fy}' AND d.PartyID LIKE 'C%'
        GROUP BY d.PartyID
    """)
    bal = run_query(f"""
        SELECT o.PartyID, ISNULL(p.PartyName, o.PartyID) AS PartyName,
               CAST(o.CloseBalTmp AS float) AS Balance
        FROM MsPartyOpening o
        LEFT JOIN MsPartyMaster p ON p.PartyID = o.PartyID
        WHERE o.PartyID LIKE 'C%'
    """)
    if bal.empty:
        return bal
    bal["Balance"] = pd.to_numeric(bal["Balance"], errors="coerce").fillna(0.0)
    if not flow.empty:
        flow["Purchases"] = pd.to_numeric(flow["Purchases"], errors="coerce").fillna(0.0)
        flow["Payments"]  = pd.to_numeric(flow["Payments"],  errors="coerce").fillna(0.0)
        out = bal.merge(flow, on="PartyID", how="left")
    else:
        out = bal.assign(Purchases=0.0, Payments=0.0)
    out[["Purchases", "Payments"]] = out[["Purchases", "Payments"]].fillna(0.0)
    # Keep parties with any activity or a live balance
    out = out[(out["Purchases"].abs() + out["Payments"].abs()
               + out["Balance"].abs()) > 1.0].copy()
    out = out.sort_values("Balance")   # most-owed (most negative) first
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _load_head_balance(acc_head_id: str) -> float:
    """Live CloseBalTmp for a single account head (₹)."""
    df = run_query(
        "SELECT CAST(CloseBalTmp AS float) AS Bal FROM MsAcHeadOpening "
        "WHERE AccHeadID=%s", (acc_head_id,))
    if df.empty or pd.isna(df.iloc[0]["Bal"]):
        return 0.0
    return float(df.iloc[0]["Bal"])


@st.cache_data(ttl=3600, show_spinner=False)
def _load_cash_movement(months_back: int = 12) -> pd.DataFrame:
    """Monthly major cash movements:
      IN  = customer collections (Cr on the debtor-control head 000002)
      OUT = supplier payments (Dr on creditor-control 000003)
          + excise / duty paid (Dr on excise heads)
    Approximate but covers the big-ticket cash flows."""
    exc = ",".join(f"'{h}'" for h in _GL_EXCISE)
    sql = f"""
        SELECT FORMAT(h.VoucherDate, 'yyyy-MM') AS Mon,
               SUM(CASE WHEN d.AccHeadID='{_GL_DEBTORS}'   AND d.DrCrIndicator='C'
                        THEN CAST(d.Amount AS float) ELSE 0 END) AS Collections,
               SUM(CASE WHEN d.AccHeadID='{_GL_CREDITORS}' AND d.DrCrIndicator='D'
                        THEN CAST(d.Amount AS float) ELSE 0 END) AS SupplierPayments,
               SUM(CASE WHEN d.AccHeadID IN ({exc})        AND d.DrCrIndicator='D'
                        THEN CAST(d.Amount AS float) ELSE 0 END) AS ExcisePaid
        FROM TrVocDetail d
        JOIN TrVocHead h ON h.TransTypeID=d.TransTypeID AND h.VoucherNo=d.VoucherNo
                        AND h.FinancialYear=d.FinancialYear
        WHERE h.Cancelled='N'
          AND d.AccHeadID IN ('{_GL_DEBTORS}','{_GL_CREDITORS}',{exc})
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
          AND h.VoucherDate <= GETDATE()
        GROUP BY FORMAT(h.VoucherDate, 'yyyy-MM')
        ORDER BY Mon
    """
    df = run_query(sql)
    if df.empty:
        return df
    for c in ("Collections", "SupplierPayments", "ExcisePaid"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["CashOut"] = df["SupplierPayments"] + df["ExcisePaid"]
    df["Net"]     = df["Collections"] - df["CashOut"]
    return df


# ═══════════════════════════════════════════════════════════════════════════
# SECTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _section_trading(today: date) -> None:
    fy_start, _ = _fy_bounds(today)
    st.subheader("📦 Stock & trading — per principal (this financial year)")
    st.caption(
        f"Opening stock (1 Apr) → + Purchases → − Sales at cost (COGS) → = "
        f"Closing stock (live). Period: {fy_start:%d %b %Y} → {today:%d %b %Y}. "
        "Stock valued at the ERP landed-cost rate (ValuationCaseRate)."
    )

    stock = _load_stock_value_by_principal()
    mov   = _load_movements_by_principal(fy_start, today)
    if stock.empty:
        st.info("No stock data."); return

    order = ["United Breweries", "United Spirits", "Diageo", "Brown-Forman", "Other"]
    rows = []
    for pr in order:
        s = stock[stock["Principal"] == pr]
        if s.empty:
            continue
        op_v = float(s["OpenValue"].sum());   cl_v = float(s["CloseValue"].sum())
        op_c = float(s["OpenCases"].sum());   cl_c = float(s["CloseCases"].sum())
        pm = mov[(mov["Principal"] == pr) & (mov["Flow"] == "PUR")]
        sm = mov[(mov["Principal"] == pr) & (mov["Flow"] == "SAL")]
        pu_v = float(pm["Value"].sum()); pu_c = float(pm["Cases"].sum())
        sa_v = float(sm["Value"].sum()); sa_c = float(sm["Cases"].sum())
        rows.append({
            "Principal": pr,
            "Opening ₹": op_v, "Purchases ₹": pu_v, "COGS ₹": sa_v, "Closing ₹": cl_v,
            "Opening cs": op_c, "Purchases cs": pu_c, "Sales cs": sa_c, "Closing cs": cl_c,
        })
    if not rows:
        st.info("No principal stock data."); return
    df = pd.DataFrame(rows)
    tot = {"Principal": "TOTAL"}
    for c in df.columns:
        if c != "Principal":
            tot[c] = float(df[c].sum())
    df = pd.concat([df, pd.DataFrame([tot])], ignore_index=True)

    val_view = st.radio("View", ["₹ value", "Cases"], horizontal=True,
                        key="cf_trading_view")
    if val_view == "₹ value":
        show = df[["Principal", "Opening ₹", "Purchases ₹", "COGS ₹", "Closing ₹"]]
        fmt = {c: (lambda v: _cr(v)) for c in show.columns if c != "Principal"}
    else:
        show = df[["Principal", "Opening cs", "Purchases cs", "Sales cs", "Closing cs"]]
        fmt = {c: "{:,.0f}" for c in show.columns if c != "Principal"}
    st.dataframe(show.style.format(fmt), use_container_width=True, hide_index=True)
    st.caption(
        "ℹ️ Identity check: Opening + Purchases − COGS ≈ Closing. Small gaps "
        "are stock adjustments / breakages / free-goods not in COGS. Diageo "
        "stock shows under its own brand company even though its *money* "
        "settles through United Spirits (see below)."
    )
    st.download_button(
        "⬇️ Download — stock & trading (₹ + cases)",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"cashflow_trading_{today:%Y%m%d}.csv", mime="text/csv",
        key="cf_dl_trading",
    )


def _section_working_capital(today: date) -> None:
    st.subheader("💼 Working-capital snapshot (live)")
    debtors   = _load_head_balance(_GL_DEBTORS)          # +ve = receivable
    creditors = _load_head_balance(_GL_CREDITORS)        # -ve = payable
    stock     = _load_stock_value_by_principal()
    stock_val = float(stock["CloseValue"].sum()) if not stock.empty else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sundry Debtors", _cr(debtors), help="What customers owe us")
    c2.metric("Stock on hand", _cr(stock_val), help="Live inventory at landed cost")
    c3.metric("Sundry Creditors", _cr(abs(creditors)),
              help="What we owe suppliers (trade payables)")
    # Working capital tied up = receivables + stock − payables
    wc = debtors + stock_val - abs(creditors)
    c4.metric("Net working capital", _cr(wc),
              help="Debtors + Stock − Creditors = cash locked in the business")
    st.caption(
        "Debtors + Stock is money parked in customers and shelves; Creditors "
        "is the free credit suppliers extend us. The net is the cash the "
        "business has tied up in working capital right now."
    )


def _section_money_to_companies(today: date) -> None:
    _, fy = _fy_bounds(today)
    st.subheader("🏦 Money to / from the principals (this FY)")
    st.caption(
        "Per supplier: what we purchased, what we've paid, and the live "
        "ledger balance — **advance** (we've paid ahead, they owe us) vs "
        "**payable** (we still owe them). Sourced from the creditor-control "
        "ledger; balance is the live CloseBalTmp."
    )
    df = _load_principal_money(fy)
    if df.empty:
        st.info("No supplier ledger activity."); return

    disp = df.copy()
    disp["Status"] = disp["Balance"].map(
        lambda b: f"🟢 Advance {_cr(b)}" if b > 1
        else (f"🔴 Payable {_cr(-b)}" if b < -1 else "— settled"))
    disp["Purchases ₹"] = disp["Purchases"]
    disp["Payments ₹"]  = disp["Payments"]
    out = disp[["PartyName", "Purchases ₹", "Payments ₹", "Status"]]
    st.dataframe(
        out.style.format({"Purchases ₹": lambda v: _cr(v),
                          "Payments ₹":  lambda v: _cr(v)}),
        use_container_width=True, hide_index=True,
    )

    payable = -df.loc[df["Balance"] < 0, "Balance"].sum()
    advance =  df.loc[df["Balance"] > 0, "Balance"].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total payable to suppliers", _cr(payable))
    c2.metric("Total advances / claims", _cr(advance))
    c3.metric("Net owed to suppliers", _cr(payable - advance))
    st.caption(
        "Note: **Diageo settles through United Spirits / imports**, so it has "
        "no separate supplier line here. '(CLAIM)' accounts are amounts the "
        "principal owes us back (scheme claims / returns)."
    )
    st.download_button(
        "⬇️ Download — supplier money position",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"cashflow_suppliers_{today:%Y%m%d}.csv", mime="text/csv",
        key="cf_dl_money",
    )


def _section_cash_movement(today: date) -> None:
    st.subheader("💸 Major cash movements — last 12 months")
    st.caption(
        "Cash IN = customer collections · Cash OUT = supplier payments + "
        "excise/duty paid. A focused view of the biggest money flows (not a "
        "full statutory cash-flow statement)."
    )
    df = _load_cash_movement(12)
    if df.empty:
        st.info("No cash-movement data."); return

    fig = go.Figure()
    fig.add_bar(x=df["Mon"], y=df["Collections"] / 1e7, name="Collections (IN)",
                marker_color="#1D9E75")
    fig.add_bar(x=df["Mon"], y=-df["CashOut"] / 1e7, name="Payments + Excise (OUT)",
                marker_color="#dc2626")
    fig.add_scatter(x=df["Mon"], y=df["Net"] / 1e7, name="Net", mode="lines+markers",
                    line=dict(color="#1B4F72", width=2))
    fig.update_layout(barmode="relative", template="plotly_white",
                      height=340, margin=dict(t=10, b=10, l=10, r=10),
                      yaxis_title="₹ Cr", legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)

    disp = df.copy()
    for c in ("Collections", "SupplierPayments", "ExcisePaid", "CashOut", "Net"):
        disp[c] = disp[c] / 1e7
    disp = disp.rename(columns={
        "Collections": "Collections ₹Cr", "SupplierPayments": "Supplier pmts ₹Cr",
        "ExcisePaid": "Excise ₹Cr", "CashOut": "Cash OUT ₹Cr", "Net": "Net ₹Cr"})
    st.dataframe(
        disp[["Mon", "Collections ₹Cr", "Supplier pmts ₹Cr", "Excise ₹Cr",
              "Cash OUT ₹Cr", "Net ₹Cr"]].style.format(
            {c: "{:,.2f}" for c in disp.columns if c != "Mon"}),
        use_container_width=True, hide_index=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("💰 Cash Flow & Working Capital")
    st.caption(
        "Where the money is: stock bought/sold/held per principal, what we "
        "owe vs what we're owed, money flowing to the companies, and the big "
        "monthly cash movements."
    )
    today = date.today()
    st.divider()

    safe_section("Working capital", _section_working_capital, today)
    st.divider()
    safe_section("Stock & trading", _section_trading, today)
    st.divider()
    safe_section("Money to companies", _section_money_to_companies, today)
    st.divider()
    safe_section("Cash movement", _section_cash_movement, today)
