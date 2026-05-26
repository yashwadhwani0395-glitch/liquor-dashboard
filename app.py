import streamlit as st
from db import get_connection_status

st.set_page_config(
    page_title="KWPL Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Global CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Hide sidebar toggle & default Streamlit chrome */
[data-testid="collapsedControl"] { display: none !important; }
#MainMenu { visibility: hidden; }
header    { visibility: hidden; }
footer    { visibility: hidden; }

/* Remove top padding so header sits flush */
.block-container { padding-top: 0 !important; }

/* ── KWPL header bar ── */
.kwpl-header {
    background: #1B4F72;
    color: #ffffff;
    padding: 0.7rem 2rem;
    margin: -1rem -1rem 1.5rem -1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 3px solid #E8A838;
}
.kwpl-logo {
    font-size: 1.6rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    color: #ffffff;
    line-height: 1.1;
}
.kwpl-sub {
    font-size: 0.72rem;
    color: rgba(255,255,255,0.72);
    margin-top: 1px;
}
.kwpl-status {
    background: rgba(168,240,198,0.18);
    border: 1px solid rgba(168,240,198,0.55);
    border-radius: 20px;
    padding: 0.25rem 0.9rem;
    font-size: 0.78rem;
    color: #a8f0c6;
    white-space: nowrap;
}
.kwpl-status-off {
    background: rgba(255,179,179,0.18);
    border: 1px solid rgba(255,179,179,0.55);
    border-radius: 20px;
    padding: 0.25rem 0.9rem;
    font-size: 0.78rem;
    color: #ffb3b3;
    white-space: nowrap;
}

/* ── Tab styling ── */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
    gap: 4px;
    border-bottom: 2px solid #E0DDD6;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
    font-size: 0.92rem;
    font-weight: 600;
    padding: 0.5rem 1.4rem;
    border-radius: 6px 6px 0 0;
    color: #555;
}
[data-testid="stTabs"] [aria-selected="true"] {
    color: #1B4F72 !important;
    border-bottom: 3px solid #1B4F72 !important;
}

/* ── Filter row styling ── */
.filter-row {
    background: #F0EFEB;
    border-radius: 8px;
    padding: 0.65rem 1rem 0.5rem 1rem;
    margin-bottom: 1rem;
}
</style>
""", unsafe_allow_html=True)

# ── Header bar ───────────────────────────────────────────────────────────────
status = get_connection_status()
status_html = (
    '<span class="kwpl-status">● DB Connected</span>'
    if status else
    '<span class="kwpl-status-off">● DB Offline</span>'
)
_hdr, _btn = st.columns([12, 1])
with _hdr:
    st.markdown(f"""
    <div class="kwpl-header">
        <div>
            <div class="kwpl-logo">KWPL</div>
            <div class="kwpl-sub">Kranti Wines Pvt. Ltd.</div>
        </div>
        {status_html}
    </div>
    """, unsafe_allow_html=True)
with _btn:
    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
    if st.button("🔄 Refresh", help="Clear cache & re-query DB",
                 key="hdr_refresh", use_container_width=True):
        st.cache_data.clear()
        # Also re-probe the connection in case it was stale
        from db import get_connection
        get_connection.clear()
        st.rerun()

# ── Navigation ───────────────────────────────────────────────────────────────
# IMPORTANT: we use radio-based navigation (NOT st.tabs) so that ONLY the
# selected page's render() runs each script pass. st.tabs executes every tab
# body on every rerun, which loaded all ~12 data-heavy pages into memory at
# once and crashed the Streamlit Cloud instance (OOM). Rendering one page at a
# time keeps peak memory to a single page.

def coming_soon(title: str, desc: str, icon: str = "🚧") -> None:
    """Render a centered placeholder + 'under construction' notice."""
    st.markdown(f"""
    <div style='padding: 60px 0 20px 0; text-align: center'>
        <div style='font-size: 56px; margin-bottom: 16px; opacity: 0.25'>{icon}</div>
        <div style='font-size: 20px; font-weight: 600; color: #1a1a1a; margin-bottom: 8px'>{title}</div>
        <div style='font-size: 13px; color: #888; max-width: 500px; margin: 0 auto 24px auto'>{desc}</div>
    </div>
    """, unsafe_allow_html=True)
    st.info(
        "🔨 This module is under construction. Other tabs are fully operational.",
        icon=None,
    )


_PAGES = ["Overview", "Purchase", "Sales", "Discounts",
          "Expenses", "Debtors Ageing", "Cash Flow", "Balance Sheet"]
page = st.radio("Section", _PAGES, horizontal=True, key="top_nav",
                label_visibility="collapsed")
st.divider()

if page == "Overview":
    coming_soon(
        "Overview Dashboard",
        "Consolidated KPIs from every module — revenue, margin, debtors, stock value, P&L summary",
        "📊",
    )

elif page == "Purchase":
    sub = st.radio("View", ["📦 Purchase Overview", "🏭 Inventory"],
                   horizontal=True, key="pur_nav", label_visibility="collapsed")
    if sub.endswith("Purchase Overview"):
        from src.purchase import render as render_purchase
        render_purchase()
    else:
        from src.inventory import render as render_inventory
        render_inventory()

elif page == "Sales":
    _SALES = ["Sales Plan", "Sales Overview", "Segment Analysis",
              "Team Performance", "Distribution", "Meeting Pack",
              "Operations Rhythm"]
    sub = st.radio("View", _SALES, horizontal=True, key="sales_nav",
                   label_visibility="collapsed")
    if sub == "Sales Plan":
        from src.sales_plan import render as render_sales_plan
        render_sales_plan()
    elif sub == "Sales Overview":
        from src.sales import render as render_sales
        render_sales()
    elif sub == "Segment Analysis":
        from src.segments import render as render_segments
        render_segments()
    elif sub == "Team Performance":
        from src.salesman import render as render_salesman
        render_salesman()
    elif sub == "Distribution":
        from src.distribution import render as render_distribution
        render_distribution()
    elif sub == "Meeting Pack":
        from src.principal import render as render_principal
        render_principal()
    else:
        from src.operations import render as render_operations
        render_operations()

elif page == "Discounts":
    from src.discounts import render as render_discounts
    render_discounts()

elif page == "Expenses":
    from src.expenses import render as render_expenses
    render_expenses()

elif page == "Debtors Ageing":
    from src.debtors import render as render_debtors
    render_debtors()

elif page == "Cash Flow":
    from src.cashflow import render as render_cashflow
    render_cashflow()

elif page == "Balance Sheet":
    from src.balance_sheet import render as render_balance_sheet
    render_balance_sheet()
