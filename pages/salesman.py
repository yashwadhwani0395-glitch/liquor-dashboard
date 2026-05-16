"""pages/salesman.py — Salesman & Channel performance page.

Salesman attribution is derived entirely from SQL CASE logic using:
  - MsBrandMaster.CompanyID  (principal / supplier)
  - MsPartyMaster.ClassID    (MOP=060021, POP=060004, Beer Shopee=060008, …)
  - MsPartyMaster.AcType3ID  (KW Institution=130004, PCMC Institution=130002,
                               KW Insti One Day=130006)

No separate salesman master join is needed — teams are inferred from the
combination of what was sold (brand → principal) and to whom (party class).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, section_header

# ── Sales transaction type IDs (same as sales.py) ─────────────────────────
SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Known principal company IDs ────────────────────────────────────────────
PRINCIPAL_IDS = {
    "United Spirits":   "C00025",
    "Diageo":           "C00040",
    "Brown-Forman":     "C00056",
    "United Breweries": "C00039",
}

# ── Principal → bar / line color ───────────────────────────────────────────
PRINCIPAL_COLORS = {
    "United Spirits":   "#1f77b4",   # blue
    "Diageo":           "#9467bd",   # purple
    "Brown-Forman":     "#FF7043",   # coral / orange
    "United Breweries": "#26C6DA",   # teal
}

# ── Preferred column order for heatmap ─────────────────────────────────────
CHANNEL_ORDER = [
    "MOP - Wine Shops",
    "POP - Permit Rooms",
    "Beer Shopee",
    "KW Institution",
    "PCMC Institution",
    "KW Insti One Day",
    "Retail",
    "FL-IV Club",
    "Other",
]

# ── Short display labels for salesman bar chart Y-axis ─────────────────────
_SHORT_LABELS: dict[str, str] = {
    "Shashank / Sachin (USL - Wine Shops)":
        "Shashank/Sachin — USL Wine Shops",
    "Ajay / Deepak (Diageo - Wine Shops)":
        "Ajay/Deepak — Diageo Wine Shops",
    "Ajay / Deepak (BF - Wine Shops)":
        "Ajay/Deepak — BF Wine Shops",
    "Aabid / Omkar (BF - Permit Rooms)":
        "Aabid/Omkar — BF Permit Rooms",
    "Miran / Rohit / Tulsi / Atish / Saurabh / Prashant (USL+Diageo - POP)":
        "Miran+Team — USL+Diageo POP",
    "Aabid / Omkar (UBL - Wine Shops)":
        "Aabid/Omkar — UBL Wine Shops",
    "Aabid / Omkar (UBL - Beer Shopee)":
        "Aabid/Omkar — UBL Beer Shopee",
    "Aabid / Omkar (UBL - Permit Rooms)":
        "Aabid/Omkar — UBL Permit Rooms",
    "Shashank Desai / Pranav / Deepak Pangare / Anand Raj (KW Institution)":
        "SD/Pranav/DP/AR — KW Insti",
    "Shashank Desai / Pranav / Deepak Pangare / Anand Raj (KW Insti One Day)":
        "SD/Pranav/DP/AR — KW 1Day",
    "Amol Sathe / Gajendra Das / Rahul Ghone (PCMC Institution)":
        "Amol/Gajendra/Rahul — PCMC",
    "Other / Unassigned": "Other / Unassigned",
}

# ── Chart theme (matches sales.py) ─────────────────────────────────────────
_BG     = "rgba(0,0,0,0)"
_GOLD   = "#E8A838"
_GRID   = dict(gridcolor="#2a2d3e")
_LAYOUT = dict(
    paper_bgcolor=_BG,
    plot_bgcolor=_BG,
    font=dict(color="#FAFAFA"),
    margin=dict(t=30, b=20),
)
_PAL = px.colors.qualitative.Bold


# ── Helpers ────────────────────────────────────────────────────────────────

def _inr(value: float) -> str:
    """Exact Indian rupee formatter for table cells."""
    neg = value < 0
    s = str(int(round(abs(value))))
    if len(s) > 3:
        tail, head = s[-3:], s[:-3]
        parts: list[str] = []
        while head:
            parts.append(head[-2:])
            head = head[:-2]
        s = ",".join(reversed(parts)) + "," + tail
    return ("−" if neg else "") + "₹" + s


# ── FY helper (mirrors sales.py) ───────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _load_fy_years(start: date, end: date) -> tuple[str, ...]:
    """Query the DB for FinancialYear tags that exist in the date range."""
    type_ph = ",".join("?" * len(SALES_TYPES))
    df = run_query(
        f"""
        SELECT DISTINCT vi.FinancialYear
        FROM TrVocItem vi
        JOIN TrVocHead h
            ON  h.TransTypeID = vi.TransTypeID
            AND h.VoucherNo   = vi.VoucherNo
        WHERE h.VoucherDate BETWEEN ? AND ?
          AND h.TransTypeID IN ({type_ph})
          AND h.Cancelled = 'N'
        """,
        (str(start), str(end)) + SALES_TYPES,
    )
    if df.empty:
        return ()

    def _fy_start_year(d: date) -> int:
        return d.year if d.month >= 4 else d.year - 1

    valid_starts = {_fy_start_year(start), _fy_start_year(end)}
    result = tuple(
        fy for fy in df["FinancialYear"].tolist()
        if fy and int(fy[:4]) in valid_starts
    )
    return result if result else tuple(df["FinancialYear"].dropna().tolist())


# ── Main data loader ────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _load_data(start: date, end: date) -> pd.DataFrame:
    """Load all sales rows with SalesmanTeam, Channel, and Principal labels."""
    fy_years = _load_fy_years(start, end)
    type_ph  = ",".join("?" * len(SALES_TYPES))

    fy_sql    = ""
    fy_params: tuple = ()
    if fy_years:
        fy_ph     = ",".join("?" * len(fy_years))
        fy_sql    = f"AND vi.FinancialYear IN ({fy_ph})"
        fy_params = tuple(fy_years)

    sql = f"""
        SELECT
            CAST(h.VoucherDate AS date)                  AS VoucherDate,
            h.VoucherNo,
            ISNULL(p.PartyName,  'Unknown')              AS PartyName,
            ISNULL(p.ClassID,    '')                     AS ClassID,
            ISNULL(p.AcType3ID,  '')                     AS AcType3ID,
            ISNULL(b.BrandName,  'Unknown')              AS BrandName,
            ISNULL(b.CompanyID,  '')                     AS CompanyID,
            CAST(vi.CaseQty     AS BIGINT)               AS CaseQty,
            CAST(vi.TotalAmount AS FLOAT)                AS TotalAmount,

            /* ── Salesman Team attribution ── */
            CASE
              WHEN b.CompanyID = 'C00025' AND p.ClassID = '060021'
                THEN 'Shashank / Sachin (USL - Wine Shops)'
              WHEN b.CompanyID = 'C00040' AND p.ClassID = '060021'
                THEN 'Ajay / Deepak (Diageo - Wine Shops)'
              WHEN b.CompanyID = 'C00056' AND p.ClassID = '060021'
                THEN 'Ajay / Deepak (BF - Wine Shops)'
              WHEN b.CompanyID = 'C00056' AND p.ClassID = '060004'
                THEN 'Aabid / Omkar (BF - Permit Rooms)'
              WHEN b.CompanyID IN ('C00025','C00040')
                AND p.ClassID = '060004'
                AND (p.AcType3ID IS NULL
                     OR p.AcType3ID NOT IN ('130004','130002','130006'))
                THEN 'Miran / Rohit / Tulsi / Atish / Saurabh / Prashant (USL+Diageo - POP)'
              WHEN b.CompanyID = 'C00039' AND p.ClassID = '060021'
                THEN 'Aabid / Omkar (UBL - Wine Shops)'
              WHEN b.CompanyID = 'C00039' AND p.ClassID = '060008'
                THEN 'Aabid / Omkar (UBL - Beer Shopee)'
              WHEN b.CompanyID = 'C00039' AND p.ClassID = '060004'
                AND (p.AcType3ID IS NULL
                     OR p.AcType3ID NOT IN ('130004','130002','130006'))
                THEN 'Aabid / Omkar (UBL - Permit Rooms)'
              WHEN p.AcType3ID = '130004'
                THEN 'Shashank Desai / Pranav / Deepak Pangare / Anand Raj (KW Institution)'
              WHEN p.AcType3ID = '130006'
                THEN 'Shashank Desai / Pranav / Deepak Pangare / Anand Raj (KW Insti One Day)'
              WHEN p.AcType3ID = '130002'
                THEN 'Amol Sathe / Gajendra Das / Rahul Ghone (PCMC Institution)'
              ELSE 'Other / Unassigned'
            END AS SalesmanTeam,

            /* ── Channel label ── */
            CASE
              WHEN p.ClassID = '060021' THEN 'MOP - Wine Shops'
              WHEN p.ClassID = '060004' AND p.AcType3ID = '130004' THEN 'KW Institution'
              WHEN p.ClassID = '060004' AND p.AcType3ID = '130002' THEN 'PCMC Institution'
              WHEN p.ClassID = '060004' AND p.AcType3ID = '130006' THEN 'KW Insti One Day'
              WHEN p.ClassID = '060004' THEN 'POP - Permit Rooms'
              WHEN p.ClassID = '060008' THEN 'Beer Shopee'
              WHEN p.ClassID = '060020' THEN 'Retail'
              WHEN p.ClassID = '060005' THEN 'FL-IV Club'
              ELSE 'Other'
            END AS Channel,

            /* ── Principal label ── */
            CASE b.CompanyID
              WHEN 'C00025' THEN 'United Spirits'
              WHEN 'C00040' THEN 'Diageo'
              WHEN 'C00056' THEN 'Brown-Forman'
              WHEN 'C00039' THEN 'United Breweries'
              ELSE ISNULL(pp.PartyName, ISNULL(b.CompanyID, 'Unknown'))
            END AS Principal

        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID LIKE 'I%'          -- exclude service/charge rows
        LEFT JOIN (
            -- One party per voucher: pick the largest debit (= the customer)
            SELECT TransTypeID, VoucherNo, PartyID
            FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (
                           PARTITION BY TransTypeID, VoucherNo
                           ORDER BY Amount DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator = 'D'
            ) x
            WHERE rn = 1
        ) d  ON  d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        LEFT JOIN MsPartyMaster p  ON p.PartyID  = d.PartyID
        LEFT JOIN MsItemMaster  im ON im.ItemID  = vi.ItemID
        LEFT JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
        LEFT JOIN MsPartyMaster pp ON pp.PartyID = b.CompanyID   -- principal name
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND vi.FreeItemYN = 'N'
          {fy_sql}
          AND CAST(h.VoucherDate AS date)
              BETWEEN CAST(? AS date) AND CAST(? AS date)
    """
    # params order: SALES_TYPES → fy_params → start, end
    params = SALES_TYPES + fy_params + (str(start), str(end))
    df = run_query(sql, params)
    if df.empty:
        return df
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["TotalAmount"] = pd.to_numeric(df["TotalAmount"], errors="coerce").fillna(0.0)
    df["CaseQty"]     = pd.to_numeric(df["CaseQty"],     errors="coerce").fillna(0).astype(int)
    return df


# ── Chart builders ──────────────────────────────────────────────────────────

def _chart_salesman_bar(df: pd.DataFrame) -> go.Figure:
    """Horizontal bar — revenue by salesman team, sorted desc, short labels."""
    agg = (
        df.groupby("SalesmanTeam")["TotalAmount"].sum()
        .sort_values(ascending=True)          # ascending so largest is at top
        .reset_index()
    )
    agg["ShortLabel"] = agg["SalesmanTeam"].map(
        lambda x: _SHORT_LABELS.get(x, x)
    )
    fig = px.bar(
        agg,
        x="TotalAmount", y="ShortLabel",
        orientation="h",
        color="TotalAmount",
        color_continuous_scale=["#1A3A5C", _GOLD],
        text_auto=".2s",
        labels={"TotalAmount": "Revenue (₹)", "ShortLabel": ""},
    )
    fig.update_layout(
        **_LAYOUT,
        coloraxis_showscale=False,
        height=max(320, len(agg) * 48),
        xaxis=_GRID,
        yaxis=dict(**_GRID),
    )
    fig.update_traces(textposition="outside")
    return fig


def _chart_channel_donut(df: pd.DataFrame) -> go.Figure:
    """Donut chart — revenue share by channel."""
    agg = (
        df.groupby("Channel")["TotalAmount"].sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    total = agg["TotalAmount"].sum()
    agg["label"] = agg.apply(
        lambda r: f"{r['Channel']}<br>₹{r['TotalAmount']/1e5:.1f}L", axis=1
    )
    fig = px.pie(
        agg, names="Channel", values="TotalAmount",
        hole=0.44,
        color_discrete_sequence=_PAL,
    )
    fig.update_layout(
        **_LAYOUT,
        annotations=[dict(
            text=f"<b>{format_inr(total)}</b>",
            x=0.5, y=0.5, font_size=14,
            showarrow=False, font_color="#FAFAFA",
        )],
        legend=dict(font=dict(size=11)),
    )
    fig.update_traces(
        textinfo="label+percent",
        pull=[0.05] + [0] * (len(agg) - 1),
    )
    return fig


def _chart_principal_bar(df: pd.DataFrame) -> go.Figure:
    """Grouped bar — revenue by principal with brand colors."""
    agg = (
        df.groupby("Principal")["TotalAmount"].sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    colors = [PRINCIPAL_COLORS.get(p, "#888888") for p in agg["Principal"]]
    fig = go.Figure(go.Bar(
        x=agg["Principal"],
        y=agg["TotalAmount"],
        marker_color=colors,
        text=[f"₹{v/1e7:.2f}Cr" for v in agg["TotalAmount"]],
        textposition="outside",
    ))
    fig.update_layout(
        **_LAYOUT,
        showlegend=False,
        xaxis=dict(tickangle=-20, **_GRID),
        yaxis=dict(title="Revenue (₹)", **_GRID),
    )
    return fig


def _chart_heatmap(df: pd.DataFrame) -> go.Figure:
    """Principal × Channel revenue heatmap."""
    pivot = (
        df.groupby(["Principal", "Channel"])["TotalAmount"]
        .sum()
        .unstack(fill_value=0)
    )
    # Order columns
    ordered_cols = [c for c in CHANNEL_ORDER if c in pivot.columns]
    extra_cols   = [c for c in pivot.columns if c not in CHANNEL_ORDER]
    pivot = pivot[ordered_cols + extra_cols]

    # Order rows: known principals first
    known_rows  = [p for p in PRINCIPAL_COLORS if p in pivot.index]
    other_rows  = sorted(p for p in pivot.index if p not in known_rows)
    pivot = pivot.loc[known_rows + other_rows]

    z    = pivot.values.tolist()
    text = [
        [f"₹{v/1e5:.1f}L" if v > 1_000 else ("" if v == 0 else f"₹{v:,.0f}")
         for v in row]
        for row in z
    ]

    fig = go.Figure(go.Heatmap(
        z=z,
        x=pivot.columns.tolist(),
        y=pivot.index.tolist(),
        text=text,
        texttemplate="%{text}",
        colorscale="Blues",
        showscale=True,
        hoverongaps=False,
        colorbar=dict(tickfont=dict(color="#FAFAFA")),
    ))
    fig.update_layout(
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color="#FAFAFA"),
        height=max(260, len(pivot) * 65 + 80),
        margin=dict(t=40, b=90, l=160, r=20),
        xaxis=dict(side="bottom", tickangle=-30, **_GRID),
        yaxis=dict(**_GRID),
    )
    return fig


def _chart_institution_bar(df: pd.DataFrame) -> go.Figure:
    """Side-by-side bar — KW vs PCMC Institution revenue by principal."""
    inst_channels = ["KW Institution", "PCMC Institution", "KW Insti One Day"]
    agg = (
        df[df["Channel"].isin(inst_channels)]
        .groupby(["Channel", "Principal"])["TotalAmount"]
        .sum()
        .reset_index()
    )
    fig = px.bar(
        agg, x="Channel", y="TotalAmount", color="Principal",
        barmode="group",
        color_discrete_map=PRINCIPAL_COLORS,
        text_auto=".2s",
        labels={"TotalAmount": "Revenue (₹)", "Channel": ""},
    )
    fig.update_layout(**_LAYOUT, xaxis=_GRID, yaxis=_GRID)
    fig.update_traces(textposition="outside")
    return fig


def _chart_daily_by_principal(df: pd.DataFrame) -> go.Figure:
    """Line chart — daily revenue trend with one line per principal."""
    agg = (
        df.groupby([df["VoucherDate"].dt.date, "Principal"])["TotalAmount"]
        .sum()
        .reset_index()
        .rename(columns={"VoucherDate": "Date"})
    )
    # Limit to top 6 principals by total to keep chart readable
    top_principals = (
        agg.groupby("Principal")["TotalAmount"].sum()
        .sort_values(ascending=False).head(6).index.tolist()
    )
    agg = agg[agg["Principal"].isin(top_principals)]

    color_map = {
        p: PRINCIPAL_COLORS.get(p, _PAL[i % len(_PAL)])
        for i, p in enumerate(top_principals)
    }
    fig = px.line(
        agg, x="Date", y="TotalAmount", color="Principal",
        markers=True,
        color_discrete_map=color_map,
        labels={"TotalAmount": "Revenue (₹)", "Date": ""},
    )
    fig.update_layout(**_LAYOUT, xaxis=_GRID, yaxis=_GRID)
    fig.update_traces(line_width=2.5, marker_size=4)
    return fig


# ── Page entry point ────────────────────────────────────────────────────────

def render():
    st.title("Salesman & Channels")

    # ── Sidebar filters ───────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("#### Filters")

        today    = date.today()
        fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

        c1, c2 = st.columns(2)
        with c1:
            start = st.date_input("From", value=fy_start, key="sm_from")
        with c2:
            end = st.date_input("To",   value=today,    key="sm_to")

        if start > end:
            st.warning("Start date must be before end date.")
            return

        sel_principals = st.multiselect(
            "Principal",
            options=[
                "United Spirits", "Diageo",
                "Brown-Forman", "United Breweries", "Others",
            ],
            key="sm_principals",
        )
        sel_channels = st.multiselect(
            "Channel",
            options=[
                "MOP - Wine Shops", "POP - Permit Rooms", "Beer Shopee",
                "KW Institution", "PCMC Institution", "KW Insti One Day",
                "Retail", "FL-IV Club", "Others",
            ],
            key="sm_channels",
        )

    # ── Fetch data ─────────────────────────────────────────────────────────
    with st.spinner("Fetching salesman & channel data…"):
        try:
            df = _load_data(start, end)
        except Exception as exc:
            st.error(f"Data load failed: {exc}")
            st.info(
                "Check `.env` / Streamlit secrets and confirm the `KWPL` "
                "login has SELECT access to `KW2526`."
            )
            return

    if df.empty:
        st.error(
            "No sales data returned for the selected date range. "
            "Check your database connection or adjust the filters."
        )
        st.info(
            "Ensure `.env` is configured and the `KWPL` login has access "
            "to `KW2526`. Run `python db_explorer.py` to verify connectivity."
        )
        return

    # ── Apply sidebar filters (post-load, in Python) ───────────────────────
    known_principals = list(PRINCIPAL_COLORS.keys())

    if sel_principals:
        named = [p for p in sel_principals if p != "Others"]
        if "Others" in sel_principals:
            df = df[
                df["Principal"].isin(named)
                | ~df["Principal"].isin(known_principals)
            ]
        else:
            df = df[df["Principal"].isin(named)]

    if sel_channels:
        named_ch = [c for c in sel_channels if c != "Others"]
        if "Others" in sel_channels:
            df = df[
                df["Channel"].isin(named_ch)
                | (df["Channel"] == "Other")
            ]
        else:
            df = df[df["Channel"].isin(named_ch)]

    if df.empty:
        st.warning("No data matches the selected filters. Try broadening the selection.")
        return

    # ── KPI calculations ───────────────────────────────────────────────────
    total_rev = df["TotalAmount"].sum()

    team_rev       = df.groupby("SalesmanTeam")["TotalAmount"].sum()
    top_team_full  = team_rev.idxmax()
    top_team_short = _SHORT_LABELS.get(top_team_full, top_team_full)
    top_team_rev   = team_rev.max()

    channel_rev  = df.groupby("Channel")["TotalAmount"].sum()
    top_channel  = channel_rev.idxmax()

    principal_rev = df.groupby("Principal")["TotalAmount"].sum()
    top_principal = principal_rev.idxmax()

    # ── KPI cards ─────────────────────────────────────────────────────────
    section_header(
        "Salesman & Channel Performance",
        f"{start.strftime('%d %b %Y')}  →  {end.strftime('%d %b %Y')}",
    )
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("Total Revenue", format_inr(total_rev))
    with k2:
        st.metric("Top Salesman Team", top_team_short,
                  delta=format_inr(top_team_rev), delta_color="off")
    with k3:
        st.metric("Top Channel", top_channel,
                  delta=format_inr(channel_rev[top_channel]), delta_color="off")
    with k4:
        st.metric("Top Principal", top_principal,
                  delta=format_inr(principal_rev[top_principal]), delta_color="off")

    st.divider()

    # ── Chart 1: Salesman Team Revenue ─────────────────────────────────────
    section_header("Revenue by Salesman Team")
    st.plotly_chart(_chart_salesman_bar(df), use_container_width=True)

    # ── Charts 2 & 3: Channel donut + Principal bar ────────────────────────
    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Revenue by Channel")
        st.plotly_chart(_chart_channel_donut(df), use_container_width=True)
    with col_b:
        st.subheader("Revenue by Principal")
        st.plotly_chart(_chart_principal_bar(df), use_container_width=True)

    # ── Chart 4: Principal × Channel heatmap ──────────────────────────────
    st.markdown("---")
    section_header("Principal × Channel Heatmap",
                   "Revenue in ₹ Lakhs per cell")
    st.plotly_chart(_chart_heatmap(df), use_container_width=True)

    # ── Chart 5: Institution breakdown (shown only when data exists) ───────
    inst_df = df[df["Channel"].isin(
        ["KW Institution", "PCMC Institution", "KW Insti One Day"]
    )]
    if not inst_df.empty:
        st.markdown("---")
        section_header(
            "Institution Breakdown",
            "KW & PCMC institutional revenue by principal",
        )
        st.plotly_chart(_chart_institution_bar(df), use_container_width=True)

    # ── Chart 6: Daily revenue trend by principal ──────────────────────────
    st.markdown("---")
    section_header("Daily Revenue Trend by Principal")
    st.plotly_chart(_chart_daily_by_principal(df), use_container_width=True)

    # ── Data table ─────────────────────────────────────────────────────────
    st.markdown("---")
    section_header(
        "Transaction Detail",
        "Latest 200 records · sorted by date desc",
    )
    disp = df.sort_values("VoucherDate", ascending=False).head(200).copy()
    disp["Date"]   = disp["VoucherDate"].dt.strftime("%d %b %Y")
    disp["Amount"] = disp["TotalAmount"].apply(_inr)
    disp["Cases"]  = disp["CaseQty"].astype(int)

    st.dataframe(
        disp[[
            "Date", "VoucherNo", "PartyName", "Channel",
            "Principal", "BrandName", "Cases", "Amount", "SalesmanTeam",
        ]].rename(columns={
            "VoucherNo":    "Invoice",
            "PartyName":    "Party",
            "BrandName":    "Brand",
            "SalesmanTeam": "Team",
        }),
        use_container_width=True,
        hide_index=True,
    )


render()
