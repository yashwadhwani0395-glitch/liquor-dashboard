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

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, CASES_SQL_EXPR as _CASES

PURCHASE_TYPES: tuple[int, ...] = (11, 20, 22, 30, 32, 33, 36, 42, 45, 46, 48, 54)
IMPORT_TYPES:   tuple[int, ...] = (22, 54)               # imports proper
DAMAN_TYPES:    tuple[int, ...] = (42,)                  # Daman / cross-state

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

@st.cache_data(ttl=300, show_spinner=False)
def _load_current_stock() -> pd.DataFrame:
    """Live closing stock per item (today). Source: MsItemBatchOpening."""
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
            lambda r: (r["ClosingBottles"] / r["BottlesPerCase"])
                      if r["BottlesPerCase"] > 0 else 0.0,
            axis=1,
        )
        df["CaseRem"]      = df["ClosingCases"].astype(int)
        df["BottleRem"]    = df.apply(
            lambda r: int(r["ClosingBottles"] - r["CaseRem"] * r["BottlesPerCase"])
                      if r["BottlesPerCase"] > 0 else int(r["ClosingBottles"]),
            axis=1,
        )
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_movements(start: date, end: date) -> pd.DataFrame:
    """Per-item In/Out bottle + case movements between start and end.

    Uses the FY-CASE filter so duplicate-FY rows in TrVocItem are dropped.
    Verified vs ERP Stock-Balance report (FY 2025-26): In within 0.005%.
    """
    sql = f"""
        SELECT
            vi.ItemID,
            SUM(CASE WHEN mt.QtyInOut='I' THEN ISNULL(vi.TotalBottleQty,0) ELSE 0 END) AS InBottles,
            SUM(CASE WHEN mt.QtyInOut='O' THEN ISNULL(vi.TotalBottleQty,0) ELSE 0 END) AS OutBottles,
            SUM(CASE WHEN mt.QtyInOut='I' THEN {_CASES} ELSE 0 END) AS InCases,
            SUM(CASE WHEN mt.QtyInOut='O' THEN {_CASES} ELSE 0 END) AS OutCases
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


@st.cache_data(ttl=300, show_spinner=False)
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


@st.cache_data(ttl=300, show_spinner=False)
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


@st.cache_data(ttl=300, show_spinner=False)
def _load_sales_velocity(as_of_date: date, days_back: int = 30) -> pd.DataFrame:
    """Per-item sales cases in last N days + last sale date (FY-CASE filtered)."""
    sql = f"""
        SELECT
            vi.ItemID,
            SUM(CAST(vi.TotalBottleQty AS decimal(18,4))
                / NULLIF(im.BottlesPerCase, 0))      AS Cases,
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

    # Recompute cases/remainder from updated ClosingBottles
    out["ClosingCases"] = out.apply(
        lambda r: (r["ClosingBottles"] / r["BottlesPerCase"])
                  if r["BottlesPerCase"] > 0 else 0.0,
        axis=1,
    )
    out["CaseRem"]   = out["ClosingCases"].astype(int)
    out["BottleRem"] = out.apply(
        lambda r: int(r["ClosingBottles"] - r["CaseRem"] * r["BottlesPerCase"])
                  if r["BottlesPerCase"] > 0 else int(r["ClosingBottles"]),
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
    merged["Value"] = merged["ClosingCases"] * merged["ValRateCase"]
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


def _section_reconciliation(opening_df: pd.DataFrame,
                            move_df: pd.DataFrame,
                            stock_df: pd.DataFrame,
                            start: date, end: date) -> None:
    """ERP-style Op | In | Out | Cl reconciliation for the chosen period."""
    st.markdown("##### Stock Reconciliation")
    st.caption(
        f"Opening → In → Out → Closing for the period "
        f"{start.strftime('%d %b %Y')} → {end.strftime('%d %b %Y')}. "
        f"Matches the ERP **Stock Balance** report (using FY-CASE dedup)."
    )

    # Per-item: opening bottles + in - out
    m = opening_df.merge(move_df, on="ItemID", how="outer")
    for c in ("OpeningBottles", "InBottles", "OutBottles", "InCases", "OutCases"):
        m[c] = pd.to_numeric(m.get(c, 0), errors="coerce").fillna(0).astype(int)

    # Bring in BottlesPerCase + only "having balances" filter
    bpc = stock_df[["ItemID", "BottlesPerCase"]].drop_duplicates("ItemID")
    m = m.merge(bpc, on="ItemID", how="left")
    m["BottlesPerCase"] = pd.to_numeric(m["BottlesPerCase"], errors="coerce").fillna(0).astype(int)
    m["ClosingBottles"] = (m["OpeningBottles"] + m["InBottles"] - m["OutBottles"]).clip(lower=0)

    def _cases(row, col):
        bpc = row["BottlesPerCase"]
        return (row[col] // bpc) if bpc > 0 else 0

    def _bot_rem(row, col):
        bpc = row["BottlesPerCase"]
        if bpc <= 0:
            return int(row[col])
        return int(row[col] - (row[col] // bpc) * bpc)

    m["OpCases"]  = m.apply(lambda r: _cases(r, "OpeningBottles"),  axis=1)
    m["OpBotR"]   = m.apply(lambda r: _bot_rem(r, "OpeningBottles"), axis=1)
    m["ClCases"]  = m.apply(lambda r: _cases(r, "ClosingBottles"),  axis=1)
    m["ClBotR"]   = m.apply(lambda r: _bot_rem(r, "ClosingBottles"), axis=1)

    grand = {
        "Opening": _fmt_cases_with_remainder(int(m["OpCases"].sum()),
                                              int(m["OpBotR"].sum())),
        "In":      f"{int(m['InCases'].sum()):,} cs",
        "Out":     f"{int(m['OutCases'].sum()):,} cs",
        "Closing": _fmt_cases_with_remainder(int(m["ClCases"].sum()),
                                              int(m["ClBotR"].sum())),
    }

    g1, g2, g3, g4 = st.columns(4)
    with g1: st.metric("Opening", grand["Opening"])
    with g2: st.metric("In",      grand["In"])
    with g3: st.metric("Out",     grand["Out"])
    with g4: st.metric("Closing", grand["Closing"])


def _section_by_principal(stock_df: pd.DataFrame) -> None:
    st.markdown("##### Stock by Principal")
    if stock_df.empty:
        st.info("No stock data."); return

    df = stock_df.copy()
    df["Value"] = df["ClosingCases"] * df["ValRateCase"]
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
    df["Value"] = df["ClosingCases"] * df["ValRateCase"]
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
    merged["Value"]   = merged["ClosingCases"] * merged["ValRateCase"]

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

    def _row_style(row):
        if row["DaysCover"] > 90:  bg = "background-color:#fee2e2"
        elif row["DaysCover"] > 30: bg = "background-color:#fef3c7"
        else: bg = ""
        return [bg] * len(row)

    disp = slow[["BrandName", "ItemDescription", "ClosingCases",
                 "Sales30", "DaysCover", "Value"]].rename(columns={
        "BrandName":       "Brand",
        "ItemDescription": "Item",
        "ClosingCases":    "Closing Cases",
        "Sales30":         "Last 30d Cases",
        "DaysCover":       "Days of Cover",
        "Value":           "Stock Value",
    })
    styled = (
        disp.style
        .apply(_row_style, axis=1)
        .format({
            "Closing Cases":   "{:,.0f}",
            "Last 30d Cases":  "{:,.2f}",
            "Days of Cover":   lambda x: "9999+" if x >= 9999 else f"{x:,.0f}",
            "Stock Value":     format_inr,
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


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

    def _doc_style(row):
        try:
            v = float(str(row["Days of Cover"]).replace("+", "").replace(",", ""))
        except ValueError:
            return [""] * len(row)
        if v >= 30:  bg = "background-color:#dcfce7"
        elif v >= 15: bg = "background-color:#fef3c7"
        else:         bg = "background-color:#fee2e2"
        return [bg if c == "Days of Cover" else "" for c in row.index]

    disp = top_sellers[["BrandName", "ItemDescription", "ClosingCases",
                        "DailyAvg", "DaysCover"]].rename(columns={
        "BrandName":       "Brand",
        "ItemDescription": "Item",
        "ClosingCases":    "Stock (Cases)",
        "DailyAvg":        "Daily Avg Sales",
        "DaysCover":       "Days of Cover",
    })
    styled = (
        disp.style
        .apply(_doc_style, axis=1)
        .format({
            "Stock (Cases)":    "{:,.0f}",
            "Daily Avg Sales":  "{:.2f}",
            "Days of Cover":    lambda x: "9999+" if x >= 9999 else f"{x:,.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


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

    # Reconciliation period (defaults to FY start through as_of)
    with st.spinner("Loading stock + movement data…"):
        stock_df    = _build_stock_df(as_of_date)
        opening_df  = _load_opening_stock()
        move_df     = _load_movements(fy_start, as_of_date)
        origin_df   = _load_item_origin()
        vel_30      = _load_sales_velocity(as_of_date, days_back=30)
        vel_90      = _load_sales_velocity(as_of_date, days_back=90)

    if stock_df.empty:
        st.error("No stock data returned."); return

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

    _section_kpis(stock_df)
    st.divider()
    _section_reconciliation(opening_df, move_df, stock_df, fy_start, as_of_date)
    st.divider()
    _section_by_principal(stock_df)
    st.divider()
    _section_top_items(view_df, origin_df)
    st.divider()
    _section_slow_movers(stock_df, vel_30)
    st.divider()
    _section_out_of_stock(stock_df, vel_30, vel_90)
    st.divider()
    _section_days_of_cover(stock_df, vel_30)
