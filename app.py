import streamlit as st
from db import get_connection_status

st.set_page_config(
    page_title="KWPL Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Sidebar ──────────────────────────────────────────
with st.sidebar:
    st.markdown("""
        <div style='text-align:center; padding: 1rem 0 0.5rem 0'>
            <h1 style='font-size:2rem; font-weight:700;
                       color:#1B4F72; margin:0'>KWPL</h1>
            <p style='color:#666; font-size:0.8rem;
                      margin:0'>Kranti Wines Pvt. Ltd.</p>
        </div>
    """, unsafe_allow_html=True)

    st.divider()

    # Navigation
    st.markdown("**📈 Sales**")
    with st.expander("Sales", expanded=True):
        if st.button("Sales & Revenue", use_container_width=True):
            st.session_state.page = "sales"

    st.markdown("**👥 Team Performance**")
    with st.expander("Team", expanded=True):
        if st.button("Salesman & Channels", use_container_width=True):
            st.session_state.page = "salesman"
        if st.button("Distribution Analytics", use_container_width=True):
            st.session_state.page = "distribution"

    st.divider()

    # DB Status
    status = get_connection_status()
    if status:
        st.success("● DB Connected")
    else:
        st.error("● DB Offline")

    st.caption("Data refreshes every 5 min")
    st.caption("v1.0 · KWPL")

# ── Routing ──────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state.page = "sales"

page = st.session_state.page

if page == "sales":
    import pages.sales as p; p.render()
elif page == "salesman":
    import pages.salesman as p; p.render()
elif page == "distribution":
    import pages.distribution as p; p.render()
