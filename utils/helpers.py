import pandas as pd
import streamlit as st
from datetime import date, timedelta


def current_month_range() -> tuple[date, date]:
    today = date.today()
    start = today.replace(day=1)
    return start, today


def format_inr(value: float) -> str:
    """Format a number as Indian Rupees with ₹ symbol and comma grouping."""
    if value >= 1_00_00_000:
        return f"₹{value / 1_00_00_000:.2f} Cr"
    if value >= 1_00_000:
        return f"₹{value / 1_00_000:.2f} L"
    return f"₹{value:,.0f}"


def kpi_card(label: str, value: str, delta: str = "", delta_color: str = "normal"):
    """Render a single KPI metric card."""
    st.metric(label=label, value=value, delta=delta if delta else None, delta_color=delta_color)


def date_filter(key_prefix: str = "date") -> tuple[date, date]:
    """Sidebar date range widget. Returns (start_date, end_date)."""
    default_start, default_end = current_month_range()
    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("From", value=default_start, key=f"{key_prefix}_start")
    with col2:
        end = st.date_input("To", value=default_end, key=f"{key_prefix}_end")
    if start > end:
        st.warning("Start date must be before end date.")
    return start, end


def section_header(title: str, subtitle: str = ""):
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)
    st.divider()


# ── KEG-aware case-equivalent SQL fragment ────────────────────────────────────
# Standard 1 case = 7.8 litres. KEG items have BottlesPerCase=1 in the ERP
# (each row's TotalBottleQty is the count of kegs sold). Use volume conversion
# for kegs; standard bottles-per-case math for everything else.
#
# Requires `vi` (TrVocItem) and `im` (MsItemMaster) to be the aliases in scope.
CASES_SQL_EXPR: str = """(CASE
    WHEN im.ItemDescription LIKE '%50 LT%' OR im.ItemDescription LIKE '%50LT%' OR im.ItemDescription LIKE '%50LTR%'
        THEN vi.TotalBottleQty * (50.0/7.8)
    WHEN im.ItemDescription LIKE '%30 LT%' OR im.ItemDescription LIKE '%30LT%' OR im.ItemDescription LIKE '%30LTR%'
        THEN vi.TotalBottleQty * (30.0/7.8)
    WHEN im.ItemDescription LIKE '%20 LT%' OR im.ItemDescription LIKE '%20LT%' OR im.ItemDescription LIKE '%20LTR%'
        THEN vi.TotalBottleQty * (20.0/7.8)
    ELSE CAST(vi.TotalBottleQty AS decimal(18,4)) / NULLIF(im.BottlesPerCase, 0)
END)"""


def cases_sql_expr() -> str:
    """Return the KEG-aware case-equivalent SQL fragment (constant string).

    Wraps every SUM/aggregation that previously used CaseQty or
    TotalBottleQty/BottlesPerCase. Kegs are reported as 1 "bottle" each
    in TrVocItem but represent volume that maps to multiple standard cases.
    """
    return CASES_SQL_EXPR
