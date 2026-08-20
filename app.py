import hmac
import traceback
from datetime import datetime, timezone, timedelta
import streamlit as st
from db import get_connection_status

# IST = UTC+5:30 — hard-coded because Streamlit Cloud runs in UTC and
# datetime.now() there prints UTC hours by default. Kept as a constant
# so timezone handling is obvious to anyone reading the header code.
_IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(
    page_title="KWPL Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Auth gate ───────────────────────────────────────────────────────────────
# Anyone with the public Streamlit Cloud URL would otherwise reach the
# dashboard. This is a simple username + password gate using credentials
# stored in st.secrets["auth"]. Credentials live in
# .streamlit/secrets.toml locally (gitignored) and in the Streamlit Cloud
# Secrets manager in production. Comparisons use hmac.compare_digest to
# prevent timing-attack-style probes.
def _require_login() -> bool:
    if st.session_state.get("auth_ok"):
        return True

    def _attempt():
        u_in = st.session_state.get("auth_user", "")
        p_in = st.session_state.get("auth_pass", "")
        try:
            u_ok = hmac.compare_digest(u_in, st.secrets["auth"]["username"])
            p_ok = hmac.compare_digest(p_in, st.secrets["auth"]["password"])
        except (KeyError, FileNotFoundError):
            st.session_state["auth_error"] = (
                "Auth not configured. Add an [auth] section with "
                "username and password to Streamlit secrets.")
            return
        if u_ok and p_ok:
            st.session_state["auth_ok"] = True
            st.session_state.pop("auth_pass", None)
            st.session_state.pop("auth_error", None)
        else:
            st.session_state["auth_error"] = "Invalid username or password."

    # Centered login card
    st.markdown("""
    <style>
      .block-container { padding-top: 4rem !important; max-width: 420px; }
    </style>
    """, unsafe_allow_html=True)
    st.markdown(
        "<div style='text-align:center; margin-bottom:1.5rem'>"
        "<div style='font-size:1.8rem; font-weight:800; color:#1B4F72; "
        "letter-spacing:0.06em'>KWPL</div>"
        "<div style='font-size:0.85rem; color:#888'>Kranti Wines Pvt. Ltd.</div>"
        "</div>",
        unsafe_allow_html=True)
    with st.form("login_form", clear_on_submit=False):
        st.text_input("Username", key="auth_user", autocomplete="username")
        st.text_input("Password", key="auth_pass", type="password",
                      autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        _attempt()
        if st.session_state.get("auth_ok"):
            st.rerun()
    if st.session_state.get("auth_error"):
        st.error(st.session_state["auth_error"])
    return False


if not _require_login():
    st.stop()

# ── Global CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Hide sidebar toggle & default Streamlit chrome.
   IMPORTANT: target Streamlit's OWN header via data-testid, not the
   bare `header` tag selector. A bare `header { visibility: hidden }`
   hides EVERY <header> element on the page — including the month/year
   navigation bar inside the BaseWeb date-picker calendar popup, which
   made every st.date_input calendar show a numbers-only grid with no
   visible month/year (owner-reported). Scoping to stHeader fixes the
   calendar without bringing back Streamlit's own toolbar. */
[data-testid="collapsedControl"] { display: none !important; }
#MainMenu { visibility: hidden; }
[data-testid="stHeader"] { visibility: hidden; }
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
# Track when the cache was last fully cleared. Streamlit's @st.cache_data
# has per-function TTLs so a global "last fresh" doesn't exist natively —
# we stamp it ourselves whenever the user clicks 🔄 Refresh (or when the
# app cold-starts). Shown as a caption in the header so the owner always
# knows how fresh the numbers are.
if "last_refresh_at" not in st.session_state:
    st.session_state["last_refresh_at"] = datetime.now(_IST)

now_ist   = datetime.now(_IST)
refresh   = st.session_state["last_refresh_at"]
age_min   = int((now_ist - refresh).total_seconds() // 60)
if age_min < 1:
    age_txt = "just now"
elif age_min < 60:
    age_txt = f"{age_min}m ago"
else:
    age_txt = f"{age_min // 60}h {age_min % 60}m ago"

status = get_connection_status()
status_html = (
    '<span class="kwpl-status">● DB Connected</span>'
    if status else
    '<span class="kwpl-status-off">● DB Offline</span>'
)
freshness_html = (
    f'<span style="color:rgba(255,255,255,0.75); font-size:0.72rem; '
    f'margin-right:12px">'
    f'Now {now_ist.strftime("%H:%M")} IST · Last refresh {refresh.strftime("%H:%M")} '
    f'({age_txt})</span>'
)
_hdr, _btn_refresh, _btn_logout = st.columns([11, 1, 1])
with _hdr:
    st.markdown(f"""
    <div class="kwpl-header">
        <div>
            <div class="kwpl-logo">KWPL</div>
            <div class="kwpl-sub">Kranti Wines Pvt. Ltd.</div>
        </div>
        <div style="display:flex; align-items:center">
            {freshness_html}
            {status_html}
        </div>
    </div>
    """, unsafe_allow_html=True)
with _btn_refresh:
    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
    if st.button("🔄 Refresh", help="Clear cache & re-query DB",
                 key="hdr_refresh", use_container_width=True):
        st.cache_data.clear()
        # Also re-probe the connection in case it was stale
        from db import get_connection
        get_connection.clear()
        st.session_state["last_refresh_at"] = datetime.now(_IST)
        st.rerun()
with _btn_logout:
    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
    if st.button("🚪 Logout", help="Sign out of the dashboard",
                 key="hdr_logout", use_container_width=True):
        st.session_state.pop("auth_ok", None)
        st.session_state.pop("auth_user", None)
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


# ── Pre-import every page renderer ONCE at app startup ──────────────────────
# Lazy imports inside elif branches (the previous pattern) triggered
# `KeyError: 'src.<module>'` on Streamlit Cloud whenever the app was
# re-deployed mid-session: Python 3.14's import machinery sees a partial
# sys.modules cache after the hot-reload and the lazy import fails.
# Top-level imports run once at cold start, so the cache is always
# consistent. Each module is small at import time (heavy work is gated by
# @st.cache_data on call), so memory cost is negligible.
from src.overview     import render as render_overview
from src.bans         import render as render_bans
from src.purchase     import render as render_purchase
from src.inventory    import render as render_inventory
from src.sales_plan   import render as render_sales_plan
from src.sales        import render as render_sales
from src.segments     import render as render_segments
from src.salesman     import render as render_salesman
from src.distribution import render as render_distribution
from src.principal    import render as render_principal
from src.operations   import render as render_operations
from src.reactivation import render as render_reactivation
from src.discounts    import render as render_discounts
from src.expenses     import render as render_expenses
from src.debtors      import render as render_debtors
from src.cashflow     import render as render_cashflow
from src.balance_sheet import render as render_balance_sheet
from src.nrm          import render as render_nrm

def safe_render(fn, page_label: str) -> None:
    """Run a page renderer with an error boundary.

    Without this, a single uncaught exception (a DB drop mid-query, a
    KeyError on a missing column, anything) takes the WHOLE app down —
    the user sees a generic Streamlit crash page and has to hard-refresh.
    With this, the broken tab shows a friendly error + the trace in an
    expander, and the radio nav still works so the user can flip to a
    working page.
    """
    try:
        fn()
    except Exception as exc:
        st.error(
            f"**{page_label}** crashed while rendering.\n\n"
            f"`{type(exc).__name__}: {exc}`\n\n"
            f"Other tabs still work — switch with the section radio "
            f"above. Click **🔄 Refresh** in the header to retry; "
            f"the issue is usually a transient DB drop and clears on "
            f"the next attempt."
        )
        with st.expander("Show technical details (for debugging)"):
            st.code(traceback.format_exc(), language="python")


_PAGES = ["Overview", "Purchase", "Sales", "NRM", "Discounts",
          "Expenses", "Debtors Ageing", "Bans", "Cash Flow", "Balance Sheet"]
page = st.radio("Section", _PAGES, horizontal=True, key="top_nav",
                label_visibility="collapsed")
st.divider()

if page == "Overview":
    safe_render(render_overview, "Overview")

elif page == "Purchase":
    sub = st.radio("View", ["📦 Purchase Overview", "🏭 Inventory"],
                   horizontal=True, key="pur_nav", label_visibility="collapsed")
    if sub.endswith("Purchase Overview"):
        safe_render(render_purchase, "Purchase Overview")
    else:
        safe_render(render_inventory, "Inventory")

elif page == "Sales":
    _SALES = ["Sales Plan", "Sales Overview", "Segment Analysis",
              "Team Performance", "Distribution", "Meeting Pack",
              "Operations Rhythm", "Reactivation"]
    sub = st.radio("View", _SALES, horizontal=True, key="sales_nav",
                   label_visibility="collapsed")
    if sub == "Sales Plan":
        safe_render(render_sales_plan, "Sales Plan")
    elif sub == "Sales Overview":
        safe_render(render_sales, "Sales Overview")
    elif sub == "Segment Analysis":
        safe_render(render_segments, "Segment Analysis")
    elif sub == "Team Performance":
        safe_render(render_salesman, "Team Performance")
    elif sub == "Distribution":
        safe_render(render_distribution, "Distribution")
    elif sub == "Meeting Pack":
        safe_render(render_principal, "Meeting Pack")
    elif sub == "Operations Rhythm":
        safe_render(render_operations, "Operations Rhythm")
    else:
        safe_render(render_reactivation, "Reactivation")

elif page == "NRM":
    safe_render(render_nrm, "NRM Planner")

elif page == "Discounts":
    safe_render(render_discounts, "Discounts")

elif page == "Expenses":
    safe_render(render_expenses, "Expenses")

elif page == "Debtors Ageing":
    safe_render(render_debtors, "Debtors Ageing")

elif page == "Bans":
    safe_render(render_bans, "Trader Association Bans")

elif page == "Cash Flow":
    safe_render(render_cashflow, "Cash Flow")

elif page == "Balance Sheet":
    safe_render(render_balance_sheet, "Balance Sheet")
