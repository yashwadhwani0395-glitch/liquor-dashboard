"""src/inventory.py — Inventory analytics.

Live stock (or historical via roll-back), slow movers, out-of-stock,
days-of-cover. Stock source: MsItemBatchOpening.ClosingQty aggregated
by ItemID (the ERP's running stock ledger).

For historical date: stock(as_of) = current_closing - movements after
as_of_date (in - out), so we can rebuild any past state cheaply.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr

PURCHASE_TYPES: tuple[int, ...] = (11, 20, 22, 30, 32, 33, 36, 42, 45, 46, 48, 54)

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
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def _load_current_stock() -> pd.DataFrame:
    """Live closing stock per item from MsItemBatchOpening."""
    sql = """
        SELECT
            bo.ItemID,
            ISNULL(im.ItemDescription, bo.ItemID)        AS ItemDescription,
            ISNULL(b.BrandName,        '(unknown)')      AS BrandName,
            ISNULL(b.CompanyID,        '')               AS CompanyID,
            ISNULL(im.BottlesPerCase,  0)                AS BottlesPerCase,
            SUM(ISNULL(bo.ClosingQty, 0))                AS ClosingBottles
        FROM MsItemBatchOpening bo
        LEFT JOIN MsItemMaster  im ON im.ItemID  = bo.ItemID
        LEFT JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
        WHERE bo.ItemID LIKE 'I%'
        GROUP BY bo.ItemID, im.ItemDescription, b.BrandName, b.CompanyID, im.BottlesPerCase
    """
    df = run_query(sql)
    if not df.empty:
        df["BottlesPerCase"] = pd.to_numeric(df["BottlesPerCase"], errors="coerce").fillna(0).astype(int)
        df["ClosingBottles"] = pd.to_numeric(df["ClosingBottles"], errors="coerce").fillna(0).astype(int)
        df["ClosingCases"]   = df.apply(
            lambda r: (r["ClosingBottles"] / r["BottlesPerCase"]) if r["BottlesPerCase"] else 0.0,
            axis=1,
        ).round(1)
        df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_movements_since(as_of_date: date) -> pd.DataFrame:
    """In/Out bottle movements per item AFTER as_of_date (used to roll back)."""
    sql = """
        SELECT
            vi.ItemID,
            SUM(CASE WHEN mt.QtyInOut = 'I' THEN ISNULL(vi.TotalBottleQty, 0) ELSE 0 END) AS InAfter,
            SUM(CASE WHEN mt.QtyInOut = 'O' THEN ISNULL(vi.TotalBottleQty, 0) ELSE 0 END) AS OutAfter
        FROM TrVocItem vi
        JOIN TrVocHead h
            ON  h.TransTypeID = vi.TransTypeID
            AND h.VoucherNo   = vi.VoucherNo
        JOIN MsTransType mt ON mt.TransTypeID = vi.TransTypeID
        WHERE h.Cancelled  = 'N'
          AND vi.FreeItemYN = 'N'
          AND vi.ItemID     LIKE 'I%'
          AND mt.ItemYN     = 'Y'
          AND mt.QtyInOut IN ('I','O')
          AND h.VoucherDate > ?
        GROUP BY vi.ItemID
    """
    df = run_query(sql, (str(as_of_date),))
    if not df.empty:
        df["InAfter"]  = pd.to_numeric(df["InAfter"],  errors="coerce").fillna(0).astype(int)
        df["OutAfter"] = pd.to_numeric(df["OutAfter"], errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_latest_purchase_rates() -> pd.DataFrame:
    """Most recent purchase CaseRate per item."""
    type_ph = ",".join(str(t) for t in PURCHASE_TYPES)
    sql = f"""
        WITH Ranked AS (
            SELECT
                vi.ItemID,
                vi.CaseRate,
                h.VoucherDate,
                ROW_NUMBER() OVER (
                    PARTITION BY vi.ItemID
                    ORDER BY h.VoucherDate DESC
                ) AS rn
            FROM TrVocItem vi
            JOIN TrVocHead h
                ON  h.TransTypeID = vi.TransTypeID
                AND h.VoucherNo   = vi.VoucherNo
            WHERE h.TransTypeID IN ({type_ph})
              AND h.Cancelled   = 'N'
              AND vi.CaseRate   > 0
              AND vi.FreeItemYN = 'N'
              AND vi.ItemID     LIKE 'I%'
        )
        SELECT ItemID, CaseRate AS LatestRate, VoucherDate AS LatestRateDate
        FROM Ranked
        WHERE rn = 1
    """
    df = run_query(sql)
    if not df.empty:
        df["LatestRate"] = pd.to_numeric(df["LatestRate"], errors="coerce").fillna(0.0)
        df["LatestRateDate"] = pd.to_datetime(df["LatestRateDate"], errors="coerce")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_sales_velocity(as_of_date: date, days_back: int = 30) -> pd.DataFrame:
    """Per-item sales cases in last N days + last sale date."""
    sql = f"""
        SELECT
            vi.ItemID,
            SUM(CAST(vi.CaseQty AS BIGINT))  AS Cases,
            MAX(h.VoucherDate)                AS LastSale
        FROM TrVocItem vi
        JOIN TrVocHead h
            ON  h.TransTypeID = vi.TransTypeID
            AND h.VoucherNo   = vi.VoucherNo
        JOIN MsTransType mt ON mt.TransTypeID = vi.TransTypeID
        WHERE h.Cancelled  = 'N'
          AND mt.QtyInOut  = 'O'
          AND vi.FreeItemYN = 'N'
          AND vi.ItemID     LIKE 'I%'
          AND h.VoucherDate BETWEEN DATEADD(DAY, -{days_back}, ?) AND ?
        GROUP BY vi.ItemID
    """
    df = run_query(sql, (str(as_of_date), str(as_of_date)))
    if not df.empty:
        df["Cases"]    = pd.to_numeric(df["Cases"], errors="coerce").fillna(0).astype(int)
        df["LastSale"] = pd.to_datetime(df["LastSale"], errors="coerce")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
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


def _build_stock_df(as_of: date) -> pd.DataFrame:
    """Combine MsItemBatchOpening with movement roll-back to get stock at as_of."""
    stock_df = _load_current_stock()
    if stock_df.empty:
        return stock_df

    today = date.today()
    if as_of >= today:
        # Live: use current closing directly
        return stock_df.copy()

    # Roll back movements between as_of and today
    moves = _load_movements_since(as_of)
    if moves.empty:
        return stock_df.copy()

    merged = stock_df.merge(moves, on="ItemID", how="left").fillna(
        {"InAfter": 0, "OutAfter": 0}
    )
    # Historical = current + Out_after - In_after  (reverse direction)
    merged["ClosingBottles"] = (
        merged["ClosingBottles"] + merged["OutAfter"] - merged["InAfter"]
    ).clip(lower=0).astype(int)
    merged["ClosingCases"] = merged.apply(
        lambda r: (r["ClosingBottles"] / r["BottlesPerCase"]) if r["BottlesPerCase"] else 0.0,
        axis=1,
    ).round(1)
    return merged.drop(columns=["InAfter", "OutAfter"], errors="ignore")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def _section_kpis(stock_df: pd.DataFrame, rates_df: pd.DataFrame) -> None:
    in_stock = stock_df[stock_df["ClosingCases"] > 0]
    total_items  = len(in_stock)
    total_cases  = float(in_stock["ClosingCases"].sum())
    out_of_stock = int((stock_df["ClosingCases"] <= 0).sum())

    # Inventory value = ClosingCases × LatestRate
    merged = stock_df.merge(rates_df[["ItemID", "LatestRate"]], on="ItemID", how="left")
    merged["LatestRate"] = pd.to_numeric(merged["LatestRate"], errors="coerce").fillna(0.0)
    merged["Value"]      = merged["ClosingCases"] * merged["LatestRate"]
    total_value = float(merged["Value"].sum())

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(_kpi_card(
            "Items in Stock",
            f"{total_items:,}",
            f"of {len(stock_df):,} total items",
            "#6b7280", _KPI_COLORS[0],
        ), unsafe_allow_html=True)
    with c2:
        st.markdown(_kpi_card(
            "Total Cases",
            f"{total_cases:,.0f}",
            f"{int(in_stock['ClosingBottles'].sum()):,} bottles",
            "#6b7280", _KPI_COLORS[1],
        ), unsafe_allow_html=True)
    with c3:
        st.markdown(_kpi_card(
            "Estimated Value",
            f"₹{total_value/1e7:.2f} Cr",
            f"At latest purchase rates",
            "#6b7280", _KPI_COLORS[2],
        ), unsafe_allow_html=True)
    with c4:
        st.markdown(_kpi_card(
            "Out of Stock",
            f"{out_of_stock:,}",
            "items with zero closing",
            "#dc2626" if out_of_stock else "#6b7280", _KPI_COLORS[3],
        ), unsafe_allow_html=True)


def _section_by_principal(stock_df: pd.DataFrame,
                          rates_df: pd.DataFrame) -> None:
    st.markdown("##### Stock by Principal")
    if stock_df.empty:
        st.info("No stock data.")
        return
    merged = stock_df.merge(rates_df[["ItemID", "LatestRate"]], on="ItemID", how="left")
    merged["LatestRate"] = pd.to_numeric(merged["LatestRate"], errors="coerce").fillna(0.0)
    merged["Value"]      = merged["ClosingCases"] * merged["LatestRate"]

    g = (
        merged[merged["ClosingCases"] > 0]
        .groupby("Principal", as_index=False)
        .agg(Items=("ItemID", "nunique"),
             Cases=("ClosingCases", "sum"),
             Value=("Value", "sum"))
        .sort_values("Value", ascending=False)
    )
    st.dataframe(
        g.rename(columns={
            "Items": "Items in Stock", "Cases": "Total Cases", "Value": "Estimated Value ₹",
        }).style.format({
            "Items in Stock": "{:,}", "Total Cases": "{:,.0f}",
            "Estimated Value ₹": format_inr,
        }),
        use_container_width=True, hide_index=True,
    )


def _section_top_items(stock_df: pd.DataFrame, rates_df: pd.DataFrame) -> None:
    st.markdown("##### Top 20 items by stock value")
    if stock_df.empty:
        st.info("No stock data.")
        return

    merged = stock_df.merge(rates_df[["ItemID", "LatestRate"]], on="ItemID", how="left")
    merged["LatestRate"] = pd.to_numeric(merged["LatestRate"], errors="coerce").fillna(0.0)
    merged["Value"]      = merged["ClosingCases"] * merged["LatestRate"]
    top = merged[merged["Value"] > 0].sort_values("Value", ascending=False).head(20).copy()
    if top.empty:
        st.info("No items with value > 0.")
        return

    # Bar chart
    top_chart = top.sort_values("Value", ascending=True)  # asc so largest on top
    colors = [_PRINCIPAL_COLOR.get(p, "#B4B2A9") for p in top_chart["Principal"]]
    top_chart["ValueCr"] = top_chart["Value"] / 1e7
    fig = go.Figure(go.Bar(
        x=top_chart["ValueCr"], y=top_chart["ItemDescription"],
        orientation="h",
        marker_color=colors,
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

    # Companion table
    tbl = top[["BrandName", "ItemDescription", "ClosingCases",
               "LatestRate", "Value"]].rename(columns={
        "BrandName":       "Brand",
        "ItemDescription": "Item",
        "ClosingCases":    "Cases",
        "LatestRate":      "Latest Rate",
        "Value":           "Stock Value",
    })
    st.dataframe(
        tbl.style.format({
            "Cases":        "{:,.0f}",
            "Latest Rate":  "₹{:,.0f}",
            "Stock Value":  format_inr,
        }),
        use_container_width=True, hide_index=True,
    )


def _section_slow_movers(stock_df: pd.DataFrame, vel_df: pd.DataFrame,
                         rates_df: pd.DataFrame, threshold_cases: int = 50) -> None:
    st.markdown("##### Slow Movers — Capital tied up")
    st.caption(f"Closing > {threshold_cases} cases AND last-30d sales < 10 cases.")

    if stock_df.empty:
        st.info("No stock data.")
        return

    merged = stock_df.merge(vel_df[["ItemID", "Cases", "LastSale"]],
                            on="ItemID", how="left").rename(columns={"Cases": "Sales30"})
    merged["Sales30"] = pd.to_numeric(merged["Sales30"], errors="coerce").fillna(0).astype(int)
    merged = merged.merge(rates_df[["ItemID", "LatestRate"]], on="ItemID", how="left")
    merged["LatestRate"] = pd.to_numeric(merged["LatestRate"], errors="coerce").fillna(0.0)
    merged["Value"]      = merged["ClosingCases"] * merged["LatestRate"]

    slow = merged[
        (merged["ClosingCases"] > threshold_cases) & (merged["Sales30"] < 10)
    ].copy()
    if slow.empty:
        st.success("No slow movers — every high-stock item is moving.")
        return

    slow["DailyAvg"]  = slow["Sales30"] / 30
    slow["DaysCover"] = slow.apply(
        lambda r: (r["ClosingCases"] / r["DailyAvg"]) if r["DailyAvg"] > 0 else 9999,
        axis=1,
    )
    slow = slow.sort_values("DaysCover", ascending=False).head(30)

    def _row_style(row):
        bg = "background-color:#fef3c7" if 30 < row["DaysCover"] <= 90 else \
             ("background-color:#fee2e2" if row["DaysCover"] > 90 else "")
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
            "Last 30d Cases":  "{:,}",
            "Days of Cover":   lambda x: "9999+" if x >= 9999 else f"{x:,.0f}",
            "Stock Value":     format_inr,
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_out_of_stock(stock_df: pd.DataFrame, vel_df: pd.DataFrame,
                          vel_90: pd.DataFrame) -> None:
    st.markdown("##### ⚠️ Out of Stock — Risk of lost sales")
    st.caption("Items with zero closing AND proven demand (>5 cases in last 30 days).")

    if stock_df.empty:
        st.info("No stock data.")
        return

    s = stock_df.merge(vel_df[["ItemID", "Cases", "LastSale"]],
                       on="ItemID", how="left").rename(columns={"Cases": "Sales30"})
    s["Sales30"] = pd.to_numeric(s["Sales30"], errors="coerce").fillna(0).astype(int)
    s = s.merge(vel_90[["ItemID", "Cases"]].rename(columns={"Cases": "Sales90"}),
                on="ItemID", how="left")
    s["Sales90"] = pd.to_numeric(s["Sales90"], errors="coerce").fillna(0).astype(int)

    risk = s[(s["ClosingCases"] <= 0) & (s["Sales30"] > 5)].copy()
    if risk.empty:
        st.success("No out-of-stock items with recent demand.")
        return
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
        disp.style.format({"Last 30d Cases": "{:,}", "Last 90d Cases": "{:,}"}),
        use_container_width=True, hide_index=True,
    )


def _section_days_of_cover(stock_df: pd.DataFrame, vel_df: pd.DataFrame) -> None:
    st.markdown("##### Days of Cover — Top 30 selling items")
    st.caption("Green > 30 days · Amber 15–30 · Red < 15 (urgent reorder)")

    if stock_df.empty or vel_df.empty:
        st.info("Insufficient data.")
        return

    merged = stock_df.merge(vel_df[["ItemID", "Cases"]],
                            on="ItemID", how="left").rename(columns={"Cases": "Sales30"})
    merged["Sales30"] = pd.to_numeric(merged["Sales30"], errors="coerce").fillna(0).astype(int)
    sellers = merged[merged["Sales30"] > 0].copy()
    if sellers.empty:
        st.info("No selling items in the last 30 days.")
        return
    sellers["DailyAvg"]  = sellers["Sales30"] / 30
    sellers["DaysCover"] = sellers.apply(
        lambda r: (r["ClosingCases"] / r["DailyAvg"]) if r["DailyAvg"] > 0 else 9999,
        axis=1,
    )
    top_sellers = sellers.sort_values("Sales30", ascending=False).head(30)

    def _doc_style(row):
        doc = row["Days of Cover"]
        try:
            v = float(str(doc).replace("+", "").replace(",", ""))
        except ValueError:
            return [""] * len(row)
        if v >= 30:  bg = "background-color:#dcfce7"   # green
        elif v >= 15: bg = "background-color:#fef3c7"  # amber
        else:         bg = "background-color:#fee2e2"  # red
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

    today = date.today()
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

    with st.spinner("Loading stock + movement data…"):
        stock_df = _build_stock_df(as_of_date)
        rates_df = _load_latest_purchase_rates()
        vel_30   = _load_sales_velocity(as_of_date, days_back=30)
        vel_90   = _load_sales_velocity(as_of_date, days_back=90)

    if stock_df.empty:
        st.error("No stock data returned.")
        return

    # Principal filter
    if principal_filter:
        stock_df = stock_df[stock_df["Principal"].isin(principal_filter)]

    # View filter (applied to "All items" section + KPIs use full set)
    if view_filter == "Low stock (<10 cases)":
        view_df = stock_df[stock_df["ClosingCases"] < 10]
    elif view_filter == "Out of stock":
        view_df = stock_df[stock_df["ClosingCases"] <= 0]
    elif view_filter == "Slow movers":
        # built in section, here we just keep all
        view_df = stock_df
    else:
        view_df = stock_df

    if as_of_date < today:
        st.info(f"Showing historical stock as of {as_of_date.strftime('%d %b %Y')} "
                f"(rolled back {(today - as_of_date).days} days from live).")

    _section_kpis(stock_df, rates_df)
    st.divider()
    _section_by_principal(stock_df, rates_df)
    st.divider()
    _section_top_items(view_df, rates_df)
    st.divider()
    _section_slow_movers(stock_df, vel_30, rates_df)
    st.divider()
    _section_out_of_stock(stock_df, vel_30, vel_90)
    st.divider()
    _section_days_of_cover(stock_df, vel_30)
