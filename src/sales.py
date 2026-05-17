from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, section_header

# ── Sales transaction type IDs (ShortName='MS', PostingType='D', ItemYN='Y')
SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Chart theme constants
_BG     = "rgba(0,0,0,0)"
_GOLD   = "#E8A838"
_GRID   = dict(gridcolor="#E8E8E8")
_LAYOUT = dict(
    paper_bgcolor=_BG, plot_bgcolor=_BG,
    font=dict(color="#1A1A1A"),
    template="plotly_white",
    margin=dict(t=30, b=20),
)
_PAL    = px.colors.qualitative.Bold


# ── Indian rupee formatter for table cells (exact, with grouping) ──────────

def _inr(value: float) -> str:
    neg = value < 0
    s = str(int(round(abs(value))))
    if len(s) > 3:
        tail, head = s[-3:], s[:-3]
        parts: list[str] = []
        while head:
            parts.append(head[-2:])
            head = head[:-2]
        s = ",".join(reversed(parts)) + "," + tail
    return ("−" if neg else "") + "₹" + s


# ── Data loaders (all cached 5 minutes) ────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _load_brands() -> pd.DataFrame:
    return run_query(
        "SELECT BrandID, BrandName FROM MsBrandMaster ORDER BY BrandName"
    )


@st.cache_data(ttl=300, show_spinner=False)
def _load_salesmen() -> pd.DataFrame:
    return run_query(
        "SELECT SalesManID, FullName FROM MsSalesmanMaster "
        "WHERE ResignDate IS NULL ORDER BY FullName"
    )


@st.cache_data(ttl=300, show_spinner=False)
def _load_fy_years(start: date, end: date) -> tuple[str, ...]:
    """Ask the DB which FinancialYear values exist in TrVocItem for this range.

    TrVocItem stores rows for BOTH the prior FY and the current FY for the
    same (TransTypeID, VoucherNo, SerialNo).  We must keep only the rows
    that belong to the date range's actual financial year(s); using whatever
    strings the DB itself returns avoids any format-mismatch on cloud hosts.

    Params order in the query: start, end  →  SALES_TYPES
    """
    type_ph = ",".join("?" * len(SALES_TYPES))
    df = run_query(
        f"""
        SELECT DISTINCT vi.FinancialYear
        FROM TrVocItem vi
        JOIN TrVocHead h
            ON  h.TransTypeID = vi.TransTypeID
            AND h.VoucherNo   = vi.VoucherNo
        WHERE h.VoucherDate BETWEEN ? AND ?
          AND h.TransTypeID IN ({type_ph})
          AND h.Cancelled = 'N'
        """,
        (str(start), str(end)) + SALES_TYPES,
    )
    if df.empty:
        return ()

    # Keep only the FY(s) that actually fall within the date range.
    # The DB may return the prior FY tag; we discard it by comparing the
    # year-start embedded in the string (e.g. '2026' in '2026-2027') to the
    # April-1 boundary of the selected range.
    def _fy_start_year(d: date) -> int:
        return d.year if d.month >= 4 else d.year - 1

    valid_starts = {_fy_start_year(start), _fy_start_year(end)}
    result = tuple(
        fy for fy in df["FinancialYear"].tolist()
        if fy and int(fy[:4]) in valid_starts
    )
    # Fallback: if the filter removed everything, use all DB-returned values
    return result if result else tuple(df["FinancialYear"].dropna().tolist())


