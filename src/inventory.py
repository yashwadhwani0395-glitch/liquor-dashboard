"""src/inventory.py — Inventory analytics (ERP-Stock-Balance aligned).

Stock source: MsItemBatchOpening (live snapshot) + TrVocItem movements
for historical roll-back, with the same FY-CASE filter we use everywhere
to drop duplicate FY-tagged rows.

Cost source: MsItemMaster.ValuationCaseRate — the ERP's maintained
landed-cost rate that already includes state excise for Daman variants
(verified to match ERP stock-value report within ~1%).

Movement classification: MsTransType.QtyInOut (I = inward, O = outward).
"""
from __future__ import annotations

import calendar
import json
import os
import sys
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import (format_inr, CASES_SQL_EXPR as _CASES, safe_section,
                          cases_sql, keg_mode_toggle)

# ERP Stock & Sale baseline — anchors live stock to the ERP's authoritative
# closing, then rolls forward with live movements (the ERP's per-item opening
# stock isn't stored in any accessible table, so we can't compute it purely
# live; this baseline is refreshed by dropping in a newer S&S export).
_BASELINE_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                              "stock_baseline.json")


def _safe_format(df: "pd.DataFrame", fmt: dict) -> dict:
    """Drop any keys from the format dict that aren't columns of `df`.

    Defensive guard: prevents KeyError crashes when a column is renamed
    or absent. Pair with df.style.format(_safe_format(df, fmt)).
    """
    return {k: v for k, v in fmt.items() if k in df.columns}

PURCHASE_TYPES: tuple[int, ...] = (11, 20, 22, 30, 32, 33, 36, 42, 45, 46, 48, 54)
IMPORT_TYPES:   tuple[int, ...] = (22, 54)               # imports proper
DAMAN_TYPES:    tuple[int, ...] = (42,)                  # Daman / cross-state
SALES_TYPES:    tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)


