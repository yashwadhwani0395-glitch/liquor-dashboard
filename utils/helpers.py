import calendar
import pandas as pd
import streamlit as st
from datetime import date, timedelta


def current_month_range() -> tuple[date, date]:
    today = date.today()
    start = today.replace(day=1)
    return start, today


# ─── MTD helpers (single source of truth for "1st-to-today" windows) ────────
# Used by every benchmark card across sales.py / salesman.py / future pages.
# Key invariant: the day cutoff is ALWAYS derived from today.day, never
# hard-coded — so "vs Same Month LY" on May-19 compares May 1-19 each year,
# on May-20 it auto-updates to May 1-20, on May-31 it's full month.

def same_mtd_window(
    reference_date: date | pd.Timestamp,
    months_back: int,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (start, end) Timestamps for the SAME month-to-date window
    `months_back` calendar months before `reference_date`.

    Example: reference May-19-2026, months_back=1 →
             (Apr-1-2026, Apr-19-2026)
             reference May-19-2026, months_back=12 →
             (May-1-2025, May-19-2025)

    If the target month is shorter than reference_date.day (e.g. trying to
    map May-31 back to Feb-31), the end date is capped at month-end.
    """
    ref = pd.Timestamp(reference_date).normalize()
    target_start = (ref.replace(day=1) - pd.DateOffset(months=months_back)) \
        .normalize()
    # Last day of target month
    _, last_dom = calendar.monthrange(target_start.year, target_start.month)
    target_end_day = min(ref.day, last_dom)
    target_end = target_start.replace(day=target_end_day)
    return target_start, target_end


def mtd_sum_in_window(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> float:
    """Sum `value_col` from rows where `date_col` ∈ [start, end] inclusive.
    Returns 0.0 on empty / missing column."""
    if df.empty or date_col not in df.columns or value_col not in df.columns:
        return 0.0
    mask = (df[date_col] >= start) & (df[date_col] <= end)
    if not mask.any():
        return 0.0
    return float(df.loc[mask, value_col].sum())


def mtd_label(reference_date: date | pd.Timestamp) -> str:
    """Returns '1-N <Mon>' label for use in card subtitles.
    Example: 1-19 May  ·  1-31 Mar (when run on last day of March)."""
    ref = pd.Timestamp(reference_date).normalize()
    return f"1-{ref.day} {ref.strftime('%b')}"


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


# Plain case expression — kegs counted as 1 physical unit (no volume
# conversion). Same shape as CASES_SQL_EXPR's ELSE branch.
CASES_SQL_PLAIN: str = (
    "(CAST(vi.TotalBottleQty AS decimal(18,4)) / NULLIF(im.BottlesPerCase, 0))"
)


def cases_sql(keg_aware: bool) -> str:
    """Return the case-equivalent SQL fragment.
    keg_aware=True  → kegs volume-converted (20LT=2.56 … 50LT=6.41 cases).
    keg_aware=False → kegs counted as 1 case (plain BottlesPerCase math)."""
    return CASES_SQL_EXPR if keg_aware else CASES_SQL_PLAIN


def keg_mode_toggle(key: str, *, label: str = "Keg counting",
                    default_keg_aware: bool = True) -> bool:
    """Render a small radio to switch keg case-counting. Returns True for the
    volume-converted (keg-aware) mode, False for plain (1 keg = 1 case).
    default_keg_aware sets which option is pre-selected — keg-aware for
    Purchase/Sales (MIS basis), plain for Inventory stock (the ERP Stock &
    Sale report counts kegs as raw units)."""
    import streamlit as _st
    opts = ["Volume-converted (20LT=2.56 · 30LT=3.85 · 50LT=6.41 cs)",
            "Count keg as 1 case"]
    choice = _st.radio(
        label, opts,
        index=0 if default_keg_aware else 1, horizontal=True, key=key,
        help="Volume-converted matches the ERP/MIS case basis used on "
             "Purchase & Sales. 'Count keg as 1 case' shows raw keg units "
             "(matches the ERP Stock & Sale report).",
    )
    return choice.startswith("Volume")


def safe_section(title: str, render_func, *args, **kwargs):
    """Run a render function inside an error boundary.

    If the section crashes, surface a friendly error and a collapsible
    traceback so the rest of the page still renders.
    """
    try:
        render_func(*args, **kwargs)
    except Exception as exc:
        st.error(
            f"⚠️ Section **{title}** failed to load: "
            f"`{type(exc).__name__}`: {str(exc)[:200]}"
        )
        with st.expander("Technical details", expanded=False):
            import traceback
            st.code(traceback.format_exc())


def cases_sql_expr() -> str:
    """Return the KEG-aware case-equivalent SQL fragment (constant string).

    Wraps every SUM/aggregation that previously used CaseQty or
    TotalBottleQty/BottlesPerCase. Kegs are reported as 1 "bottle" each
    in TrVocItem but represent volume that maps to multiple standard cases.
    """
    return CASES_SQL_EXPR
