"""src/purchase.py — Purchase Analytics.

What we buy from principals: volume, value, monthly trend, top brands,
and the critical Purchase-vs-Sales reconciliation table that every
principal review meeting opens with.
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

# ── Transaction types ────────────────────────────────────────────────────────
PURCHASE_TYPES: tuple[int, ...] = (11, 20, 22, 30, 32, 33, 36, 42, 45, 46, 48, 54)
SALES_TYPES:    tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── GL Account heads (the Balance-Sheet truth) ──────────────────────────────
_GL_PURCHASES        = "000005"   # PURCHASES - TRADING
_GL_EXCISE_IMPORT    = "000403"   # EXCISE DUTY ON IMPORT
_GL_EXCISE_DIAGEO    = "000555"   # EXCISE DUTY ON IMPORT-DIAGEO
_GL_EXCISE_SUPERV    = "000035"   # EXCISE SUPERVISION
_GL_EXCISE_ALL       = (_GL_EXCISE_IMPORT, _GL_EXCISE_DIAGEO, _GL_EXCISE_SUPERV)

# ── Principal config ─────────────────────────────────────────────────────────
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
}
_KPI_COLORS = ["#1B4F72", "#378ADD", "#1D9E75", "#EF9F27"]

# ── FY-CASE join fragment (kills TrVocItem duplicate FY rows) ────────────────
_FY_JOIN = """
    AND vi.FinancialYear = CASE
        WHEN MONTH(h.VoucherDate) >= 4
        THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
             + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
        ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
             + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
    END
"""


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


def _fmt_month(ym: str) -> str:
    return date(int(ym[:4]), int(ym[5:]), 1).strftime("%b %y")


def _fy_of(ym: str) -> int:
    y, m = int(ym[:4]), int(ym[5:])
    return y if m >= 4 else y - 1


def _last_n_month_strs(n: int, ref: date | None = None) -> list[str]:
    ref = ref or date.today()
    out = []
    for i in range(n - 1, -1, -1):
        mo = ref.month - i
        yr = ref.year
        while mo <= 0:
            mo += 12
            yr -= 1
        out.append(f"{yr:04d}-{mo:02d}")
    return out


def _yoy_delta(curr: float, prev: float) -> tuple[str, str]:
    if prev == 0:
        return ("→ new", "#6b7280")
    pct = (curr - prev) / prev * 100
    if pct > 0.05:  return (f"↑ {pct:.1f}%",      "#16a34a")
    if pct < -0.05: return (f"↓ {abs(pct):.1f}%", "#dc2626")
    return ("→ flat", "#6b7280")


def _kpi_card(label: str, value: str, delta_text: str,
              delta_color: str, accent: str) -> str:
    return f"""
    <div style='background:#fff;border:1px solid #e5e7eb;border-radius:8px;
                border-left:4px solid {accent};padding:12px 16px;
                box-shadow:0 1px 2px rgba(0,0,0,0.04);'>
        <div style='font-size:0.7rem;color:#6b7280;
                    text-transform:uppercase;letter-spacing:0.05em'>{label}</div>
        <div style='font-size:1.5rem;font-weight:700;color:#111827;
                    margin-top:4px;line-height:1.15'>{value}</div>
        <div style='font-size:0.78rem;color:{delta_color};margin-top:2px;
                    font-weight:600'>{delta_text}</div>
    </div>
    """


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def _load_purchase_kpis(start: date, end: date,
                        ly_start: date, ly_end: date,
                        principal_ids: tuple[str, ...]) -> dict:
    type_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS P,
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS LyP,
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS C,
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS LyC,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN h.VoucherNo END) AS I,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN h.VoucherNo END) AS LyI,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN b.CompanyID END) AS Sup,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN b.CompanyID END) AS LySup
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            {_FY_JOIN}
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          {company_sql}
    """
    s, e, ls, le = str(start), str(end), str(ly_start), str(ly_end)
    params = (
        s, e, ls, le, s, e, ls, le,
        s, e, ls, le, s, e, ls, le,
        ls, e,
    ) + company_params
    df = run_query(sql, params)
    if df.empty:
        return {"p": 0, "ly_p": 0, "c": 0, "ly_c": 0,
                "i": 0, "ly_i": 0, "sup": 0, "ly_sup": 0}
    r = df.iloc[0]
    return {
        "p":      float(r["P"]    or 0),
        "ly_p":   float(r["LyP"]  or 0),
        "c":      float(r["C"]    or 0),
        "ly_c":   float(r["LyC"]  or 0),
        "i":      int(r["I"]      or 0),
        "ly_i":   int(r["LyI"]    or 0),
        "sup":    int(r["Sup"]    or 0),
        "ly_sup": int(r["LySup"]  or 0),
    }