def _month_first(d: date, months_offset: int = 0) -> date:
    m = d.month - 1 + months_offset
    return date(d.year + m // 12, m % 12 + 1, 1)

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

# FY-CASE join fragment that kills TrVocItem duplicate FY rows.
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
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

def _kegaware_cases(desc: str, units: float, bpc) -> float:
    """Convert a physical quantity (bottles, or keg count) to case-equivalents
    using the SAME keg rule as CASES_SQL_EXPR (20LT=2.56, 30LT=3.85, 50LT=6.41
    cases; everything else = bottles / BottlesPerCase). Keeps the Inventory
    page's case counts consistent with Purchase / Sales / Sales-Plan."""
    d = str(desc or "").upper().replace(" ", "")
    u = float(units or 0)
    if "50LT" in d:
        return u * (50.0 / 7.8)
    if "30LT" in d:
        return u * (30.0 / 7.8)
    if "20LT" in d:
        return u * (20.0 / 7.8)
    try:
        b = float(bpc or 0)
    except (TypeError, ValueError):
        b = 0.0
    return (u / b) if b > 0 else 0.0


def _is_keg(desc: str) -> bool:
    d = str(desc or "").upper().replace(" ", "")
    return ("50LT" in d) or ("30LT" in d) or ("20LT" in d)


@st.cache_data(ttl=3600, show_spinner=False)
def _load_stock_baseline() -> tuple[str, dict]:
    """Per-item closing stock (bottles) from the last ERP Stock & Sale export
    — the authoritative anchor. Returns (baseline_date 'YYYY-MM-DD',
    {ItemID: bottles}). Empty if no baseline file is present."""
    try:
        with open(_BASELINE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        items = {str(k): int(v) for k, v in d.get("items", {}).items()}
        return str(d.get("baseline_date", "")), items
    except Exception as exc:
        print(f"[inventory] stock baseline unavailable: {exc}", file=sys.stderr)
        return "", {}


@st.cache_data(ttl=300, show_spinner=False)
def _load_rollforward(after_date: str) -> dict:
    """Net stock movement per item (bottles, In − Out, FREE GOODS INCLUDED)
    for vouchers dated ON/AFTER the baseline date through today. FY-CASE
    deduped. The baseline is the FY-opening stock (start of 1 April), so we
    include movements from that date onward (>=). Free goods are included
    because dispatched free stock physically leaves the godown (matches the
    ERP Stock & Sale 'Out')."""
    if not after_date:
        return {}
    sql = f"""
        SELECT vi.ItemID,
            SUM(CASE WHEN mt.QtyInOut='I' THEN ISNULL(vi.TotalBottleQty,0)
                     WHEN mt.QtyInOut='O' THEN -ISNULL(vi.TotalBottleQty,0)
                     ELSE 0 END)                          AS NetB
        FROM TrVocItem vi
        JOIN TrVocHead   h  ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
        JOIN MsTransType mt ON mt.TransTypeID = vi.TransTypeID
        WHERE h.Cancelled = 'N' AND mt.ItemYN = 'Y' AND mt.QtyInOut IN ('I','O')
          AND vi.ItemID LIKE 'I%'
          AND CAST(h.VoucherDate AS date) >= ?
          AND CAST(h.VoucherDate AS date) <= CAST(GETDATE() AS date)
          {_FY_JOIN}
        GROUP BY vi.ItemID
    """
    df = run_query(sql, (after_date,))
    if df.empty:
        return {}
    return {str(r.ItemID).strip(): int(r.NetB or 0) for r in df.itertuples()}


@st.cache_data(ttl=300, show_spinner=False)
def _load_rollforward_io(after_date: str) -> pd.DataFrame:
    """Per-item Inward / Outward bottles (free goods INCLUDED) on/after the
    baseline date — the split version of _load_rollforward, used by the stock
    reconciliation so Opening + In − Out ties exactly to the live Closing."""
    cols = ["ItemID", "InB", "OutB"]
    if not after_date:
        return pd.DataFrame(columns=cols)
    sql = f"""
        SELECT vi.ItemID,
            SUM(CASE WHEN mt.QtyInOut='I' THEN ISNULL(vi.TotalBottleQty,0) ELSE 0 END) AS InB,
            SUM(CASE WHEN mt.QtyInOut='O' THEN ISNULL(vi.TotalBottleQty,0) ELSE 0 END) AS OutB
        FROM TrVocItem vi
        JOIN TrVocHead   h  ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
        JOIN MsTransType mt ON mt.TransTypeID = vi.TransTypeID
        WHERE h.Cancelled = 'N' AND mt.ItemYN = 'Y' AND mt.QtyInOut IN ('I','O')
          AND vi.ItemID LIKE 'I%'
          AND CAST(h.VoucherDate AS date) >= ?
          AND CAST(h.VoucherDate AS date) <= CAST(GETDATE() AS date)
          {_FY_JOIN}
        GROUP BY vi.ItemID
    """
    df = run_query(sql, (after_date,))
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["InB"]  = pd.to_numeric(df["InB"],  errors="coerce").fillna(0).astype(int)
    df["OutB"] = pd.to_numeric(df["OutB"], errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_current_stock() -> pd.DataFrame:
    """Live closing stock per item = ERP Stock & Sale baseline + live movements
    since the baseline date. Ties to the ERP report and stays current as bills
    post — no daily export needed. Falls back to the MsItemBatchOpening batch
    table only if no baseline file is present."""
    base_date, base = _load_stock_baseline()
    if not base:
        return _load_current_stock_legacy()
    roll = _load_rollforward(base_date)
    item_ids = set(base) | set(roll)
    ph = ",".join(f"'{i}'" for i in item_ids)
    df = run_query(f"""
        SELECT im.ItemID,
            ISNULL(im.ItemDescription, im.ItemID)        AS ItemDescription,
            ISNULL(b.BrandName,        '(unknown)')      AS BrandName,
            ISNULL(b.CompanyID,        '')               AS CompanyID,
            ISNULL(im.BottlesPerCase,  0)                AS BottlesPerCase,
            ISNULL(im.ValuationCaseRate,   0.0)          AS ValRateCase,
            ISNULL(im.ValuationBottleRate, 0.0)          AS ValRateBottle
        FROM MsItemMaster im
        LEFT JOIN MsBrandMaster b ON b.BrandID = im.BrandID
        WHERE im.ItemID IN ({ph})
    """)
    if df.empty:
        return df
    df["BottlesPerCase"] = pd.to_numeric(df["BottlesPerCase"], errors="coerce").fillna(0).astype(int)
    df["ValRateCase"]    = pd.to_numeric(df["ValRateCase"],    errors="coerce").fillna(0.0)
    df["ValRateBottle"]  = pd.to_numeric(df["ValRateBottle"],  errors="coerce").fillna(0.0)
    # Clamp at 0: re-coded SKUs (price change → new ItemID) can roll forward
    # negative when their pre-recode outflow lands on the new code. A negative
    # physical stock is meaningless and would silently drag down totals.
    df["ClosingBottles"] = df["ItemID"].map(
        lambda i: base.get(i, 0) + roll.get(i, 0)).clip(lower=0).astype(int)
    df["Principal"]      = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    df["ClosingCases"]   = df.apply(
        lambda r: _kegaware_cases(r["ItemDescription"], r["ClosingBottles"],
                                  r["BottlesPerCase"]), axis=1)
    df["ClosingCasesPlain"] = df.apply(
        lambda r: (r["ClosingBottles"] / r["BottlesPerCase"])
                  if r["BottlesPerCase"] > 0 else 0.0, axis=1)
    df["CaseRem"]   = df["ClosingCases"].astype(int)
    df["BottleRem"] = df.apply(
        lambda r: 0 if _is_keg(r["ItemDescription"])
                  else (int(r["ClosingBottles"] - r["CaseRem"] * r["BottlesPerCase"])
                        if r["BottlesPerCase"] > 0 else int(r["ClosingBottles"])),
        axis=1)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_current_stock_legacy() -> pd.DataFrame:
    """Fallback: live closing stock from MsItemBatchOpening (used only when no
    S&S baseline file exists). NOTE: this batch table doesn't reconcile to the
    ERP Stock & Sale report for re-coded SKUs — that's why the baseline exists."""
    sql = """
        SELECT
            bo.ItemID,
            ISNULL(im.ItemDescription, bo.ItemID)        AS ItemDescription,
            ISNULL(b.BrandName,        '(unknown)')      AS BrandName,
            ISNULL(b.CompanyID,        '')               AS CompanyID,
            ISNULL(im.BottlesPerCase,  0)                AS BottlesPerCase,
            ISNULL(im.ValuationCaseRate,   0.0)          AS ValRateCase,
            ISNULL(im.ValuationBottleRate, 0.0)          AS ValRateBottle,
            SUM(ISNULL(bo.ClosingQty, 0))                AS ClosingBottles
        FROM MsItemBatchOpening bo
        LEFT JOIN MsItemMaster  im ON im.ItemID  = bo.ItemID
        LEFT JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
        WHERE bo.ItemID LIKE 'I%'
        GROUP BY bo.ItemID, im.ItemDescription, b.BrandName, b.CompanyID,
                 im.BottlesPerCase, im.ValuationCaseRate, im.ValuationBottleRate
    """
    df = run_query(sql)
    if not df.empty:
        df["BottlesPerCase"] = pd.to_numeric(df["BottlesPerCase"], errors="coerce").fillna(0).astype(int)
        df["ClosingBottles"] = pd.to_numeric(df["ClosingBottles"], errors="coerce").fillna(0).astype(int)
        df["ValRateCase"]    = pd.to_numeric(df["ValRateCase"],    errors="coerce").fillna(0.0)
        df["ValRateBottle"]  = pd.to_numeric(df["ValRateBottle"],  errors="coerce").fillna(0.0)
        df["Principal"]      = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
        df["ClosingCases"]   = df.apply(
            lambda r: _kegaware_cases(r["ItemDescription"], r["ClosingBottles"],
                                      r["BottlesPerCase"]),
            axis=1,
        )
        df["ClosingCasesPlain"] = df.apply(
            lambda r: (r["ClosingBottles"] / r["BottlesPerCase"])
                      if r["BottlesPerCase"] > 0 else 0.0,
            axis=1,
        )
        df["CaseRem"]      = df["ClosingCases"].astype(int)
        df["BottleRem"]    = df.apply(
            lambda r: 0 if _is_keg(r["ItemDescription"])
                      else (int(r["ClosingBottles"] - r["CaseRem"] * r["BottlesPerCase"])
                            if r["BottlesPerCase"] > 0 else int(r["ClosingBottles"])),
            axis=1,
        )
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_movements(start: date, end: date, keg_aware: bool = True) -> pd.DataFrame:
    """Per-item In/Out bottle + case movements between start and end.

    Uses the FY-CASE filter so duplicate-FY rows in TrVocItem are dropped.
    Verified vs ERP Stock-Balance report (FY 2025-26): In within 0.005%.
    keg_aware toggles volume-conversion of kegs in the case columns.
    """
    _CX = cases_sql(keg_aware)
    sql = f"""
        SELECT
            vi.ItemID,
            SUM(CASE WHEN mt.QtyInOut='I' THEN ISNULL(vi.TotalBottleQty,0) ELSE 0 END) AS InBottles,
            SUM(CASE WHEN mt.QtyInOut='O' THEN ISNULL(vi.TotalBottleQty,0) ELSE 0 END) AS OutBottles,
            SUM(CASE WHEN mt.QtyInOut='I' THEN {_CX} ELSE 0 END) AS InCases,
            SUM(CASE WHEN mt.QtyInOut='O' THEN {_CX} ELSE 0 END) AS OutCases
        FROM TrVocItem vi
        JOIN TrVocHead   h  ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
        JOIN MsTransType mt ON mt.TransTypeID = vi.TransTypeID
        JOIN MsItemMaster im ON im.ItemID    = vi.ItemID
        WHERE h.Cancelled  = 'N'
          AND vi.FreeItemYN = 'N'
          AND vi.ItemID     LIKE 'I%'
          AND mt.ItemYN     = 'Y'
          AND mt.QtyInOut IN ('I','O')
          AND h.VoucherDate BETWEEN ? AND ?
          {_FY_JOIN}
        GROUP BY vi.ItemID
    """
    df = run_query(sql, (str(start), str(end)))
    if not df.empty:
        for c in ("InBottles", "OutBottles"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        for c in ("InCases", "OutCases"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=86400, show_spinner=False)   # FY-fixed — 24h
def _load_opening_stock() -> pd.DataFrame:
    """Per-item opening bottles from MsItemBatchOpening (FY-opening basis)."""
    sql = """
        SELECT ItemID,
               SUM(ISNULL(OpeningQty, 0))  AS OpeningBottles
        FROM MsItemBatchOpening
        WHERE ItemID LIKE 'I%'
        GROUP BY ItemID
    """
    df = run_query(sql)
    if not df.empty:
        df["OpeningBottles"] = pd.to_numeric(df["OpeningBottles"], errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=86400, show_spinner=False)   # item classification — 24h
def _load_item_origin() -> pd.DataFrame:
    """Classify each item as Import / Daman / Domestic based on dominant
    purchase TransType in the last 12 months."""
    imp_ph    = ",".join(str(t) for t in IMPORT_TYPES)
    daman_ph  = ",".join(str(t) for t in DAMAN_TYPES)
    pu_ph     = ",".join(str(t) for t in PURCHASE_TYPES)
    sql = f"""
        WITH PerTT AS (
            SELECT
                vi.ItemID, h.TransTypeID,
                SUM(ISNULL(vi.TotalBottleQty, 0)) AS Bottles
            FROM TrVocItem vi
            JOIN TrVocHead h ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
            WHERE h.Cancelled  = 'N'
              AND vi.FreeItemYN = 'N'
              AND vi.ItemID     LIKE 'I%'
              AND h.TransTypeID IN ({pu_ph})
              AND h.VoucherDate >= DATEADD(MONTH, -12, GETDATE())
              {_FY_JOIN}
            GROUP BY vi.ItemID, h.TransTypeID
        )
        SELECT
            ItemID,
            CASE
              WHEN MAX(CASE WHEN TransTypeID IN ({imp_ph})   THEN Bottles ELSE 0 END) > 0 THEN 'Import'
              WHEN MAX(CASE WHEN TransTypeID IN ({daman_ph}) THEN Bottles ELSE 0 END) > 0 THEN 'Daman'
              ELSE 'Domestic'
            END AS Origin
        FROM PerTT
        GROUP BY ItemID
    """
    return run_query(sql)


@st.cache_data(ttl=3600, show_spinner=False)
def _load_sales_velocity(as_of_date: date, days_back: int = 30,
                         keg_aware: bool = True) -> pd.DataFrame:
    """Per-item sales cases in last N days + last sale date (FY-CASE filtered)."""
    sql = f"""
        SELECT
            vi.ItemID,
            SUM({cases_sql(keg_aware)})               AS Cases,
            MAX(h.VoucherDate)                        AS LastSale
        FROM TrVocItem vi
        JOIN TrVocHead    h  ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
        JOIN MsTransType  mt ON mt.TransTypeID = vi.TransTypeID
        JOIN MsItemMaster im ON im.ItemID      = vi.ItemID
        WHERE h.Cancelled  = 'N'
          AND mt.QtyInOut  = 'O'
          AND vi.FreeItemYN = 'N'
          AND vi.ItemID     LIKE 'I%'
          AND h.VoucherDate BETWEEN DATEADD(DAY, -{days_back}, ?) AND ?
          {_FY_JOIN}
        GROUP BY vi.ItemID
    """
    df = run_query(sql, (str(as_of_date), str(as_of_date)))
    if not df.empty:
        df["Cases"]    = pd.to_numeric(df["Cases"], errors="coerce").fillna(0.0)
        df["LastSale"] = pd.to_datetime(df["LastSale"], errors="coerce")
    return df


@st.cache_data(ttl=600, show_spinner=False)
def _load_demand_signals(as_of: date, keg_aware: bool = True) -> pd.DataFrame:
    """Per-item SALES demand signals for the indent predictor:
    L3M = avg/month over the 3 prior full months · PrevMo = last full month ·
    ThisMTD = sold so far this month. SALES_TYPES only (true outlet demand)."""
    msf   = _month_first(as_of)               # 1st of this month
    l3s   = _month_first(as_of, -3)           # 1st, 3 months back
    prevs = _month_first(as_of, -1)           # 1st of last month
    preve = msf - timedelta(days=1)           # last day of last month
    sales = ",".join(str(t) for t in SALES_TYPES)
    cx = cases_sql(keg_aware)
    sql = f"""
        SELECT vi.ItemID,
            SUM(CASE WHEN h.VoucherDate >= ? AND h.VoucherDate < ?  THEN {cx} ELSE 0 END) AS L3MCases,
            SUM(CASE WHEN h.VoucherDate >= ? AND h.VoucherDate <= ? THEN {cx} ELSE 0 END) AS PrevMo,
            SUM(CASE WHEN h.VoucherDate >= ? AND h.VoucherDate <= ? THEN {cx} ELSE 0 END) AS ThisMTD
        FROM TrVocItem vi
        JOIN TrVocHead    h  ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
        JOIN MsItemMaster im ON im.ItemID      = vi.ItemID
        WHERE h.Cancelled = 'N' AND vi.FreeItemYN = 'N' AND vi.ItemID LIKE 'I%'
          AND h.TransTypeID IN ({sales})
          AND h.VoucherDate >= ? AND h.VoucherDate <= ?
          {_FY_JOIN}
        GROUP BY vi.ItemID
    """
    p = (str(l3s), str(msf), str(prevs), str(preve), str(msf), str(as_of),
         str(l3s), str(as_of))
    df = run_query(sql, p)
    if df.empty:
        return pd.DataFrame(columns=["ItemID", "L3MMonthly", "PrevMo", "ThisMTD"])
    for c in ("L3MCases", "PrevMo", "ThisMTD"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["L3MMonthly"] = df["L3MCases"] / 3.0
    return df[["ItemID", "L3MMonthly", "PrevMo", "ThisMTD"]]


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _kpi_card(label: str, value: str, sub: str,
              sub_color: str, accent: str) -> str:
    return f"""
    <div style='background:#fff;border:1px solid #e5e7eb;border-radius:8px;
                border-left:4px solid {accent};padding:12px 16px;
                box-shadow:0 1px 2px rgba(0,0,0,0.04);'>
        <div style='font-size:0.7rem;color:#6b7280;
                    text-transform:uppercase;letter-spacing:0.05em'>{label}</div>
        <div style='font-size:1.5rem;font-weight:700;color:#111827;
                    margin-top:4px;line-height:1.15'>{value}</div>
        <div style='font-size:0.78rem;color:{sub_color};margin-top:2px;
                    font-weight:600'>{sub}</div>
    </div>
    """


def _fmt_cases_with_remainder(cases_int: int, bottle_rem: int) -> str:
    if bottle_rem > 0:
        return f"{cases_int:,} cs + {bottle_rem:,} bot"
    return f"{cases_int:,} cs"


def _build_stock_df(as_of: date) -> pd.DataFrame:
    """Stock per item as of `as_of`. Live for today; rolled back otherwise.

    Live source: MsItemBatchOpening.ClosingQty
    Historical:  Live - movements after as_of (now using FY-CASE filter)
    """
    stock_df = _load_current_stock()
    if stock_df.empty:
        return stock_df

    today = date.today()
    if as_of >= today:
        out = stock_df.copy()
    else:
        # Movements strictly AFTER as_of through today, FY-CASE filtered
        moves_after = _load_movements(as_of, today)
        if moves_after.empty:
            out = stock_df.copy()
        else:
            merged = stock_df.merge(
                moves_after[["ItemID", "InBottles", "OutBottles"]],
                on="ItemID", how="left",
            ).fillna({"InBottles": 0, "OutBottles": 0})
            merged["ClosingBottles"] = (
                merged["ClosingBottles"] + merged["OutBottles"] - merged["InBottles"]
            ).clip(lower=0).astype(int)
            out = merged.drop(columns=["InBottles", "OutBottles"], errors="ignore")

    # Recompute cases/remainder from updated ClosingBottles (keg-aware)
    out["ClosingCases"] = out.apply(
        lambda r: _kegaware_cases(r["ItemDescription"], r["ClosingBottles"],
                                  r["BottlesPerCase"]),
        axis=1,
    )
    out["ClosingCasesPlain"] = out.apply(
        lambda r: (r["ClosingBottles"] / r["BottlesPerCase"])
                  if r["BottlesPerCase"] > 0 else 0.0,
        axis=1,
    )
    out["CaseRem"]   = out["ClosingCases"].astype(int)
    out["BottleRem"] = out.apply(
        lambda r: 0 if _is_keg(r["ItemDescription"])
                  else (int(r["ClosingBottles"] - r["CaseRem"] * r["BottlesPerCase"])
                        if r["BottlesPerCase"] > 0 else int(r["ClosingBottles"])),
        axis=1,
    )
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _section_kpis(stock_df: pd.DataFrame) -> None:
    in_stock = stock_df[stock_df["ClosingBottles"] > 0]
    total_items   = len(in_stock)
    total_bottles = int(in_stock["ClosingBottles"].sum())
    total_cases   = float(in_stock["ClosingCases"].sum())

    # Inventory value uses Valuation rate (already includes excise / landed cost)
    merged = stock_df.copy()
    merged["Value"] = merged["ClosingCasesPlain"] * merged["ValRateCase"]
    total_value = float(merged["Value"].sum())

    out_of_stock = int((stock_df["ClosingBottles"] <= 0).sum())

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(_kpi_card(
            "Items in Stock",
            f"{total_items:,}",
            f"of {len(stock_df):,} total items",
            "#6b7280", _KPI_COLORS[0],
        ), unsafe_allow_html=True)
    with c2:
        # Cases first, bottles secondary
        cs_int = int(total_cases)
        bot_rem = total_bottles - cs_int * (total_bottles // cs_int if cs_int else 0)
        bot_only = total_bottles  # show all bottles in sub
        st.markdown(_kpi_card(
            "Total Cases",
            f"{cs_int:,}",
            f"{total_bottles:,} bottles total",
            "#6b7280", _KPI_COLORS[1],
        ), unsafe_allow_html=True)
    with c3:
        st.markdown(_kpi_card(
            "Estimated Value",
            f"₹{total_value/1e7:.2f} Cr",
            "At MsItemMaster ValuationCaseRate",
            "#6b7280", _KPI_COLORS[2],
        ), unsafe_allow_html=True)
    with c4:
        st.markdown(_kpi_card(
            "Out of Stock",
            f"{out_of_stock:,}",
            "items with zero closing",
            "#dc2626" if out_of_stock else "#6b7280", _KPI_COLORS[3],
        ), unsafe_allow_html=True)


def _section_reconciliation(stock_df: pd.DataFrame,
                            keg_aware: bool = True) -> None:
    """Stock flow this FY: Opening (1 Apr) + Inward − Outward = Closing (now).
    Sourced consistently with the live stock — Opening from the S&S baseline,
    In/Out from live movements since — so the four numbers always tie out."""
    st.markdown("##### Stock flow this year")

    base_date, base = _load_stock_baseline()
    io = _load_rollforward_io(base_date)

    # One row per item: opening + inward − outward = closing (bottles)
    meta = stock_df[["ItemID", "BottlesPerCase", "ItemDescription"]].drop_duplicates("ItemID")
    m = meta.copy()
    m["OpenB"] = m["ItemID"].map(lambda i: base.get(i, 0)).fillna(0)
    io_in  = dict(zip(io["ItemID"], io["InB"]))  if not io.empty else {}
    io_out = dict(zip(io["ItemID"], io["OutB"])) if not io.empty else {}
    m["InB"]  = m["ItemID"].map(lambda i: io_in.get(i, 0))
    m["OutB"] = m["ItemID"].map(lambda i: io_out.get(i, 0))
    m["CloseB"] = m["OpenB"] + m["InB"] - m["OutB"]
    m["BottlesPerCase"]  = pd.to_numeric(m["BottlesPerCase"], errors="coerce").fillna(0).astype(int)
    m["ItemDescription"] = m["ItemDescription"].fillna("")

    def _cs(col):
        return m.apply(lambda r: _kegaware_cases(r["ItemDescription"], r[col],
                                                 r["BottlesPerCase"]) if keg_aware
                       else (r[col] / r["BottlesPerCase"] if r["BottlesPerCase"] > 0 else 0.0),
                       axis=1).sum()

    opening, inward, outward, closing = _cs("OpenB"), _cs("InB"), _cs("OutB"), _cs("CloseB")

    bd = base_date or "FY start"
    st.caption(
        f"**Opening stock ({bd})  +  Inward  −  Outward  =  Closing stock (today).** "
        "Opening is the ERP Stock & Sale year-opening; In/Out are live bill "
        "movements (free goods included). Cases shown rounded."
    )
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Opening (1 Apr)", f"{opening:,.0f} cs")
    g2.metric("➕ Inward",        f"{inward:,.0f} cs")
    g3.metric("➖ Outward",       f"{outward:,.0f} cs")
    g4.metric("🟰 Closing (now)", f"{closing:,.0f} cs")


def _section_by_principal(stock_df: pd.DataFrame) -> None:
    st.markdown("##### Stock by Principal")
    if stock_df.empty:
        st.info("No stock data."); return

    df = stock_df.copy()
    df["Value"] = df["ClosingCasesPlain"] * df["ValRateCase"]
    g = (
        df[df["ClosingBottles"] > 0]
        .groupby("Principal", as_index=False)
        .agg(Items=("ItemID", "nunique"),
             Cases=("ClosingCases", "sum"),
             Value=("Value", "sum"))
        .sort_values("Value", ascending=False)
    )
    st.dataframe(
        g.rename(columns={
            "Items": "Items in Stock", "Cases": "Total Cases",
            "Value": "Estimated Value ₹",
        }).style.format({
            "Items in Stock":     "{:,}",
            "Total Cases":        "{:,.0f}",
            "Estimated Value ₹":  format_inr,
        }),
        use_container_width=True, hide_index=True,
    )


def _section_top_items(stock_df: pd.DataFrame, origin_df: pd.DataFrame) -> None:
    st.markdown("##### Top 20 items by stock value")
    if stock_df.empty:
        st.info("No stock data."); return

    df = stock_df.merge(origin_df, on="ItemID", how="left").fillna({"Origin": "Domestic"})
    df["Value"] = df["ClosingCasesPlain"] * df["ValRateCase"]
    top = df[df["Value"] > 0].sort_values("Value", ascending=False).head(20).copy()
    if top.empty:
        st.info("No items with value > 0."); return

    # Bar chart by principal color
    top_chart = top.sort_values("Value", ascending=True)
    colors = [_PRINCIPAL_COLOR.get(p, "#B4B2A9") for p in top_chart["Principal"]]
    top_chart["ValueCr"] = top_chart["Value"] / 1e7
    fig = go.Figure(go.Bar(
        x=top_chart["ValueCr"], y=top_chart["ItemDescription"],
        orientation="h", marker_color=colors,
        text=[f"₹{v:.2f} Cr" for v in top_chart["ValueCr"]],
        textposition="outside",
        customdata=top_chart["Value"].apply(format_inr),
        hovertemplate="<b>%{y}</b><br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        template="plotly_white",
        margin=dict(l=16, r=16, t=16, b=16),
        height=max(420, len(top_chart) * 24),
        xaxis=dict(title="Value (₹ Cr)", ticksuffix=" Cr", gridcolor="#E8E8E8"),
        yaxis=dict(gridcolor="#E8E8E8"),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Companion table with Origin column + duplicate items kept separate
    top["Stock"] = top.apply(
        lambda r: _fmt_cases_with_remainder(int(r["CaseRem"]), int(r["BottleRem"])),
        axis=1,
    )
    tbl = top[["BrandName", "ItemDescription", "Origin",
               "Stock", "ValRateCase", "Value"]].rename(columns={
        "BrandName":       "Brand",
        "ItemDescription": "Item",
        "ValRateCase":     "Landed Rate",
        "Value":           "Stock Value",
    })
    st.dataframe(
        tbl.style.format({
            "Landed Rate":  "₹{:,.0f}",
            "Stock Value":  format_inr,
        }),
        use_container_width=True, hide_index=True,
    )


def _section_slow_movers(stock_df: pd.DataFrame, vel_df: pd.DataFrame,
                         threshold_cases: int = 50) -> None:
    st.markdown("##### Slow Movers — Capital tied up")
    st.caption(f"Closing > {threshold_cases} cases AND last-30d sales < 10 cases.")
    if stock_df.empty:
        st.info("No stock data."); return

    merged = stock_df.merge(vel_df[["ItemID", "Cases", "LastSale"]],
                            on="ItemID", how="left").rename(columns={"Cases": "Sales30"})
    merged["Sales30"] = pd.to_numeric(merged["Sales30"], errors="coerce").fillna(0.0)
    # Valuation is ALWAYS on physical (un-converted) units — a keg is one keg,
    # never 6.41 cases. Keg-aware cases are only for purchase/sales reporting.
    merged["Value"]   = merged["ClosingCasesPlain"] * merged["ValRateCase"]

    slow = merged[
        (merged["ClosingCases"] > threshold_cases) & (merged["Sales30"] < 10)
    ].copy()
    if slow.empty:
        st.success("No slow movers."); return

    slow["DailyAvg"]  = slow["Sales30"] / 30
    slow["DaysCover"] = slow.apply(
        lambda r: (r["ClosingCases"] / r["DailyAvg"]) if r["DailyAvg"] > 0 else 9999,
        axis=1,
    )
    slow = slow.sort_values("DaysCover", ascending=False).head(30)

    # Keep original column names so styler + format always agree.
    # Display labels are handled via Streamlit column_config below.
    disp = slow[["BrandName", "ItemDescription", "ClosingCases",
                 "Sales30", "DaysCover", "Value"]].copy()

    def _row_style(row):
        v = row["DaysCover"]
        if v > 90:    bg = "background-color:#fee2e2"
        elif v > 30:  bg = "background-color:#fef3c7"
        else:         bg = ""
        return [bg] * len(row)

    fmt = {
        "ClosingCases": "{:,.0f}",
        "Sales30":      "{:,.2f}",
        "DaysCover":    lambda x: "9999+" if x >= 9999 else f"{x:,.0f}",
        "Value":        format_inr,
    }
    styled = (
        disp.style
        .apply(_row_style, axis=1)
        .format(_safe_format(disp, fmt))
    )
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "BrandName":       st.column_config.Column("Brand"),
            "ItemDescription": st.column_config.Column("Item"),
            "ClosingCases":    st.column_config.Column("Closing Cases"),
            "Sales30":         st.column_config.Column("Last 30d Cases"),
            "DaysCover":       st.column_config.Column("Days of Cover"),
            "Value":           st.column_config.Column("Stock Value"),
        },
    )


def _section_out_of_stock(stock_df: pd.DataFrame, vel_30: pd.DataFrame,
                          vel_90: pd.DataFrame) -> None:
    st.markdown("##### ⚠️ Out of Stock — Risk of lost sales")
    st.caption("Items with zero closing AND proven demand (>5 cases in last 30 days).")
    if stock_df.empty:
        st.info("No stock data."); return

    s = stock_df.merge(vel_30[["ItemID", "Cases", "LastSale"]],
                       on="ItemID", how="left").rename(columns={"Cases": "Sales30"})
    s["Sales30"] = pd.to_numeric(s["Sales30"], errors="coerce").fillna(0.0)
    s = s.merge(vel_90[["ItemID", "Cases"]].rename(columns={"Cases": "Sales90"}),
                on="ItemID", how="left")
    s["Sales90"] = pd.to_numeric(s["Sales90"], errors="coerce").fillna(0.0)

    risk = s[(s["ClosingCases"] <= 0) & (s["Sales30"] > 5)].copy()
    if risk.empty:
        st.success("No out-of-stock items with recent demand."); return

    risk = risk.sort_values("Sales30", ascending=False).head(40)
    risk["Last Sale"] = pd.to_datetime(risk["LastSale"], errors="coerce") \
        .dt.strftime("%d %b %Y").fillna("—")
    disp = risk[["BrandName", "ItemDescription", "Last Sale",
                 "Sales30", "Sales90"]].rename(columns={
        "BrandName":       "Brand",
        "ItemDescription": "Item",
        "Sales30":         "Last 30d Cases",
        "Sales90":         "Last 90d Cases",
    })
    st.dataframe(
        disp.style.format({"Last 30d Cases": "{:,.2f}", "Last 90d Cases": "{:,.2f}"}),
        use_container_width=True, hide_index=True,
    )


def _section_days_of_cover(stock_df: pd.DataFrame, vel_df: pd.DataFrame) -> None:
    st.markdown("##### Days of Cover — Top 30 selling items")
    st.caption("Green > 30 days · Amber 15–30 · Red < 15 (urgent reorder)")
    if stock_df.empty or vel_df.empty:
        st.info("Insufficient data."); return

    merged = stock_df.merge(vel_df[["ItemID", "Cases"]],
                            on="ItemID", how="left").rename(columns={"Cases": "Sales30"})
    merged["Sales30"] = pd.to_numeric(merged["Sales30"], errors="coerce").fillna(0.0)
    sellers = merged[merged["Sales30"] > 0].copy()
    if sellers.empty:
        st.info("No selling items in the last 30 days."); return
    sellers["DailyAvg"]  = sellers["Sales30"] / 30
    sellers["DaysCover"] = sellers.apply(
        lambda r: (r["ClosingCases"] / r["DailyAvg"]) if r["DailyAvg"] > 0 else 9999,
        axis=1,
    )
    top_sellers = sellers.sort_values("Sales30", ascending=False).head(30)

    # Style on original column names; show display labels via column_config.
    disp = top_sellers[["BrandName", "ItemDescription", "ClosingCases",
                        "DailyAvg", "DaysCover"]].copy()

    def _doc_style(row):
        v = row["DaysCover"]
        if v >= 9999:        bg = ""
        elif v >= 30:        bg = "background-color:#dcfce7"
        elif v >= 15:        bg = "background-color:#fef3c7"
        else:                bg = "background-color:#fee2e2"
        return [bg if c == "DaysCover" else "" for c in row.index]

    fmt = {
        "ClosingCases": "{:,.0f}",
        "DailyAvg":     "{:.2f}",
        "DaysCover":    lambda x: "9999+" if x >= 9999 else f"{x:,.0f}",
    }
    styled = (
        disp.style
        .apply(_doc_style, axis=1)
        .format(_safe_format(disp, fmt))
    )
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "BrandName":       st.column_config.Column("Brand"),
            "ItemDescription": st.column_config.Column("Item"),
            "ClosingCases":    st.column_config.Column("Stock (Cases)"),
            "DailyAvg":        st.column_config.Column("Daily Avg Sales"),
            "DaysCover":       st.column_config.Column("Days of Cover"),
        },
    )


def _section_indent_planner(stock_df: pd.DataFrame, as_of: date,
                            keg_aware: bool = True) -> None:
    """Predict the stock you'll fall short of this month, so you can indent
    early. Per SKU: projected demand = balanced blend of L3M monthly, last
    month, and this-month run-rate; remaining = projected − sold MTD; if
    remaining > stock on hand, the gap is the indent quantity."""
    st.markdown("##### 📋 Indent planner — stock you may fall short of")
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    days_done = max(as_of.day, 1)
    st.caption(
        f"Projected demand = balanced blend of L3M monthly avg, last month, and "
        f"this-month run-rate (day {days_done}/{days_in_month}). **Indent** = "
        f"demand still expected this month − stock on hand. Order these early."
    )

    dem = _load_demand_signals(as_of, keg_aware=keg_aware)
    if dem.empty:
        st.info("No recent sales to forecast from."); return
    m = stock_df.merge(dem, on="ItemID", how="left")
    for c in ("L3MMonthly", "PrevMo", "ThisMTD"):
        m[c] = pd.to_numeric(m.get(c, 0), errors="coerce").fillna(0.0)

    # Run-rate projection of this month to a full month
    m["ThisProj"] = m["ThisMTD"] / days_done * days_in_month
    # Balanced blend (35% L3M / 30% last month / 35% this-month run-rate)
    m["Projected"] = 0.35 * m["L3MMonthly"] + 0.30 * m["PrevMo"] + 0.35 * m["ThisProj"]
    m["Remaining"] = (m["Projected"] - m["ThisMTD"]).clip(lower=0)
    m["Indent"]    = (m["Remaining"] - m["ClosingCases"]).clip(lower=0)
    daily = (m["L3MMonthly"] / 30.0).replace(0, pd.NA)
    m["DaysCover"] = (m["ClosingCases"] / daily).fillna(9999)

    short = m[m["Indent"] >= 1].sort_values("Indent", ascending=False)
    if short.empty:
        st.success("✅ Enough stock on hand to meet this month's projected demand "
                   "for every SKU — nothing to indent right now.")
        return

    st.warning(f"⚠️ **{len(short)} SKUs** projected to fall short this month — "
               f"total indent **{short['Indent'].sum():,.0f} cases**.")
    disp = short[["BrandName", "ItemDescription", "ClosingCases", "L3MMonthly",
                  "PrevMo", "ThisMTD", "Projected", "Remaining", "Indent",
                  "DaysCover"]].copy()
    fmt = {c: "{:,.0f}" for c in ["ClosingCases", "L3MMonthly", "PrevMo",
                                  "ThisMTD", "Projected", "Remaining", "Indent"]}
    fmt["DaysCover"] = lambda v: "—" if v >= 9999 else f"{v:,.0f} d"
    st.dataframe(
        disp.style.format(_safe_format(disp, fmt)),
        use_container_width=True, hide_index=True, height=440,
        column_config={
            "BrandName":       st.column_config.Column("Brand"),
            "ItemDescription": st.column_config.Column("Item"),
            "ClosingCases":    st.column_config.Column("Stock now"),
            "L3MMonthly":      st.column_config.Column("L3M/mo"),
            "PrevMo":          st.column_config.Column("Last month"),
            "ThisMTD":         st.column_config.Column("Sold MTD"),
            "Projected":       st.column_config.Column("Projected demand"),
            "Remaining":       st.column_config.Column("Still needed"),
            "Indent":          st.column_config.Column("➡️ INDENT cs"),
            "DaysCover":       st.column_config.Column("Days cover"),
        },
    )
    st.download_button(
        "⬇️ Download indent list",
        short[["BrandName", "ItemDescription", "ClosingCases", "Projected",
               "Remaining", "Indent"]].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"indent_planner_{as_of:%Y%m%d}.csv", mime="text/csv",
        key="inv_indent_dl")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("Inventory")
    st.caption("Stock levels, slow movers, and out-of-stock alerts")
    st.divider()

    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        as_of_date = st.date_input(
            "Stock as of",
            value=today,
            key="inv_as_of",
        )
    with c2:
        principal_filter = st.multiselect(
            "Principal",
            options=list(_PRINCIPAL_NAMES.values()),
            default=[],
            key="inv_principal_filter",
        )
    with c3:
        view_filter = st.selectbox(
            "Show",
            ["All items", "Low stock (<10 cases)", "Out of stock", "Slow movers"],
            key="inv_view_filter",
        )

    keg_aware = keg_mode_toggle("inv_keg_mode", default_keg_aware=False)

    # Show what the stock is anchored to (ERP S&S baseline + live roll-forward)
    _bdate, _bitems = _load_stock_baseline()
    if _bdate:
        st.caption(
            f"📦 Stock = **FY-opening ({_bdate})** from the ERP Stock & Sale "
            f"report + live bill movements since. Computed automatically — "
            f"you only upload the S&S **once a year on 1 April** to set the new "
            f"FY opening. ({len(_bitems)} opening items.)"
        )
    else:
        st.caption("⚠️ No S&S baseline found — showing MsItemBatchOpening "
                   "(may not match the ERP Stock & Sale report).")

    # Reconciliation period (defaults to FY start through as_of)
    with st.spinner("Loading stock + movement data…"):
        stock_df    = _build_stock_df(as_of_date)
        origin_df   = _load_item_origin()
        vel_30      = _load_sales_velocity(as_of_date, days_back=30, keg_aware=keg_aware)
        vel_90      = _load_sales_velocity(as_of_date, days_back=90, keg_aware=keg_aware)

    if stock_df.empty:
        st.error("No stock data returned."); return

    # Apply keg mode to the stock case counts (value always uses the plain
    # physical-unit basis via ClosingCasesPlain, so it's unaffected).
    if not keg_aware:
        stock_df = stock_df.copy()
        stock_df["ClosingCases"] = stock_df["ClosingCasesPlain"]
        stock_df["CaseRem"]      = stock_df["ClosingCases"].astype(int)

    if principal_filter:
        stock_df = stock_df[stock_df["Principal"].isin(principal_filter)]

    if view_filter == "Low stock (<10 cases)":
        view_df = stock_df[stock_df["ClosingCases"] < 10]
    elif view_filter == "Out of stock":
        view_df = stock_df[stock_df["ClosingBottles"] <= 0]
    else:
        view_df = stock_df

    if as_of_date < today:
        st.info(f"Showing historical stock as of {as_of_date.strftime('%d %b %Y')} "
                f"(rolled back from live).")

    safe_section("KPIs",            _section_kpis,            stock_df)
    st.divider()
    safe_section("Stock reconciliation", _section_reconciliation,
                 stock_df, keg_aware)
    st.divider()
    safe_section("By principal",    _section_by_principal,    stock_df)
    st.divider()
    safe_section("Top items",       _section_top_items,       view_df, origin_df)
    st.divider()
    safe_section("Slow movers",     _section_slow_movers,     stock_df, vel_30)
    st.divider()
    safe_section("Out of stock",    _section_out_of_stock,    stock_df, vel_30, vel_90)
    st.divider()
    safe_section("Days of cover",   _section_days_of_cover,   stock_df, vel_30)
    st.divider()
    safe_section("Indent planner",  _section_indent_planner,  stock_df, as_of_date, keg_aware)