def _build_query(
    brand_ids: tuple,
    salesman_ids: tuple,
    fy_years: tuple,
) -> tuple[str, tuple]:
    """Return (sql, params) with optional brand / salesman / FY IN-filters.

    FinancialYear filter: TrVocItem contains duplicate rows for the same
    (TransTypeID, VoucherNo, SerialNo) tagged with the prior year AND the
    current year.  Filtering vi.FinancialYear to only the correct FY(s)
    removes the duplicates and matches the ERP brandwise sales figure.

    IMPORTANT — params order must match ? placeholders left-to-right in SQL:
        1. SALES_TYPES   → TransTypeID IN (...)        [WHERE]
        2. fy_years      → FinancialYear IN (...)       [WHERE]
        3. brand_ids     → BrandID IN (...)             [WHERE, optional]
        4. salesman_ids  → SalesManID IN (...)          [WHERE, optional]
        5. start, end    → VoucherDate BETWEEN          [WHERE]

    The FinancialYear IN clause lives in WHERE (not the JOIN) so its
    placeholders come after the TransTypeID placeholders — keeping the
    params tuple in the same left-to-right order as the SQL.
    """
    type_ph   = ",".join("?" * len(SALES_TYPES))
    fy_ph     = ",".join("?" * len(fy_years))
    brand_sql = sm_sql = fy_sql = ""
    extra: tuple = ()

    if fy_years:
        fy_sql = f"AND vi.FinancialYear IN ({fy_ph})"
        extra += tuple(fy_years)
    if brand_ids:
        brand_sql = f"AND vi.BrandID IN ({','.join('?' * len(brand_ids))})"
        extra += brand_ids
    if salesman_ids:
        sm_sql = f"AND h.SalesManID IN ({','.join('?' * len(salesman_ids))})"
        extra += salesman_ids

    sql = f"""
        SELECT
            h.VoucherDate,
            h.VoucherNo,
            h.SalesManID,
            ISNULL(sm.FullName,       'Unassigned') AS SalesmanName,
            ISNULL(d.PartyID,         '')            AS PartyID,
            ISNULL(p.PartyName,       'Unknown')     AS PartyName,
            ISNULL(b.BrandName,       'Unknown')     AS BrandName,
            ISNULL(im.ItemDescription,'Unknown')     AS ItemDescription,
            ISNULL(im.LiquorSize,     '')            AS LiquorSize,
            CAST(vi.CaseQty        AS BIGINT) AS CaseQty,
            CAST(vi.BottleQty      AS BIGINT) AS BottleQty,
            CAST(vi.TotalBottleQty AS BIGINT) AS TotalBottleQty,
            CAST(vi.TotalAmount    AS FLOAT)  AS TotalAmount
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID LIKE 'I%'          -- exclude service/charge rows (S%)
        LEFT JOIN (
            -- One party per voucher: pick the largest debit (= the customer)
            SELECT TransTypeID, VoucherNo, PartyID
            FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (
                           PARTITION BY TransTypeID, VoucherNo
                           ORDER BY Amount DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator = 'D'
            ) x
            WHERE rn = 1
        ) d  ON  d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        LEFT JOIN MsPartyMaster    p  ON p.PartyID    = d.PartyID
        LEFT JOIN MsItemMaster     im ON im.ItemID    = vi.ItemID
        LEFT JOIN MsBrandMaster    b  ON b.BrandID    = im.BrandID
        LEFT JOIN MsSalesmanMaster sm ON sm.SalesManID = h.SalesManID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND vi.FreeItemYN = 'N'
          {fy_sql}
          {brand_sql}
          {sm_sql}
          AND CAST(h.VoucherDate AS date) BETWEEN CAST(? AS date) AND CAST(? AS date)
    """
    return sql, extra


@st.cache_data(ttl=300, show_spinner=False)
def _load_sales(
    start: date,
    end: date,
    brand_ids: tuple = (),
    salesman_ids: tuple = (),
) -> pd.DataFrame:
    fy_years = _load_fy_years(start, end)
    sql, extra = _build_query(brand_ids, salesman_ids, fy_years)
    # params order: SALES_TYPES → extra (fy, brand, sm) → start, end
    params = SALES_TYPES + extra + (str(start), str(end))
    df = run_query(sql, params)
    if df.empty:
        return df
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["TotalAmount"] = pd.to_numeric(df["TotalAmount"], errors="coerce").fillna(0.0)
    df["CaseQty"]     = pd.to_numeric(df["CaseQty"],     errors="coerce").fillna(0).astype(int)
    df["BottleQty"]   = pd.to_numeric(df["BottleQty"],   errors="coerce").fillna(0).astype(int)
    return df


# ── Chart builders ──────────────────────────────────────────────────────────

def _daily_trend(df: pd.DataFrame) -> go.Figure:
    agg = (
        df.groupby(df["VoucherDate"].dt.date)["TotalAmount"]
        .sum().reset_index()
        .rename(columns={"VoucherDate": "Date", "TotalAmount": "Sales"})
    )
    fig = px.line(
        agg, x="Date", y="Sales", markers=True,
        color_discrete_sequence=[_GOLD],
        labels={"Sales": "Sales (₹)", "Date": ""},
    )
    fig.update_layout(**_LAYOUT, xaxis=_GRID, yaxis=_GRID)
    fig.update_traces(line_width=2.5, marker_size=5)
    return fig


