"""src/sales.py — Sales Overview (CFO lens).

Revenue, growth, year-on-year performance.  Designed to answer the
two questions a CFO asks first: "How are we doing this period?" and
"How does that compare to last year and the recent trend?"
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from dateutil.relativedelta import relativedelta
except ImportError:  # fallback if dateutil missing
    relativedelta = None

from db import run_query
from utils.helpers import (
    format_inr,
    CASES_SQL_EXPR as _CASES,
    same_mtd_window,
    mtd_sum_in_window,
    mtd_label,
)

# ── Sales transaction type IDs ──────────────────────────────────────────────
SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

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


# ═══════════════════════════════════════════════════════════════════════════════
# DATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _shift_year(d: date, years: int) -> date:
    """Shift a date back by N years; handles Feb-29 by clamping."""
    if relativedelta:
        return d - relativedelta(years=years)
    try:
        return d.replace(year=d.year - years)
    except ValueError:        # Feb 29 in non-leap year
        return d.replace(year=d.year - years, day=28)


def _month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _fmt_month(ym: str) -> str:
    return date(int(ym[:4]), int(ym[5:]), 1).strftime("%b %y")


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


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADERS  (one query covers current + LY periods via conditional sums)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_period_kpis(start: date, end: date,
                      ly_start: date, ly_end: date,
                      principal_ids: tuple[str, ...]) -> dict:
    """KPIs for current period AND same-period-last-year, in one query."""
    type_ph     = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS Rev,
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS LyRev,
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS Cases,
          SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS LyCases,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN h.VoucherNo END) AS Invoices,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN h.VoucherNo END) AS LyInvoices,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN d.PartyID END) AS Cust,
          COUNT(DISTINCT CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN d.PartyID END) AS LyCust
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
                           ORDER BY id_key DESC
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
    """
    s, e, ls, le = str(start), str(end), str(ly_start), str(ly_end)
    params = (
        s, e,    ls, le,           # Rev, LyRev
        s, e,    ls, le,           # Cases, LyCases
        s, e,    ls, le,           # Invoices, LyInvoices
        s, e,    ls, le,           # Cust, LyCust
        ls, e,                     # outer date range
    ) + company_params
    df = run_query(sql, params)
    if df.empty:
        return {"rev": 0, "ly_rev": 0, "cases": 0, "ly_cases": 0,
                "invoices": 0, "ly_invoices": 0, "cust": 0, "ly_cust": 0}
    r = df.iloc[0]
    return {
        "rev":         float(r["Rev"]      or 0),
        "ly_rev":      float(r["LyRev"]    or 0),
        "cases":       float(r["Cases"]    or 0),
        "ly_cases":    float(r["LyCases"]  or 0),
        "invoices":    int(r["Invoices"]   or 0),
        "ly_invoices": int(r["LyInvoices"] or 0),
        "cust":        int(r["Cust"]       or 0),
        "ly_cust":     int(r["LyCust"]     or 0),
    }


