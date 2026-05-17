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
st.markdown(f"""
<div class="kwpl-header">
    <div>
        <div class="kwpl-logo">KWPL</div>
        <div class="kwpl-sub">Kranti Wines Pvt. Ltd.</div>
    </div>
    {status_html}
</div>
""", unsafe_allow_html=True)

# ── Top-level tabs ───────────────────────────────────────────────────────────
t1, t2, t3, t4, t5, t6, t7 = st.tabs([
    "Overview", "Purchase", "Sales", "Inventory",
    "Expenses", "Cash Flow", "Balance Sheet",
])


def coming_soon(title: str, desc: str, icon: str = "🚧") -> None:
    st.markdown(f"""
    <div style='padding: 80px 0; text-align: center'>
        <div style='font-size: 56px; margin-bottom: 16px; opacity: 0.25'>{icon}</div>
        <div style='font-size: 20px; font-weight: 600; color: #1a1a1a; margin-bottom: 8px'>{title}</div>
        <div style='font-size: 13px; color: #888; max-width: 500px; margin: 0 auto'>{desc}</div>
    </div>
    """, unsafe_allow_html=True)


with t1:
    coming_soon("Overview Dashboard",
                "Consolidated KPIs from every module")

with t2:
    coming_soon("Purchase Analytics",
                "Principal-wise purchases and claim tracking")

with t3:
    s1, s2, s3, s4, s5 = st.tabs([
        "Sales Overview", "Team Performance",
        "Distribution", "Meeting Pack", "Operations Rhythm",
    ])
    with s1:
        from src.sales import render as render_sales
        render_sales()
    with s2:
        from src.salesman import render as render_salesman
        render_salesman()
    with s3:
        from src.distribution import render as render_distribution
        render_distribution()
    with s4:
        from src.principal import render as render_principal
        render_principal()
    with s5:
        from src.operations import render as render_operations
        render_operations()

with t4:
    coming_soon("Inventory Module", "Stock levels and movement")

with t5:
    coming_soon("Expense Tracking", "Operating expenses by category")

with t6:
    coming_soon("Cash Flow & Outstanding", "Debtor and creditor ageing")

with t7:
    coming_soon("Balance Sheet", "Key ratios coming soon")
