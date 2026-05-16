import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from datetime import date, timedelta
import numpy as np
import random

from utils.helpers import format_inr, kpi_card, date_filter, section_header

# ---------------------------------------------------------------------------
# Mock data generator — replace calls to _mock_*() with run_query() later
# ---------------------------------------------------------------------------

BRANDS = ["Royal Stag", "Officer's Choice", "McDowell's", "Kingfisher", "Budweiser",
          "Old Monk", "Jack Daniel's", "Johnnie Walker", "Bira 91", "Tuborg"]

CUSTOMERS = [f"Customer {chr(65+i)}" for i in range(15)]


def _mock_transactions(start: date, end: date) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n_days = max((end - start).days + 1, 1)
    rows = []
    for i in range(n_days * 8):
        order_date = start + timedelta(days=rng.integers(0, n_days))
        brand = rng.choice(BRANDS)
        customer = rng.choice(CUSTOMERS)
        qty = int(rng.integers(5, 150))
        rate = float(rng.uniform(200, 3000))
        rows.append({
            "order_date": order_date,
            "brand": brand,
            "customer": customer,
            "quantity": qty,
            "rate": round(rate, 2),
            "amount": round(qty * rate, 2),
        })
    df = pd.DataFrame(rows)
    df["order_date"] = pd.to_datetime(df["order_date"])
    return df.sort_values("order_date", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------

CHART_BG = "rgba(0,0,0,0)"
GOLD = "#E8A838"
PALETTE = px.colors.qualitative.Bold


def _sales_by_brand(df: pd.DataFrame) -> go.Figure:
    agg = (df.groupby("brand")["amount"].sum()
             .sort_values(ascending=False)
             .reset_index())
    fig = px.bar(
        agg, x="brand", y="amount",
        color="brand", color_discrete_sequence=PALETTE,
        labels={"brand": "Brand", "amount": "Sales (₹)"},
        text_auto=".2s",
    )
    fig.update_layout(
        paper_bgcolor=CHART_BG, plot_bgcolor=CHART_BG,
        showlegend=False, margin=dict(t=20, b=20),
        yaxis=dict(gridcolor="#2a2d3e"),
        font=dict(color="#FAFAFA"),
    )
    fig.update_traces(textposition="outside")
    return fig


def _daily_trend(df: pd.DataFrame) -> go.Figure:
    agg = (df.groupby(df["order_date"].dt.date)["amount"].sum()
             .reset_index()
             .rename(columns={"order_date": "date"}))
    fig = px.line(
        agg, x="date", y="amount",
        labels={"date": "Date", "amount": "Daily Sales (₹)"},
        markers=True,
        color_discrete_sequence=[GOLD],
    )
    fig.update_layout(
        paper_bgcolor=CHART_BG, plot_bgcolor=CHART_BG,
        margin=dict(t=20, b=20),
        xaxis=dict(gridcolor="#2a2d3e"),
        yaxis=dict(gridcolor="#2a2d3e"),
        font=dict(color="#FAFAFA"),
    )
    fig.update_traces(line_width=2.5, marker_size=5)
    return fig


def _sales_by_customer(df: pd.DataFrame, top_n: int = 10) -> go.Figure:
    agg = (df.groupby("customer")["amount"].sum()
             .sort_values(ascending=False)
             .head(top_n)
             .reset_index())
    fig = px.bar(
        agg, x="amount", y="customer",
        orientation="h",
        color="amount",
        color_continuous_scale=["#1A3A5C", GOLD],
        labels={"customer": "Customer", "amount": "Sales (₹)"},
        text_auto=".2s",
    )
    fig.update_layout(
        paper_bgcolor=CHART_BG, plot_bgcolor=CHART_BG,
        showlegend=False, margin=dict(t=20, b=20),
        yaxis=dict(autorange="reversed", gridcolor="#2a2d3e"),
        xaxis=dict(gridcolor="#2a2d3e"),
        coloraxis_showscale=False,
        font=dict(color="#FAFAFA"),
    )
    fig.update_traces(textposition="outside")
    return fig


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render():
    st.title("Sales & Revenue")
    st.caption("All figures in Indian Rupees (₹). Using sample data — connect your DB to see live numbers.")

    # ── Filters ────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("#### Filters")
        start, end = date_filter(key_prefix="sales")

    # ── Data ───────────────────────────────────────────────────────────────
    # TODO: replace with real query once table names are confirmed
    # from db import run_query
    # df = run_query(f"""
    #     SELECT order_date, brand, customer, quantity, rate, amount
    #     FROM   dbo.SalesTransactions
    #     WHERE  order_date BETWEEN '{start}' AND '{end}'
    # """)
    df = _mock_transactions(start, end)

    if df.empty:
        st.warning("No data found for the selected date range.")
        return

    # ── KPI Cards ──────────────────────────────────────────────────────────
    total_sales = df["amount"].sum()
    total_orders = len(df)
    avg_order_value = df["amount"].mean()
    top_brand = df.groupby("brand")["amount"].sum().idxmax()

    section_header("Key Performance Indicators")
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        kpi_card("Total Sales", format_inr(total_sales))
    with k2:
        kpi_card("Total Orders", f"{total_orders:,}")
    with k3:
        kpi_card("Avg Order Value", format_inr(avg_order_value))
    with k4:
        kpi_card("Top Brand", top_brand)

    st.divider()

    # ── Charts ─────────────────────────────────────────────────────────────
    section_header("Sales by Brand")
    st.plotly_chart(_sales_by_brand(df), use_container_width=True)

    section_header("Daily Sales Trend")
    st.plotly_chart(_daily_trend(df), use_container_width=True)

    section_header("Top 10 Customers by Sales")
    st.plotly_chart(_sales_by_customer(df), use_container_width=True)

    # ── Recent Transactions ────────────────────────────────────────────────
    section_header("Recent Transactions")
    display_df = df.head(50).copy()
    display_df["order_date"] = display_df["order_date"].dt.strftime("%d %b %Y")
    display_df["amount"] = display_df["amount"].apply(lambda x: f"₹{x:,.2f}")
    display_df["rate"] = display_df["rate"].apply(lambda x: f"₹{x:,.2f}")
    display_df.columns = ["Date", "Brand", "Customer", "Qty", "Rate", "Amount"]
    st.dataframe(display_df, use_container_width=True, hide_index=True)


render()
