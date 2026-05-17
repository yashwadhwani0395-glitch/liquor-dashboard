import streamlit as st

st.set_page_config(
    page_title="KWPL Dashboard",
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
    # Branding header
    st.markdown(
        """
        <div style='text-align:center; padding: 1.2rem 0 1.4rem 0;'>
            <span style='font-size:2.2rem; font-weight:800; color:#1B4F72;
                         letter-spacing:0.05em;'>KWPL</span><br>
            <span style='font-size:0.8rem; color:#555; font-weight:500;'>
                Kranti Wines Pvt. Ltd.
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()

    # ── Navigation via session_state ────────────────────────────────────────
    if "page" not in st.session_state:
        st.session_state["page"] = "Sales & Revenue"

    def _nav(label: str):
        st.session_state["page"] = label

    with st.expander("📈  Sales", expanded=st.session_state["page"] in ("Sales & Revenue",)):
        if st.button("Sales & Revenue", use_container_width=True,
                     type="primary" if st.session_state["page"] == "Sales & Revenue" else "secondary"):
            _nav("Sales & Revenue")

    with st.expander("👥  Team Performance",
                     expanded=st.session_state["page"] in ("Salesman & Channels", "Distribution")):
        if st.button("Salesman & Channels", use_container_width=True,
                     type="primary" if st.session_state["page"] == "Salesman & Channels" else "secondary"):
            _nav("Salesman & Channels")
        if st.button("Distribution Analytics", use_container_width=True,
                     type="primary" if st.session_state["page"] == "Distribution" else "secondary"):
            _nav("Distribution")

    st.divider()

    # DB status
    ok, err = _db_status()
    if ok:
        st.markdown(
            "<span style='color:#27ae60; font-size:0.82rem;'>&#9679;&nbsp;DB Connected</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<span style='color:#e74c3c; font-size:0.82rem;'>&#9679;&nbsp;DB Offline</span>",
            unsafe_allow_html=True,
        )
        if err:
            st.caption(f"_{err}_")

    st.markdown(
        "<div style='font-size:0.72rem; color:#888; margin-top:4px;'>"
        "Data refreshes every 5 min</div>",
        unsafe_allow_html=True,
    )
    st.caption("v1.0 · KWPL")


# ── Page routing ───────────────────────────────────────────────────────────
_page = st.session_state.get("page", "Sales & Revenue")

if _page == "Sales & Revenue":
    from pages.sales import render
    render()
elif _page == "Salesman & Channels":
    from pages.salesman import render
    render()
elif _page == "Distribution":
    from pages.distribution import render
    render()
