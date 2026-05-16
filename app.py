import streamlit as st

st.set_page_config(
    page_title="LiquorBiz Dashboard",
    page_icon="🥃",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    # Logo placeholder
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
        options=["Sales & Revenue"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("v1.0.0 · LiquorBiz")

# ── Page routing ───────────────────────────────────────────────────────────
if page == "Sales & Revenue":
    from pages.sales import render
    render()