@st.cache_data(ttl=3600, show_spinner=False)
def _load_monthly_revenue(months: int = 24,
                          principal_ids: tuple[str, ...] = ()) -> pd.DataFrame:
    """Per-month revenue for last `months` months (used by trend chart + benchmarks)."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
            FORMAT(h.VoucherDate, 'yyyy-MM') AS Month,
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
        df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
        df["Cases"]   = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_daily_revenue(months: int = 24,
                        principal_ids: tuple[str, ...] = ()) -> pd.DataFrame:
    """Per-DAY revenue for the last `months` months. Used by MTD-to-MTD
    benchmark cards so the day cutoff can be set dynamically from
    today.day rather than baked into the SQL aggregation level.
    """
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
            CAST(h.VoucherDate AS date)        AS BillDate,
            SUM(CAST(vi.TotalAmount AS FLOAT)) AS Revenue,
            SUM({_CASES})                      AS Cases
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
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          {company_sql}
        GROUP BY CAST(h.VoucherDate AS date)
        ORDER BY BillDate
    """
    df = run_query(sql, company_params)
    if df.empty:
        return pd.DataFrame(columns=["BillDate", "Revenue", "Cases"])
    df["BillDate"] = pd.to_datetime(df["BillDate"])
    df["Revenue"]  = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
    df["Cases"]    = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_party_daily_revenue(months: int = 14,
                              principal_ids: tuple[str, ...] = ()) -> pd.DataFrame:
    """Per-(party, day) revenue used by the 'Parties Pulling Us Down'
    section. Pulls the last `months` calendar months (default 14 — gives
    13 prior months + the current MTD so LYSM windows resolve).

    Returns columns: PartyID, PartyName, LicenseTypeID, AcType3ID,
    SalesManID, SalesManID1, SalesManID2, BillDate, Revenue.
    """
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
            d.PartyID,
            ISNULL(p.PartyName, '(unknown)')           AS PartyName,
            ISNULL(p.LicenseTypeID, '')                AS LicenseTypeID,
            ISNULL(p.AcType3ID, '')                    AS AcType3ID,
            ISNULL(p.SalesManID, '')                   AS SalesManID,
            ISNULL(p.SalesManID1, '')                  AS SalesManID1,
            ISNULL(p.SalesManID2, '')                  AS SalesManID2,
            CAST(h.VoucherDate AS date)                AS BillDate,
            SUM(CAST(vi.TotalAmount AS FLOAT))         AS Revenue
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
            SELECT TransTypeID, VoucherNo, PartyID FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo
                                          ORDER BY id_key DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator='D' AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d  ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        LEFT JOIN MsPartyMaster p  ON p.PartyID  = d.PartyID
        JOIN MsItemMaster   im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster  b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          {company_sql}
        GROUP BY d.PartyID, p.PartyName, p.LicenseTypeID, p.AcType3ID,
                 p.SalesManID, p.SalesManID1, p.SalesManID2,
                 CAST(h.VoucherDate AS date)
    """
    df = run_query(sql, company_params)
    if df.empty:
        return pd.DataFrame(columns=[
            "PartyID","PartyName","LicenseTypeID","AcType3ID",
            "SalesManID","SalesManID1","SalesManID2",
            "BillDate","Revenue",
        ])
    df["BillDate"] = pd.to_datetime(df["BillDate"])
    df["Revenue"]  = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_principal_growth(start: date, end: date,
                           ly_start: date, ly_end: date) -> pd.DataFrame:
    """Per-principal revenue + cases for current period and LY."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            b.CompanyID,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS Rev,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS LyRev,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS Cases,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS LyCases
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
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          AND b.CompanyID IN ('C00025','C00040','C00039','C00056')
        GROUP BY b.CompanyID
    """
    s, e, ls, le = str(start), str(end), str(ly_start), str(ly_end)
    params = (s, e, ls, le, s, e, ls, le, ls, e)
    df = run_query(sql, params)
    if not df.empty:
        df["Rev"]     = pd.to_numeric(df["Rev"],     errors="coerce").fillna(0.0)
        df["LyRev"]   = pd.to_numeric(df["LyRev"],   errors="coerce").fillna(0.0)
        df["Cases"]   = pd.to_numeric(df["Cases"],   errors="coerce").fillna(0.0)
        df["LyCases"] = pd.to_numeric(df["LyCases"], errors="coerce").fillna(0.0)
        df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_channel_revenue(start: date, end: date,
                          principal_ids: tuple[str, ...]) -> pd.DataFrame:
    """Per-channel revenue using LicenseType+AcType3 logic (mirrors salesman.py)."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT
            CASE
              WHEN p.LicenseTypeID = '180001'                                THEN 'FL-II Wine Shop'
              WHEN p.LicenseTypeID = '180004'                                THEN 'FL-BR-II Beer Shopee'
              WHEN p.LicenseTypeID = '180005'                                THEN 'FL-IV Club'
              WHEN p.LicenseTypeID = '180007'                                THEN 'FL-IV One Day'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130004'    THEN 'FL-III KW Institution'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130002'    THEN 'FL-III PCMC Institution'
              WHEN p.LicenseTypeID = '180002'                                THEN 'FL-III Permit Room'
              WHEN p.AcType3ID     = '130007'                                THEN 'Beer Cross Supply'
              ELSE 'Other'
            END AS Channel,
            SUM(CAST(vi.TotalAmount AS FLOAT)) AS Revenue
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
                           ORDER BY id_key DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator = 'D'
                  AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        LEFT JOIN MsPartyMaster p  ON p.PartyID = d.PartyID
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          {company_sql}
        GROUP BY
            CASE
              WHEN p.LicenseTypeID = '180001'                                THEN 'FL-II Wine Shop'
              WHEN p.LicenseTypeID = '180004'                                THEN 'FL-BR-II Beer Shopee'
              WHEN p.LicenseTypeID = '180005'                                THEN 'FL-IV Club'
              WHEN p.LicenseTypeID = '180007'                                THEN 'FL-IV One Day'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130004'    THEN 'FL-III KW Institution'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130002'    THEN 'FL-III PCMC Institution'
              WHEN p.LicenseTypeID = '180002'                                THEN 'FL-III Permit Room'
              WHEN p.AcType3ID     = '130007'                                THEN 'Beer Cross Supply'
              ELSE 'Other'
            END
        ORDER BY Revenue DESC
    """
    df = run_query(sql, (str(start), str(end)) + company_params)
    if not df.empty:
        df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_brand_growth(start: date, end: date,
                       ly_start: date, ly_end: date,
                       principal_ids: tuple[str, ...]) -> pd.DataFrame:
    """Brand-level revenue + cases for current and LY, top 30."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    company_sql = ""
    company_params: tuple = ()
    if principal_ids:
        company_sql = f"AND b.CompanyID IN ({','.join('?' * len(principal_ids))})"
        company_params = principal_ids

    sql = f"""
        SELECT TOP 30
            b.BrandName,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS Rev,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN vi.TotalAmount ELSE 0 END) AS LyRev,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS Cases,
            SUM(CASE WHEN h.VoucherDate BETWEEN ? AND ? THEN {_CASES} ELSE 0 END) AS LyCases
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
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.VoucherDate BETWEEN ? AND ?
          {company_sql}
        GROUP BY b.BrandName
        ORDER BY Rev DESC
    """
    s, e, ls, le = str(start), str(end), str(ly_start), str(ly_end)
    params = (s, e, ls, le, s, e, ls, le, ls, e) + company_params
    df = run_query(sql, params)
    if not df.empty:
        for c in ("Rev", "LyRev"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        for c in ("Cases", "LyCases"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _yoy_delta(curr: float, prev: float) -> tuple[str, str]:
    """Return ('↑ 12.3%', color) — green/red/gray text style."""
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
    <div style='background:#fff; border:1px solid #e5e7eb; border-radius:8px;
                border-left:4px solid {accent}; padding:12px 16px;
                box-shadow:0 1px 2px rgba(0,0,0,0.04);'>
        <div style='font-size:0.7rem; color:#6b7280;
                    text-transform:uppercase; letter-spacing:0.05em'>{label}</div>
        <div style='font-size:1.55rem; font-weight:700; color:#111827;
                    margin-top:4px; line-height:1.15'>{value}</div>
        <div style='font-size:0.78rem; color:{delta_color};
                    margin-top:2px; font-weight:600'>{delta_text}</div>
    </div>
    """


def _bench_card(title: str, ref_label: str, curr: float, ref: float,
                color_accent: str) -> str:
    delta_text, delta_color = _yoy_delta(curr, ref)
    return f"""
    <div style='background:#fff; border:1px solid #e5e7eb; border-radius:8px;
                border-left:4px solid {color_accent}; padding:14px 16px;'>
        <div style='font-size:0.78rem; color:#6b7280;
                    text-transform:uppercase; letter-spacing:0.04em'>{title}</div>
        <div style='font-size:1.4rem; font-weight:700; color:#111827; margin-top:4px'>
            ₹{curr/1e7:.2f} Cr
        </div>
        <div style='font-size:0.78rem; color:{delta_color}; font-weight:600;
                    margin-top:2px'>{delta_text} vs {ref_label}</div>
        <div style='font-size:0.72rem; color:#6b7280; margin-top:2px'>
            {ref_label}: ₹{ref/1e7:.2f} Cr
        </div>
    </div>
    """


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _section_kpis(kpi: dict) -> None:
    rev_delta,   rev_color   = _yoy_delta(kpi["rev"],      kpi["ly_rev"])
    case_delta,  case_color  = _yoy_delta(kpi["cases"],    kpi["ly_cases"])
    cust_delta,  cust_color  = _yoy_delta(kpi["cust"],     kpi["ly_cust"])

    avg_inv    = (kpi["rev"]    / kpi["invoices"])    if kpi["invoices"]    else 0
    ly_avg_inv = (kpi["ly_rev"] / kpi["ly_invoices"]) if kpi["ly_invoices"] else 0
    inv_delta, inv_color = _yoy_delta(avg_inv, ly_avg_inv)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(_kpi_card(
            "Revenue This Period",
            f"₹{kpi['rev']/1e7:.2f} Cr",
            f"{rev_delta} vs LY (₹{kpi['ly_rev']/1e7:.2f} Cr)",
            rev_color, _KPI_COLORS[0],
        ), unsafe_allow_html=True)
    with c2:
        st.markdown(_kpi_card(
            "Cases Sold",
            f"{kpi['cases']:,.2f}",
            f"{case_delta} YoY (LY: {kpi['ly_cases']:,.2f})",
            case_color, _KPI_COLORS[1],
        ), unsafe_allow_html=True)
    with c3:
        # Indian comma grouping for invoice value
        st.markdown(_kpi_card(
            "Avg Invoice Value",
            f"₹{avg_inv:,.0f}",
            f"{inv_delta} YoY (LY: ₹{ly_avg_inv:,.0f})",
            inv_color, _KPI_COLORS[2],
        ), unsafe_allow_html=True)
    with c4:
        st.markdown(_kpi_card(
            "Active Customers",
            f"{kpi['cust']:,}",
            f"{cust_delta} YoY (LY: {kpi['ly_cust']:,})",
            cust_color, _KPI_COLORS[3],
        ), unsafe_allow_html=True)


def _section_benchmarks(daily_df: pd.DataFrame) -> None:
    """3 cards: MTD-this-month vs SAME-MTD-window in (a) LY same month,
    (b) trailing-3-month average, (c) trailing-6-month average.

    The day cutoff is derived from today.day every render — no hard-coded
    day numbers. On May-19 each card compares May 1-19 ranges; on
    May-31 each card compares full months.

    Fixes the previous apples-to-oranges bug where current partial
    month was being compared to FULL prior periods (which made the
    comparison spuriously look -45% to -90% mid-month).
    """
    st.markdown("##### This month vs benchmarks  ·  *MTD-to-MTD comparison*")
    if daily_df.empty:
        st.info("Insufficient daily data to compute benchmarks.")
        return

    today = pd.Timestamp.today().normalize()
    label = mtd_label(today)
    cur_start, cur_end = today.replace(day=1), today

    # Current MTD
    curr_rev = mtd_sum_in_window(daily_df, "BillDate", "Revenue",
                                 cur_start, cur_end)

    if curr_rev <= 0:
        st.info(f"No revenue yet for {today.strftime('%b %Y')} "
                f"(window: {label}).")
        return

    # vs Same Month LY (12 months back, same day range)
    ly_start, ly_end = same_mtd_window(today, months_back=12)
    ly_rev = mtd_sum_in_window(daily_df, "BillDate", "Revenue",
                               ly_start, ly_end)

    # Trailing 3 and 6 months — average of each same-MTD window
    prev_3_mtds = [
        mtd_sum_in_window(daily_df, "BillDate", "Revenue",
                          *same_mtd_window(today, months_back=i))
        for i in range(1, 4)
    ]
    prev_6_mtds = [
        mtd_sum_in_window(daily_df, "BillDate", "Revenue",
                          *same_mtd_window(today, months_back=i))
        for i in range(1, 7)
    ]
    avg_3 = sum(prev_3_mtds) / max(len(prev_3_mtds), 1)
    avg_6 = sum(prev_6_mtds) / max(len(prev_6_mtds), 1)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(_bench_card(
            f"{label} vs Same Month LY",
            f"LY {ly_start.strftime('%b')} {label}",
            curr_rev, ly_rev, "#1B4F72",
        ), unsafe_allow_html=True)
    with c2:
        st.markdown(_bench_card(
            f"{label} vs 3-mo avg ({label})",
            "3-mo avg MTD", curr_rev, avg_3, "#378ADD",
        ), unsafe_allow_html=True)
    with c3:
        st.markdown(_bench_card(
            f"{label} vs 6-mo avg ({label})",
            "6-mo avg MTD", curr_rev, avg_6, "#1D9E75",
        ), unsafe_allow_html=True)
    st.caption(
        f"All three cards compare the **first {today.day} days** of "
        f"each month — apples to apples. Auto-updates daily."
    )


def _fy_of(ym: str) -> int:
    """Return the financial-year start year for a 'yyyy-MM' string (Apr-Mar)."""
    y, m = int(ym[:4]), int(ym[5:])
    return y if m >= 4 else y - 1


def _compute_party_pulling_down(
    party_daily: pd.DataFrame,
    today: pd.Timestamp,
) -> pd.DataFrame:
    """Per-party MTD vs L3M-avg-MTD vs LYSM-MTD comparison.

    Returns one row per party with `meaningful baseline`:
      L3M_Avg_MTD > Rs1k  OR  LYSM_MTD > Rs1k.
    Filtering decisions (min drop %, min baseline) are layered on top
    by the calling section; this function just produces the universe.
    """
    if party_daily.empty:
        return pd.DataFrame()

    day = today.day
    cur_start, cur_end = today.replace(day=1), today

    # Helper: revenue for a (party, month_start, day_cap) window
    def window_sum(start: pd.Timestamp, day_cap: int) -> pd.Series:
        _, last_dom = calendar.monthrange(start.year, start.month) \
            if False else (None, calendar.monthrange(start.year, start.month)[1])
        end_day = min(day_cap, last_dom)
        end = start.replace(day=end_day)
        mask = (party_daily["BillDate"] >= start) & (party_daily["BillDate"] <= end)
        return (party_daily.loc[mask].groupby("PartyID")["Revenue"].sum())

    cur     = window_sum(cur_start, day)
    l3_wins = [window_sum(cur_start - pd.DateOffset(months=i), day)
               for i in range(1, 4)]
    lysm    = window_sum(cur_start - pd.DateOffset(years=1), day)

    # Months active in L12M (consistency proxy)
    one_yr_ago = cur_start - pd.DateOffset(months=12)
    active = (
        party_daily.loc[
            (party_daily["BillDate"] >= one_yr_ago)
            & (party_daily["BillDate"] < cur_start)
        ]
        .assign(MonthStamp=lambda d: d["BillDate"].dt.to_period("M"))
        .groupby("PartyID")["MonthStamp"].nunique()
    )

    # Static columns per party (PartyName, channel, SM fields)
    info = (
        party_daily.sort_values("BillDate")
        .groupby("PartyID")
        .agg(PartyName=("PartyName", "first"),
             LicenseTypeID=("LicenseTypeID", "first"),
             AcType3ID=("AcType3ID", "first"),
             SalesManID=("SalesManID", "first"),
             SalesManID1=("SalesManID1", "first"),
             SalesManID2=("SalesManID2", "first"))
    )

    # Assemble
    all_ids = (set(cur.index) | set(lysm.index)
               | set().union(*[set(w.index) for w in l3_wins]))
    if not all_ids:
        return pd.DataFrame()

    rows = []
    for pid in all_ids:
        if pid not in info.index:
            continue
        ir = info.loc[pid]
        c  = float(cur.get(pid, 0.0))
        l3 = float(sum(w.get(pid, 0.0) for w in l3_wins) / 3.0)
        ly = float(lysm.get(pid, 0.0))
        # Skip parties without meaningful baseline
        if l3 < 1000 and ly < 1000:
            continue
        rows.append({
            "PartyID":            pid,
            "PartyName":          ir["PartyName"],
            "LicenseTypeID":      ir["LicenseTypeID"],
            "AcType3ID":          ir["AcType3ID"],
            "SalesManID":         ir["SalesManID"],
            "SalesManID1":        ir["SalesManID1"],
            "SalesManID2":        ir["SalesManID2"],
            "MonthsActiveL12M":   int(active.get(pid, 0)),
            "CurrentMTD":         c,
            "L3M_Avg_MTD":        l3,
            "LYSM_MTD":           ly,
            "Gap_L3M":            c - l3,
            "Gap_LYSM":           c - ly,
            "Pct_L3M":            ((c - l3) / l3 * 100) if l3 > 0 else 0.0,
            "Pct_LYSM":           ((c - ly) / ly * 100) if ly > 0 else 0.0,
        })
    return pd.DataFrame(rows)


def _section_parties_pulling_down(
    party_daily: pd.DataFrame,
    section_key: str = "sales",
) -> None:
    """Outlets where current MTD trails L3M-avg or LYSM materially.

    The L3M / LYSM windows use the same day cutoff (today.day) as the
    benchmarks section above — apples-to-apples by construction.

    `section_key` lets the same function be reused inside the Meeting
    Pack (principal.py) with separate Streamlit widget keys.
    """
    st.subheader("🔻 Parties Pulling Us Down")
    today = pd.Timestamp.today().normalize()
    st.caption(
        f"Outlets where current MTD ({mtd_label(today)}) is materially "
        "behind their L3M-avg MTD or LYSM (Last Year Same Month) MTD. "
        "Sorted by biggest decline. Use this for daily follow-up calls "
        "by salesman."
    )

    perf = _compute_party_pulling_down(party_daily, today)
    if perf.empty:
        st.info("No party data available yet for this period."); return

    # Filter knobs
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        benchmark = st.radio(
            "Compare against",
            ["L3M avg", "LYSM", "Both (worst case)"],
            horizontal=True,
            key=f"{section_key}_pp_bench",
        )
    with c2:
        min_drop_pct = st.slider(
            "Min decline %", min_value=10, max_value=80, value=25, step=5,
            key=f"{section_key}_pp_drop",
            help="Only show parties down by at least this %.",
        )
    with c3:
        min_baseline = st.number_input(
            "Min baseline (₹ Lakhs)", min_value=0.1, value=1.0, step=0.5,
            key=f"{section_key}_pp_base",
            help="Filter out parties with insignificant historical revenue.",
        )

    drop_threshold = -float(min_drop_pct)
    min_base_inr   = float(min_baseline) * 1e5

    if benchmark == "L3M avg":
        mask = (perf["Pct_L3M"] < drop_threshold) & (perf["L3M_Avg_MTD"] >= min_base_inr)
        filt = perf[mask].sort_values("Gap_L3M").head(50).copy()
    elif benchmark == "LYSM":
        mask = (perf["Pct_LYSM"] < drop_threshold) & (perf["LYSM_MTD"] >= min_base_inr)
        filt = perf[mask].sort_values("Gap_LYSM").head(50).copy()
    else:  # Both
        mask = (
            ((perf["Pct_L3M"] < drop_threshold) | (perf["Pct_LYSM"] < drop_threshold))
            & ((perf["L3M_Avg_MTD"] >= min_base_inr) | (perf["LYSM_MTD"] >= min_base_inr))
        )
        filt = perf[mask].copy()
        filt["WorstGap"] = filt[["Gap_L3M", "Gap_LYSM"]].min(axis=1)
        filt = filt.sort_values("WorstGap").head(50)

    if filt.empty:
        st.success("✅ No parties significantly down at the current filter."); return

    # Status badge
    def _status(row: pd.Series) -> str:
        if row["CurrentMTD"] == 0 and row["MonthsActiveL12M"] >= 9:
            return "⚫ Stopped buying"
        if row["Pct_L3M"] < -50 or row["Pct_LYSM"] < -50:
            return "🔴 Severe drop"
        if row["Pct_L3M"] < -25 or row["Pct_LYSM"] < -25:
            return "🟡 Watch"
        return "🟢 Slight dip"
    filt["Status"] = filt.apply(_status, axis=1)

    # Salesman attribution: first-non-empty across SM1/SM2/SM3
    sm_master_rows = run_query("""
        SELECT ISNULL(SalesManID,'') AS SalesManID,
               ISNULL(FullName, LTRIM(RTRIM(
                   ISNULL(FirstName,'') + ' ' + ISNULL(LastName,'')))) AS FullName
        FROM MsSalesmanMaster
    """)
    sm_dict = dict(zip(sm_master_rows["SalesManID"], sm_master_rows["FullName"])) \
        if not sm_master_rows.empty else {}

    def _primary_sm(row: pd.Series) -> str:
        for col in ("SalesManID", "SalesManID1", "SalesManID2"):
            sid = (row.get(col) or "").strip()
            if sid:
                return sid
        return ""
    filt["SalesmanID"] = filt.apply(_primary_sm, axis=1)
    filt["Salesman"]   = filt["SalesmanID"].map(sm_dict).fillna("(Unmapped)")
    filt.loc[filt["Salesman"].str.strip() == "", "Salesman"] = "(Unmapped)"

    # Headline KPIs
    total_gap_l3m = float(filt["Gap_L3M"].sum()) / 1e5
    total_gap_lysm = float(filt["Gap_LYSM"].sum()) / 1e5
    k1, k2, k3 = st.columns(3)
    k1.metric("Parties down", f"{len(filt):,}")
    k2.metric("Total gap vs L3M",
              f"₹{total_gap_l3m:,.1f} L",
              delta_color="inverse")
    k3.metric("Total gap vs LYSM",
              f"₹{total_gap_lysm:,.1f} L",
              delta_color="inverse")

    # Worklist table — preformatted
    work = filt.copy()
    work["CurrentMTD_L"] = work["CurrentMTD"]  / 1e5
    work["L3M_L"]        = work["L3M_Avg_MTD"] / 1e5
    work["LYSM_L"]       = work["LYSM_MTD"]    / 1e5
    work["Gap_L3M_L"]    = work["Gap_L3M"]     / 1e5
    work["Gap_LYSM_L"]   = work["Gap_LYSM"]    / 1e5

    cols = ["Status", "PartyName", "Salesman",
            "CurrentMTD_L", "L3M_L", "Gap_L3M_L", "Pct_L3M",
            "LYSM_L", "Gap_LYSM_L", "Pct_LYSM",
            "MonthsActiveL12M"]
    rename = {
        "Status":           "Status",
        "PartyName":        "Party",
        "Salesman":         "Salesman",
        "CurrentMTD_L":     "MTD (₹L)",
        "L3M_L":            "L3M avg MTD (₹L)",
        "Gap_L3M_L":        "Gap L3M (₹L)",
        "Pct_L3M":          "vs L3M %",
        "LYSM_L":           "LYSM MTD (₹L)",
        "Gap_LYSM_L":       "Gap LYSM (₹L)",
        "Pct_LYSM":         "vs LYSM %",
        "MonthsActiveL12M": "Active mo (L12M)",
    }
    disp = work[cols].rename(columns=rename)
    styled = (
        disp.style.format({
            "MTD (₹L)":        "₹{:.2f} L",
            "L3M avg MTD (₹L)": "₹{:.2f} L",
            "Gap L3M (₹L)":    "₹{:+.2f} L",
            "vs L3M %":        "{:+.1f}%",
            "LYSM MTD (₹L)":   "₹{:.2f} L",
            "Gap LYSM (₹L)":   "₹{:+.2f} L",
            "vs LYSM %":       "{:+.1f}%",
            "Active mo (L12M)": "{:.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=480)

    # Salesman workload
    st.markdown("**Recovery workload by salesman**")
    by_sm = (
        filt.groupby("Salesman", observed=True)
            .agg(Parties=("PartyID", "nunique"),
                 TotalGap_L3M=("Gap_L3M", "sum"),
                 TotalGap_LYSM=("Gap_LYSM", "sum"))
            .reset_index()
            .sort_values("TotalGap_L3M")   # most-negative-first
    )
    by_sm["Gap_L3M_L"]  = by_sm["TotalGap_L3M"]  / 1e5
    by_sm["Gap_LYSM_L"] = by_sm["TotalGap_LYSM"] / 1e5
    sm_styled = (
        by_sm[["Salesman", "Parties", "Gap_L3M_L", "Gap_LYSM_L"]].style.format({
            "Parties":    "{:,}",
            "Gap_L3M_L":  "₹{:+,.1f} L",
            "Gap_LYSM_L": "₹{:+,.1f} L",
        })
    )
    st.dataframe(sm_styled, use_container_width=True, hide_index=True)

    # CSV
    export = filt[[
        "PartyID", "PartyName", "Salesman", "Status",
        "CurrentMTD", "L3M_Avg_MTD", "LYSM_MTD",
        "Gap_L3M", "Gap_LYSM", "Pct_L3M", "Pct_LYSM",
        "MonthsActiveL12M",
    ]]
    csv = export.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download recovery list (CSV)", csv,
        f"parties_pulling_down_{date.today()}.csv", "text/csv",
        key=f"{section_key}_pp_dl",
    )


def _section_trend(monthly_df: pd.DataFrame) -> None:
    st.markdown("##### 12-month revenue trend")
    if monthly_df.empty:
        st.info("No monthly data available.")
        return

    months_12 = _last_n_month_strs(12)
    df = (
        pd.DataFrame({"Month": months_12})
        .merge(monthly_df, on="Month", how="left")
        .fillna({"Revenue": 0, "Cases": 0})
    )
    today_fy   = _fy_of(_month_str(date.today()))
    df["FY"]   = df["Month"].apply(lambda m: "Current FY" if _fy_of(m) == today_fy else "Previous FY")
    df["RevCr"]   = df["Revenue"] / 1e7
    df["MonthLbl"] = df["Month"].apply(_fmt_month)

    # Build a hover-text column with YoY annotation
    rev_lookup = dict(zip(df["Month"], df["Revenue"]))
    hover_text = []
    for m, rev in zip(df["Month"], df["Revenue"]):
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
        x=df["MonthLbl"],
        y=df["RevCr"],
        marker_color=[color_map[fy] for fy in df["FY"]],
        text=[f"₹{v:.2f}" for v in df["RevCr"]],
        textposition="outside",
        customdata=hover_text,
        hovertemplate="%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        template="plotly_white",
        height=360,
        margin=dict(l=16, r=16, t=16, b=16),
        showlegend=False,
        xaxis=dict(gridcolor="#E8E8E8"),
        yaxis=dict(gridcolor="#E8E8E8", ticksuffix=" Cr", title="Revenue (₹ Cr)"),
    )
    st.plotly_chart(fig, use_container_width=True)


def _section_breakdown(principal_df: pd.DataFrame,
                       channel_df: pd.DataFrame) -> None:
    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown("##### Revenue by Principal")
        if principal_df.empty:
            st.info("No principal-level data for this period.")
        else:
            tbl = principal_df.sort_values("Rev", ascending=False).copy()
            tbl["YoY"] = tbl.apply(
                lambda r: _yoy_delta(r["Rev"], r["LyRev"])[0], axis=1
            )
            display = tbl[["Principal", "Cases", "Rev", "YoY"]].rename(columns={
                "Rev": "Revenue", "YoY": "YoY Growth",
            })

            def _yoy_style(val):
                if val.startswith("↑"):
                    return "color:#16a34a; font-weight:600"
                if val.startswith("↓"):
                    return "color:#dc2626; font-weight:600"
                return "color:#6b7280; font-weight:500"

            styled = (
                display.style
                .format({"Revenue": format_inr, "Cases": "{:,.2f}"})
                .map(_yoy_style, subset=["YoY Growth"])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

    with col_r:
        st.markdown("##### Revenue by Channel")
        if channel_df.empty:
            st.info("No channel-level data for this period.")
        else:
            total = channel_df["Revenue"].sum()
            tbl = channel_df.copy()
            tbl["% of Total"] = (tbl["Revenue"] / total * 100).round(1)
            tbl = tbl[["Channel", "Revenue", "% of Total"]]
            styled = tbl.style.format({
                "Revenue":     format_inr,
                "% of Total":  "{:.1f}%",
            })
            st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_brand_growth(brands_df: pd.DataFrame) -> None:
    st.markdown("##### Top 10 brands — performance and growth")
    if brands_df.empty:
        st.info("No brand-level data for this period.")
        return

    top = brands_df.head(10).copy()
    top.insert(0, "Rank", range(1, len(top) + 1))
    top["YoY Cases %"]   = top.apply(lambda r: _yoy_delta(r["Cases"], r["LyCases"])[0], axis=1)
    top["YoY Revenue %"] = top.apply(lambda r: _yoy_delta(r["Rev"],   r["LyRev"])[0],   axis=1)

    display = top[["Rank", "BrandName", "Cases", "Rev",
                   "YoY Cases %", "YoY Revenue %"]].rename(columns={
        "BrandName": "Brand", "Rev": "Revenue",
    })

    def _yoy_style(val):
        if val.startswith("↑"):
            return "color:#16a34a; font-weight:600"
        if val.startswith("↓"):
            return "color:#dc2626; font-weight:600"
        return "color:#6b7280; font-weight:500"

    styled = (
        display.style
        .format({"Revenue": format_inr, "Cases": "{:,.2f}"})
        .map(_yoy_style, subset=["YoY Cases %", "YoY Revenue %"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("Sales Overview")
    st.caption("CFO view — revenue, growth, and year-on-year performance")
    st.divider()

    # ── Filter bar ──
    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

    c1, c2 = st.columns([2, 1])
    with c1:
        date_range = st.date_input(
            "Analysis period",
            value=(fy_start, today),
            key="sales_dates",
        )
    with c2:
        principal_filter = st.multiselect(
            "Principal",
            options=list(_PRINCIPAL_NAMES.values()),
            default=[],
            key="sales_principal_filter",
        )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
    else:
        start, end = fy_start, today
    if start > end:
        st.warning("Start date must be before end date.")
        return

    ly_start, ly_end = _shift_year(start, 1), _shift_year(end, 1)

    # Map principal names → company IDs
    reverse_map = {v: k for k, v in _PRINCIPAL_NAMES.items()}
    principal_ids: tuple[str, ...] = tuple(
        reverse_map[name] for name in principal_filter if name in reverse_map
    )

    # ── Load data ──
    with st.spinner("Loading sales data…"):
        kpi          = _load_period_kpis(start, end, ly_start, ly_end, principal_ids)
        monthly_df   = _load_monthly_revenue(24, principal_ids)
        # Daily revenue: feeds the MTD-to-MTD benchmark cards (need
        # day-level granularity so today.day drives the cutoff).
        daily_df     = _load_daily_revenue(24, principal_ids)
        # Per-party daily: feeds the Parties Pulling Down section
        # (14 months covers LYSM + L3M windows).
        party_daily  = _load_party_daily_revenue(14, principal_ids)
        principal_df = _load_principal_growth(start, end, ly_start, ly_end)
        channel_df   = _load_channel_revenue(start, end, principal_ids)
        brands_df    = _load_brand_growth(start, end, ly_start, ly_end, principal_ids)

    if kpi["rev"] == 0 and kpi["ly_rev"] == 0:
        st.warning("No sales data for the selected period or filters.")
        return

    # ── Sections ──
    _section_kpis(kpi)
    st.divider()
    _section_benchmarks(daily_df)
    st.divider()
    _section_parties_pulling_down(party_daily, section_key="sales")
    st.divider()
    _section_trend(monthly_df)
    st.divider()
    _section_breakdown(principal_df, channel_df)
    st.divider()
    _section_brand_growth(brands_df)