@st.cache_data(ttl=300, show_spinner=False)
def _load_purchase_gl(start: date, end: date,
                      ly_start: date, ly_end: date) -> dict:
    """GL-based purchase totals (canonical — matches Balance Sheet).

    Returns dict keyed by AccHeadID with both current and LY net-debit
    figures, plus convenience aggregates (purchases / excise / total).
    """
    sql = """
        SELECT
            ah.AccHeadID, ah.AccName,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ?
                       THEN (CASE WHEN d.DrCrIndicator='D' THEN d.Amount ELSE -d.Amount END)
                     ELSE 0 END) AS Curr,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ?
                       THEN (CASE WHEN d.DrCrIndicator='D' THEN d.Amount ELSE -d.Amount END)
                     ELSE 0 END) AS Ly
        FROM TrVocDetail d
        JOIN TrVocHead h
            ON  h.TransTypeID = d.TransTypeID
            AND h.VoucherNo   = d.VoucherNo
        JOIN MsAccountHead ah ON ah.AccHeadID = d.AccHeadID
        WHERE h.Cancelled = 'N'
          AND ah.AccHeadID IN (?, ?, ?, ?)
          AND h.VoucherDate BETWEEN ? AND ?
        GROUP BY ah.AccHeadID, ah.AccName
    """
    s, e, ls, le = str(start), str(end), str(ly_start), str(ly_end)
    params = (
        s, e, ls, le,
        _GL_PURCHASES, _GL_EXCISE_IMPORT, _GL_EXCISE_DIAGEO, _GL_EXCISE_SUPERV,
        ls, e,
    )
    df = run_query(sql, params)

    out = {
        "purchases":      0.0, "ly_purchases":      0.0,
        "excise_import":  0.0, "ly_excise_import":  0.0,
        "excise_diageo":  0.0, "ly_excise_diageo":  0.0,
        "excise_superv":  0.0, "ly_excise_superv":  0.0,
        "accounts":       [],   # full list for the breakdown table
    }
    if df.empty:
        return out

    for _, r in df.iterrows():
        aid  = str(r["AccHeadID"]).strip()
        curr = float(r["Curr"] or 0)
        ly   = float(r["Ly"]   or 0)
        out["accounts"].append({
            "AccHeadID": aid, "AccName": r["AccName"], "Curr": curr, "Ly": ly,
        })
        if aid == _GL_PURCHASES:
            out["purchases"], out["ly_purchases"] = curr, ly
        elif aid == _GL_EXCISE_IMPORT:
            out["excise_import"], out["ly_excise_import"] = curr, ly
        elif aid == _GL_EXCISE_DIAGEO:
            out["excise_diageo"], out["ly_excise_diageo"] = curr, ly
        elif aid == _GL_EXCISE_SUPERV:
            out["excise_superv"], out["ly_excise_superv"] = curr, ly

    out["excise_total"]    = out["excise_import"]    + out["excise_diageo"]    + out["excise_superv"]
    out["ly_excise_total"] = out["ly_excise_import"] + out["ly_excise_diageo"] + out["ly_excise_superv"]
    out["total_cost"]      = out["purchases"]    + out["excise_total"]
    out["ly_total_cost"]   = out["ly_purchases"] + out["ly_excise_total"]
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _load_purchase_by_principal(start: date, end: date,
                                ly_start: date, ly_end: date) -> pd.DataFrame:
    type_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    sql = f"""
        SELECT
            b.CompanyID,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS P,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS LyP,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS C,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS LyC,
            COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN h.VoucherNo END) AS I
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            {_FY_JOIN}
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          AND b.CompanyID IN ('C00025','C00040','C00039','C00056')
        GROUP BY b.CompanyID
    """
    s, e, ls, le = str(start), str(end), str(ly_start), str(ly_end)
    params = (s, e, ls, le, s, e, ls, le, s, e, ls, e)
    df = run_query(sql, params)
    if not df.empty:
        for c in ("P", "LyP"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        for c in ("C", "LyC", "I"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_purchase_vs_sales(start: date, end: date) -> pd.DataFrame:
    """Per principal: (cases bought + value) vs (cases sold + value)."""
    pu_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    ms_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            b.CompanyID,
            SUM(CASE WHEN h.TransTypeID IN ({pu_ph}) THEN {_CASES} ELSE 0 END) AS BCases,
            SUM(CASE WHEN h.TransTypeID IN ({pu_ph}) THEN vi.TotalAmount ELSE 0 END) AS BVal,
            SUM(CASE WHEN h.TransTypeID IN ({ms_ph}) THEN {_CASES} ELSE 0 END) AS SCases,
            SUM(CASE WHEN h.TransTypeID IN ({ms_ph}) THEN vi.TotalAmount ELSE 0 END) AS SVal
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            {_FY_JOIN}
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.Cancelled = 'N'
          AND h.TransTypeID IN ({pu_ph},{ms_ph})
          AND h.VoucherDate BETWEEN ? AND ?
          AND b.CompanyID IN ('C00025','C00040','C00039','C00056')
        GROUP BY b.CompanyID
    """
    df = run_query(sql, (str(start), str(end)))
    if not df.empty:
        for c in ("BVal", "SVal"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        for c in ("BCases", "SCases"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_purchase_monthly(months: int = 24,
                           principal_ids: tuple[str, ...] = ()) -> pd.DataFrame:
    type_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
            FORMAT(h.VoucherDate, 'yyyy-MM') AS Month,
            SUM(CAST(vi.TotalAmount AS FLOAT))  AS Purchase,
            SUM({_CASES}) AS Cases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            {_FY_JOIN}
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          {company_sql}
        GROUP BY FORMAT(h.VoucherDate, 'yyyy-MM')
        ORDER BY Month
    """
    df = run_query(sql, company_params)
    if not df.empty:
        df["Purchase"] = pd.to_numeric(df["Purchase"], errors="coerce").fillna(0.0)
        df["Cases"]    = pd.to_numeric(df["Cases"],    errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_top_brands_purchased(start: date, end: date,
                               principal_ids: tuple[str, ...]) -> pd.DataFrame:
    type_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT TOP 15
            b.BrandName, b.CompanyID,
            SUM({_CASES}) AS Cases,
            SUM(CAST(vi.TotalAmount AS FLOAT))  AS Purchase
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            {_FY_JOIN}
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          {company_sql}
        GROUP BY b.BrandName, b.CompanyID
        ORDER BY Purchase DESC
    """
    df = run_query(sql, (str(start), str(end)) + company_params)
    if not df.empty:
        df["Purchase"]  = pd.to_numeric(df["Purchase"], errors="coerce").fillna(0.0)
        df["Cases"]     = pd.to_numeric(df["Cases"],    errors="coerce").fillna(0.0)
        df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_recent_vouchers(end: date, limit: int = 50) -> pd.DataFrame:
    type_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    sql = f"""
        SELECT TOP {limit}
            h.VoucherDate,
            h.VoucherNo,
            h.TransTypeID,
            SUM({_CASES}) AS Cases,
            SUM(CAST(vi.TotalAmount AS FLOAT))  AS Amount,
            -- Principal inferred from brand CompanyID majority on the voucher
            (
              SELECT TOP 1 ISNULL(_pp.PartyName,_b.CompanyID)
              FROM TrVocItem _vi
              JOIN MsItemMaster  _im ON _im.ItemID  = _vi.ItemID
              JOIN MsBrandMaster _b  ON _b.BrandID  = _im.BrandID
              LEFT JOIN MsPartyMaster _pp ON _pp.PartyID = _b.CompanyID
              WHERE _vi.TransTypeID = h.TransTypeID
                AND _vi.VoucherNo   = h.VoucherNo
                AND _vi.FreeItemYN  = 'N'
                AND _vi.ItemID      LIKE 'I%'
              GROUP BY ISNULL(_pp.PartyName,_b.CompanyID)
              ORDER BY SUM(_vi.TotalAmount) DESC
            ) AS Principal
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            {_FY_JOIN}
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate <= ?
        GROUP BY h.VoucherDate, h.VoucherNo, h.TransTypeID
        ORDER BY h.VoucherDate DESC, h.VoucherNo DESC
    """
    df = run_query(sql, (str(end),))
    if not df.empty:
        df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
        df["Cases"]       = pd.to_numeric(df["Cases"],  errors="coerce").fillna(0.0)
        df["Amount"]      = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _section_kpis(kpi: dict, gl: dict) -> None:
    """KPI row uses GL-based totals (matches BS); cases/suppliers stay item-based."""
    tot_delta, tot_color = _yoy_delta(gl["total_cost"], gl["ly_total_cost"])
    exc_delta, exc_color = _yoy_delta(gl["excise_total"], gl["ly_excise_total"])
    c_delta,   c_color   = _yoy_delta(kpi["c"], kpi["ly_c"])

    exc_pct = (gl["excise_total"] / gl["total_cost"] * 100) if gl["total_cost"] else 0

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(_kpi_card(
            "Total Purchase Cost",
            f"₹{gl['total_cost']/1e7:.2f} Cr",
            f"Invoice ₹{gl['purchases']/1e7:.2f} Cr + Excise ₹{gl['excise_total']/1e7:.2f} Cr  ·  {tot_delta} YoY",
            tot_color, _KPI_COLORS[0],
        ), unsafe_allow_html=True)
    with c2:
        st.markdown(_kpi_card(
            "Excise Duty Paid",
            f"₹{gl['excise_total']/1e7:.2f} Cr",
            f"{exc_pct:.1f}% of total  ·  {exc_delta} YoY",
            exc_color, _KPI_COLORS[1],
        ), unsafe_allow_html=True)
    with c3:
        st.markdown(_kpi_card(
            "Cases Purchased",
            f"{kpi['c']:,.2f}",
            f"{c_delta} YoY (LY: {kpi['ly_c']:,.2f})",
            c_color, _KPI_COLORS[2],
        ), unsafe_allow_html=True)
    with c4:
        st.markdown(_kpi_card(
            "Invoices · Suppliers",
            f"{kpi['i']}  ·  {kpi['sup']}",
            f"LY: {kpi['ly_i']} invoices  ·  {kpi['ly_sup']} suppliers",
            "#6b7280", _KPI_COLORS[3],
        ), unsafe_allow_html=True)


def _section_cost_breakdown(gl: dict) -> None:
    """3 cards (Invoice / Excise / Total) + reconciliation table."""
    st.markdown("##### Purchase Cost Breakdown")
    st.caption("Reconciles to Balance Sheet — total cost of goods purchased")

    total = gl["total_cost"] or 1.0  # avoid div0 in % calc

    m1, m2, m3 = st.columns(3)
    with m1:
        pct = gl["purchases"] / total * 100
        st.markdown(_kpi_card(
            "Invoice Value",
            f"₹{gl['purchases']/1e7:.2f} Cr",
            f"{pct:.1f}% of cost  ·  source: PURCHASES - TRADING (GL)",
            "#6b7280", "#1B4F72",
        ), unsafe_allow_html=True)
    with m2:
        pct = gl["excise_total"] / total * 100
        st.markdown(_kpi_card(
            "Excise Duty on Imports",
            f"₹{gl['excise_total']/1e7:.2f} Cr",
            f"{pct:.1f}% of cost  ·  paid directly to govt",
            "#6b7280", "#EF9F27",
        ), unsafe_allow_html=True)
    with m3:
        st.markdown(_kpi_card(
            "Total Cost",
            f"₹{gl['total_cost']/1e7:.2f} Cr",
            "Invoice + Excise (matches Balance Sheet)",
            "#16a34a", "#1D9E75",
        ), unsafe_allow_html=True)

    # Per-account breakdown table
    rows = []
    if gl["purchases"]:
        rows.append({"Account": "PURCHASES - TRADING",
                     "Amount": gl["purchases"], "% of Total": gl["purchases"]/total*100})
    if gl["excise_import"]:
        rows.append({"Account": "EXCISE DUTY ON IMPORT",
                     "Amount": gl["excise_import"], "% of Total": gl["excise_import"]/total*100})
    if gl["excise_diageo"]:
        rows.append({"Account": "EXCISE DUTY ON IMPORT - DIAGEO",
                     "Amount": gl["excise_diageo"], "% of Total": gl["excise_diageo"]/total*100})
    if gl["excise_superv"]:
        rows.append({"Account": "EXCISE SUPERVISION",
                     "Amount": gl["excise_superv"], "% of Total": gl["excise_superv"]/total*100})
    rows.append({"Account": "TOTAL COST", "Amount": gl["total_cost"], "% of Total": 100.0})

    df = pd.DataFrame(rows)

    def _bold_total(row):
        return ["font-weight:700; background-color:#f3f4f6" if row["Account"] == "TOTAL COST" else ""
                for _ in row]

    styled = (
        df.style
        .apply(_bold_total, axis=1)
        .format({"Amount": format_inr, "% of Total": "{:.1f}%"})
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_reconciliation(rec_df: pd.DataFrame) -> None:
    st.markdown("##### Purchase vs Sales Reconciliation")
    st.caption("What we bought vs what we sold by principal — critical for meetings.")

    if rec_df.empty:
        st.info("No data for reconciliation.")
        return

    df = rec_df.copy()
    df["CasesDiff"] = df["BCases"] - df["SCases"]
    df["Margin"]    = df["SVal"]   - df["BVal"]
    df["MarginPct"] = df.apply(
        lambda r: (r["Margin"] / r["SVal"] * 100) if r["SVal"] else 0.0,
        axis=1,
    ).round(1)
    df = df.sort_values("BVal", ascending=False)

    display = df[[
        "Principal", "BCases", "SCases", "CasesDiff",
        "BVal", "SVal", "Margin", "MarginPct",
    ]].rename(columns={
        "BCases":    "Cases Bought",
        "SCases":    "Cases Sold",
        "CasesDiff": "Cases Diff",
        "BVal":      "Purchase ₹",
        "SVal":      "Sales ₹",
        "Margin":    "Margin ₹",
        "MarginPct": "Margin %",
    })

    def _margin_style(val):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return ""
        if v > 0:  return "color:#16a34a; font-weight:600"
        if v < 0:  return "color:#dc2626; font-weight:600"
        return "color:#6b7280;"

    styled = (
        display.style
        .format({
            "Cases Bought": "{:,.2f}", "Cases Sold": "{:,.2f}", "Cases Diff": "{:+,.2f}",
            "Purchase ₹": format_inr, "Sales ₹": format_inr, "Margin ₹": format_inr,
            "Margin %":   "{:.1f}%",
        })
        .map(_margin_style, subset=["Margin %", "Margin ₹"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_principal_breakdown(p_df: pd.DataFrame) -> None:
    col_l, col_r = st.columns([1.2, 1])

    with col_l:
        st.markdown("##### Purchase by Principal")
        if p_df.empty:
            st.info("No principal-level data.")
        else:
            tbl = p_df.sort_values("P", ascending=False).copy()
            tbl["YoY"] = tbl.apply(lambda r: _yoy_delta(r["P"], r["LyP"])[0], axis=1)
            display = tbl[["Principal", "I", "C", "P", "YoY"]].rename(columns={
                "I": "Invoices", "C": "Cases", "P": "Purchase", "YoY": "YoY Growth",
            })
            def _yoy_style(val):
                if val.startswith("↑"): return "color:#16a34a; font-weight:600"
                if val.startswith("↓"): return "color:#dc2626; font-weight:600"
                return "color:#6b7280;"
            styled = (
                display.style
                .format({"Purchase": format_inr, "Cases": "{:,.2f}", "Invoices": "{:,}"})
                .map(_yoy_style, subset=["YoY Growth"])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

    with col_r:
        st.markdown("##### Share of Purchase")
        if p_df.empty:
            st.info("No data.")
        else:
            d = p_df[p_df["P"] > 0].copy()
            if d.empty:
                st.info("All-zero current period.")
            else:
                colors = [_PRINCIPAL_COLOR.get(n, "#B4B2A9") for n in d["Principal"]]
                labels = [
                    f"{p}  ({v/d['P'].sum()*100:.1f}%)"
                    for p, v in zip(d["Principal"], d["P"])
                ]
                fig = go.Figure(go.Pie(
                    labels=labels, values=d["P"], hole=0.55,
                    textinfo="none", marker=dict(colors=colors),
                    hovertemplate="<b>%{label}</b><br>₹%{value:,.0f}<extra></extra>",
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    margin=dict(l=16, r=16, t=8, b=8), height=320,
                    legend=dict(orientation="v", font=dict(size=11),
                                bgcolor="rgba(0,0,0,0)"),
                    annotations=[dict(text="Purchase<br>Mix",
                                      x=0.5, y=0.5,
                                      font=dict(size=13, color="#1a1a1a"),
                                      showarrow=False)],
                )
                st.plotly_chart(fig, use_container_width=True)


def _section_top_brands(brands_df: pd.DataFrame) -> None:
    st.markdown("##### Top 15 brands purchased")
    if brands_df.empty:
        st.info("No brand-level data.")
        return
    df = brands_df.copy()
    df["RevCr"] = df["Purchase"] / 1e7
    df = df.sort_values("RevCr", ascending=True)  # ascending so largest is on top
    colors = [_PRINCIPAL_COLOR.get(p, "#B4B2A9") for p in df["Principal"]]

    fig = go.Figure(go.Bar(
        x=df["RevCr"], y=df["BrandName"], orientation="h",
        marker_color=colors,
        text=[f"₹{v:.2f} Cr" for v in df["RevCr"]],
        textposition="outside",
        customdata=df["Purchase"].apply(format_inr),
        hovertemplate="<b>%{y}</b><br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        template="plotly_white",
        margin=dict(l=16, r=16, t=16, b=16),
        height=max(360, len(df) * 28),
        xaxis=dict(title="Purchase (₹ Cr)", ticksuffix=" Cr", gridcolor="#E8E8E8"),
        yaxis=dict(gridcolor="#E8E8E8"),
    )
    st.plotly_chart(fig, use_container_width=True)


def _section_trend(monthly_df: pd.DataFrame) -> None:
    st.markdown("##### 12-month purchase trend")
    if monthly_df.empty:
        st.info("No monthly data.")
        return
    months_12 = _last_n_month_strs(12)
    df = (
        pd.DataFrame({"Month": months_12})
        .merge(monthly_df, on="Month", how="left")
        .fillna({"Purchase": 0, "Cases": 0})
    )
    today_fy   = _fy_of(_month_str(date.today()))
    df["FY"]   = df["Month"].apply(lambda m: "Current FY" if _fy_of(m) == today_fy else "Previous FY")
    df["RevCr"]    = df["Purchase"] / 1e7
    df["MonthLbl"] = df["Month"].apply(_fmt_month)

    rev_lookup = dict(zip(df["Month"], df["Purchase"]))
    hover_text = []
    for m, rev in zip(df["Month"], df["Purchase"]):
        yr, mo = int(m[:4]), int(m[5:])
        ly_key = f"{yr-1:04d}-{mo:02d}"
        ly_rev = rev_lookup.get(ly_key)
        ly_txt = ""
        if ly_rev is not None and ly_rev > 0:
            pct = (rev - ly_rev) / ly_rev * 100
            sign = "↑" if pct >= 0 else "↓"
            ly_txt = f"<br>{sign} {abs(pct):.1f}% vs {_fmt_month(ly_key)} (₹{ly_rev/1e7:.2f} Cr)"
        hover_text.append(f"<b>{_fmt_month(m)}</b><br>₹{rev/1e7:.2f} Cr{ly_txt}")

    color_map = {"Current FY": "#1B4F72", "Previous FY": "#B5D4F4"}
    fig = go.Figure(go.Bar(
        x=df["MonthLbl"], y=df["RevCr"],
        marker_color=[color_map[fy] for fy in df["FY"]],
        text=[f"₹{v:.2f}" for v in df["RevCr"]],
        textposition="outside",
        customdata=hover_text,
        hovertemplate="%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        template="plotly_white", showlegend=False,
        margin=dict(l=16, r=16, t=16, b=16), height=360,
        xaxis=dict(gridcolor="#E8E8E8"),
        yaxis=dict(gridcolor="#E8E8E8", ticksuffix=" Cr", title="Purchase (₹ Cr)"),
    )
    st.plotly_chart(fig, use_container_width=True)


def _section_recent_vouchers(vouchers_df: pd.DataFrame) -> None:
    st.markdown("##### Recent purchase vouchers")
    st.caption("Latest 50 purchase invoices · sorted by date desc")
    if vouchers_df.empty:
        st.info("No vouchers to display.")
        return
    disp = vouchers_df.copy()
    disp["Date"] = disp["VoucherDate"].dt.strftime("%d %b %Y")
    disp = disp[["Date", "VoucherNo", "Principal", "Cases", "Amount"]].rename(columns={
        "VoucherNo": "Voucher No",
    })
    st.dataframe(
        disp.style.format({"Cases": "{:,.2f}", "Amount": format_inr}),
        use_container_width=True, hide_index=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("Purchase Analytics")
    st.caption("What we buy from principals — volume, value, reconciliation with sales")
    st.divider()

    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

    c1, c2 = st.columns([2, 1])
    with c1:
        date_range = st.date_input(
            "Period",
            value=(fy_start, today),
            key="purchase_dates",
        )
    with c2:
        principal_filter = st.multiselect(
            "Principal",
            options=list(_PRINCIPAL_NAMES.values()),
            default=[],
            key="purchase_principal_filter",
        )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
    else:
        start, end = fy_start, today
    if start > end:
        st.warning("Start date must be before end date.")
        return

    ly_start, ly_end = _shift_year(start, 1), _shift_year(end, 1)
    reverse_map = {v: k for k, v in _PRINCIPAL_NAMES.items()}
    principal_ids: tuple[str, ...] = tuple(
        reverse_map[n] for n in principal_filter if n in reverse_map
    )

    with st.spinner("Loading purchase data…"):
        kpi          = _load_purchase_kpis(start, end, ly_start, ly_end, principal_ids)
        gl           = _load_purchase_gl(start, end, ly_start, ly_end)
        p_df         = _load_purchase_by_principal(start, end, ly_start, ly_end)
        rec_df       = _load_purchase_vs_sales(start, end)
        monthly_df   = _load_purchase_monthly(24, principal_ids)
        brands_df    = _load_top_brands_purchased(start, end, principal_ids)
        vouchers_df  = _load_recent_vouchers(end)

    if gl["total_cost"] == 0 and gl["ly_total_cost"] == 0 and kpi["p"] == 0:
        st.warning("No purchase data for the selected period or filters.")
        return

    # Apply principal filter to item-level data sets (GL is not principal-aware)
    if principal_ids:
        p_df   = p_df[p_df["CompanyID"].isin(principal_ids)]
        rec_df = rec_df[rec_df["CompanyID"].isin(principal_ids)]

    _section_kpis(kpi, gl)
    st.divider()
    _section_cost_breakdown(gl)
    st.divider()
    st.caption(
        "Brand, principal, and reconciliation breakdowns below show "
        "**invoice value only** (item-level, excludes excise paid to govt). "
        "Total cost including excise is in the header above."
    )
    _section_reconciliation(rec_df)
    st.divider()
    _section_principal_breakdown(p_df)
    st.divider()
    _section_top_brands(brands_df)
    st.divider()
    _section_trend(monthly_df)
    st.divider()
    _section_recent_vouchers(vouchers_df)