def _brand_bar(df: pd.DataFrame) -> go.Figure:
    agg = (
        df.groupby("BrandName")["TotalAmount"].sum()
        .sort_values().tail(15).reset_index()
    )
    fig = px.bar(
        agg, x="TotalAmount", y="BrandName", orientation="h",
        color="TotalAmount", color_continuous_scale=["#D6E8F7", _GOLD],
        text_auto=".2s", labels={"TotalAmount": "Sales (₹)", "BrandName": ""},
    )
    fig.update_layout(**_LAYOUT, coloraxis_showscale=False,
                      yaxis=dict(autorange="reversed", **_GRID), xaxis=_GRID)
    fig.update_traces(textposition="outside")
    return fig


def _brand_donut(df: pd.DataFrame) -> go.Figure:
    agg = (
        df.groupby("BrandName")["TotalAmount"].sum()
        .sort_values(ascending=False).reset_index()
    )
    if len(agg) > 10:
        top   = agg.head(10)
        other = pd.DataFrame([{
            "BrandName": "Others",
            "TotalAmount": agg.iloc[10:]["TotalAmount"].sum(),
        }])
        agg = pd.concat([top, other], ignore_index=True)
    fig = px.pie(
        agg, names="BrandName", values="TotalAmount",
        hole=0.42, color_discrete_sequence=_PAL,
    )
    fig.update_layout(**_LAYOUT, legend=dict(font=dict(size=11)))
    fig.update_traces(textinfo="label+percent",
                      pull=[0.05] + [0] * (len(agg) - 1))
    return fig


def _salesman_bar(df: pd.DataFrame) -> go.Figure:
    agg = (
        df.groupby("SalesmanName")["TotalAmount"].sum()
        .sort_values(ascending=False).reset_index()
    )
    fig = px.bar(
        agg, x="SalesmanName", y="TotalAmount",
        color="SalesmanName", color_discrete_sequence=_PAL,
        text_auto=".2s", labels={"TotalAmount": "Sales (₹)", "SalesmanName": ""},
    )
    fig.update_layout(**_LAYOUT, showlegend=False,
                      xaxis=dict(tickangle=-30, **_GRID), yaxis=_GRID)
    fig.update_traces(textposition="outside")
    return fig


def _customer_bar(df: pd.DataFrame) -> go.Figure:
    agg = (
        df[df["PartyName"] != "Unknown"]
        .groupby("PartyName")["TotalAmount"].sum()
        .sort_values().tail(10).reset_index()
    )
    fig = px.bar(
        agg, x="TotalAmount", y="PartyName", orientation="h",
        color="TotalAmount", color_continuous_scale=["#D6E8F7", _GOLD],
        text_auto=".2s", labels={"TotalAmount": "Sales (₹)", "PartyName": ""},
    )
    fig.update_layout(**_LAYOUT, coloraxis_showscale=False,
                      yaxis=dict(autorange="reversed", **_GRID), xaxis=_GRID)
    fig.update_traces(textposition="outside")
    return fig


def _size_bar(df: pd.DataFrame) -> go.Figure:
    agg = (
        df[df["LiquorSize"] != ""]
        .groupby("LiquorSize")["TotalAmount"].sum()
        .sort_values(ascending=False).reset_index()
    )
    fig = px.bar(
        agg, x="LiquorSize", y="TotalAmount",
        color="LiquorSize", color_discrete_sequence=_PAL,
        text_auto=".2s", labels={"TotalAmount": "Sales (₹)", "LiquorSize": "Size"},
    )
    fig.update_layout(**_LAYOUT, showlegend=False,
                      xaxis=_GRID, yaxis=_GRID)
    fig.update_traces(textposition="outside")
    return fig


# ── Page entry point ────────────────────────────────────────────────────────

