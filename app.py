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

# ── Tabs ─────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "📈  Overview",
    "👥  Team Performance",
    "📦  Distribution",
    "📊  Meeting Pack",
])

with tab1:
    from src.sales import render as render_sales
    render_sales()

with tab2:
    from src.salesman import render as render_salesman
    render_salesman()

with tab3:
    from src.distribution import render as render_distribution
    render_distribution()

with tab4:
    from src.principal import render as render_principal
    render_principal()
