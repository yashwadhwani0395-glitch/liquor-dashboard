import streamlit as st

st.set_page_config(
    page_title="LiquorBiz Dashboard",
    page_icon="🥃",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── DB status check (cached 60 s) ──────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _db_status() -> tuple[bool, str]:
    """Lightweight connectivity check. Returns (ok, error_message)."""
    try:
        import pymssql
        from db import _HOST, _PORT, _USER, _PASS, _DBNAME
        conn = pymssql.connect(
            server=_HOST, user=_USER, password=_PASS,
            database=_DBNAME, port=str(_PORT),
            timeout=5, login_timeout=5,
        )
        conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    # Logo
    st.markdown(
        """
        <div style='text-align:center; padding: 1rem 0 1.5rem 0;'>
            <span style='font-size:2.5rem;'>🥃</span><br>
            <span style='font-size:1.3rem; font-weight:700; color:#E8A838;'>
                LiquorBiz
            </span><br>
            <span style='font-size:0.75rem; color:#888;'>Distribution Dashboard</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()

    st.markdown("#### Navigation")
    page = st.radio(
        label="page",
        options=["Sales & Revenue", "Salesman & Channels", "Distribution"],
        label_visibility="collapsed",
    )

    st.divider()

    # DB status indicator
    ok, err = _db_status()
    if ok:
        st.markdown(
            "<span style='color:#2ecc71; font-size:0.85rem;'>&#9679; DB Connected</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<span style='color:#e74c3c; font-size:0.85rem;'>&#9679; DB Offline</span>",
            unsafe_allow_html=True,
        )
        if err:
            st.caption(f"_{err}_")

    st.divider()
    st.caption("v1.0.0 · LiquorBiz")


# ── Page routing ───────────────────────────────────────────────────────────
if page == "Sales & Revenue":
    from pages.sales import render
    render()
elif page == "Salesman & Channels":
    from pages.salesman import render
    render()
elif page == "Distribution":
    from pages.distribution import render
    render()