def render():
    st.title("Sales & Revenue")
    st.caption("Invoicewise sales performance for KWPL | Source: TEKNIK ERP")
    st.divider()

    # ── Pre-load filter option lists ──────────────────────────────────────
    brands_df   = _load_brands()
    salesmen_df = _load_salesmen()

    # ── Inline filter row ─────────────────────────────────────────────────
    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

    with st.container():
        fc1, fc2, fc3, fc4 = st.columns([1.1, 1.1, 2.4, 2.4])
        with fc1:
            start = st.date_input("From", value=fy_start, key="s_from")
        with fc2:
            end = st.date_input("To", value=today, key="s_to")

        # Brand filter
        brand_map: dict[str, int] = {}
        if not brands_df.empty:
            brand_map = dict(zip(brands_df["BrandName"], brands_df["BrandID"].astype(int)))
        with fc3:
            sel_brands = st.multiselect("Brand", options=sorted(brand_map), key="s_brands")
        brand_ids = tuple(brand_map[b] for b in sel_brands)

        # Salesman filter
        sm_map: dict[str, str] = {}
        if not salesmen_df.empty:
            sm_map = dict(zip(salesmen_df["FullName"], salesmen_df["SalesManID"]))
        with fc4:
            sel_sm = st.multiselect("Salesman", options=sorted(sm_map), key="s_sm")
        salesman_ids = tuple(sm_map[s] for s in sel_sm)

    if start > end:
        st.warning("Start date must be before end date.")
        return

    # ── Fetch main period data ─────────────────────────────────────────────
    with st.spinner("Fetching sales data..."):
        df = _load_sales(start, end, brand_ids, salesman_ids)

    if df.empty:
        st.error(
            "No sales data returned for the selected range. "
            "Check your database connection or adjust the date filter."
        )
        st.info(
            "Ensure `.env` is filled and the `KWPL` login has access to `KW2526`. "
            "Run `python db_explorer.py` to verify connectivity."
        )
        return

    # ── Fetch prior period for delta (same number of days, immediately before)
    n_days     = max((end - start).days, 1)
    prev_end   = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=n_days - 1)
    df_prev = _load_sales(prev_start, prev_end, brand_ids, salesman_ids)

    # ── KPI calculations ───────────────────────────────────────────────────
    total_rev   = df["TotalAmount"].sum()
    total_cases = int(df["CaseQty"].sum())
    total_inv   = df["VoucherNo"].nunique()
    top_brand   = df.groupby("BrandName")["TotalAmount"].sum().idxmax()

    prev_rev  = df_prev["TotalAmount"].sum() if not df_prev.empty else 0.0
    rev_delta = format_inr(total_rev - prev_rev) if prev_rev else None

    # ── KPI cards ─────────────────────────────────────────────────────────
    section_header(
        "Key Performance Indicators",
        f"{start.strftime('%d %b %Y')}  →  {end.strftime('%d %b %Y')}",
    )
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("Total Revenue", format_inr(total_rev),
                  delta=rev_delta, delta_color="normal")
    with k2:
        st.metric("Total Cases Sold", f"{total_cases:,}")
    with k3:
        st.metric("Total Invoices", f"{total_inv:,}")
    with k4:
        st.metric("Top Brand", top_brand)

    st.divider()

    # ── Daily Sales Trend (full width) ─────────────────────────────────────
    section_header("Daily Sales Trend")
    st.plotly_chart(_daily_trend(df), use_container_width=True)

    # ── Brand bar + Brand donut (side by side) ─────────────────────────────
    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Sales by Brand (Top 15)")
        st.plotly_chart(_brand_bar(df), use_container_width=True)
    with col_b:
        st.subheader("Brand Mix")
        st.plotly_chart(_brand_donut(df), use_container_width=True)

    # ── Salesman (full width) ──────────────────────────────────────────────
    st.markdown("---")
    section_header("Sales by Salesman")
    st.plotly_chart(_salesman_bar(df), use_container_width=True)

    # ── Customers + Liquor Size (side by side) ─────────────────────────────
    st.markdown("---")
    col_c, col_d = st.columns(2)
    with col_c:
        st.subheader("Top 10 Customers")
        st.plotly_chart(_customer_bar(df), use_container_width=True)
    with col_d:
        st.subheader("Liquor Size Analysis")
        st.plotly_chart(_size_bar(df), use_container_width=True)

    # ── Transactions table ─────────────────────────────────────────────────
    st.markdown("---")
    section_header("Recent Transactions", "Latest 200 records · sorted by date desc")

    disp = df.sort_values("VoucherDate", ascending=False).head(200).copy()
    disp["Date"]    = disp["VoucherDate"].dt.strftime("%d %b %Y")
    disp["Amount"]  = disp["TotalAmount"].apply(_inr)
    disp["Cases"]   = disp["CaseQty"].astype(int)
    disp["Bottles"] = disp["BottleQty"].astype(int)

    st.dataframe(
        disp[[
            "Date", "VoucherNo", "PartyName", "BrandName",
            "ItemDescription", "LiquorSize", "Cases", "Bottles", "Amount",
        ]].rename(columns={
            "VoucherNo":        "Invoice",
            "PartyName":        "Party",
            "BrandName":        "Brand",
            "ItemDescription":  "Item",
            "LiquorSize":       "Size",
        }),
        use_container_width=True,
        hide_index=True,
    )


