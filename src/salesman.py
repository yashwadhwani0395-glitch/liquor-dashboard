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
    "Retail",
    "POP - Permit Rooms",
    "Beer Shopee",
    "KW Beer",
    "Beer Cross Supply",
    "KW Institution",
    "PCMC Institution",
    "FL-IV Club",
    "Other",
]

# ── Short display labels for salesman bar chart Y-axis ─────────────────────
_SHORT_LABELS: dict[str, str] = {
    # USL teams
    "Shashank / Sachin (USL - Wine Shops FL-II)":
        "Shashank/Sachin — USL Wine Shops",
    "Tulsiram / Saurabh / Miran / Prashant / Atish (USL - Permit Rooms FL-III)":
        "Tulsiram+Team — USL Permit Rooms",
    "Shashank / Sachin (USL - Cross Supply Wine Shop)":
        "Shashank/Sachin — USL CS Wine Shop",
    "Tulsiram / Saurabh / Miran / Prashant / Atish (USL - Cross Supply Permit Room)":
        "Tulsiram+Team — USL CS Permit Room",
    "Shashank / Sachin (USL - Other)":
        "Shashank/Sachin — USL Other",
    # Diageo teams
    "Ajay / Deepak Patil (Diageo - Wine Shops FL-II)":
        "Ajay/DP — Diageo Wine Shops",
    "Tulsiram / Saurabh / Miran / Prashant / Atish (Diageo - Permit Rooms FL-III)":
        "Tulsiram+Team — Diageo Permit Rooms",
    "Ajay / Deepak Patil (Diageo - Cross Supply Wine Shop)":
        "Ajay/DP — Diageo CS Wine Shop",
    "Tulsiram / Saurabh / Miran / Prashant / Atish (Diageo - Cross Supply Permit Room)":
        "Tulsiram+Team — Diageo CS Permit Room",
    "Ajay / Deepak Patil (Diageo - Other)":
        "Ajay/DP — Diageo Other",
    # BF teams
    "Ajay / Deepak Patil / Aabid / Omkar (BF - Wine Shops FL-II)":
        "Ajay/DP/Aabid/Omkar — BF Wine Shops",
    "Anand Raj / Deepak Pangare / Shashank Desai / Pranav (BF - KW Institution)":
        "AR/DP/SD/Pranav — BF KW Insti",
    "Gajendra Das / Amol Sathe / Rahul Ghone (BF - PCMC Institution)":
        "GD/Amol/Rahul — BF PCMC",
    "Anand Raj / Deepak Pangare / Shashank Desai / Pranav (BF - FL-III KW Beer)":
        "AR/DP/SD/Pranav — BF FL-III",
    "Ajay / Deepak Patil / Aabid / Omkar (BF - Other)":
        "Ajay/DP/Aabid/Omkar — BF Other",
    # UBL teams
    "Anand Raj / Deepak Pangare / Shashank Desai / Pranav (UBL - KW Institution)":
        "AR/DP/SD/Pranav — UBL KW Insti",
    "Gajendra Das / Amol Sathe / Rahul Ghone (UBL - PCMC Institution)":
        "GD/Amol/Rahul — UBL PCMC",
    "Cross Supply - No Assigned Salesman (UBL)":
        "Cross Supply — UBL (no salesman)",
    "Aabid / Omkar (UBL - KW Beer)":
        "Aabid/Omkar — UBL KW Beer",
    "Other / Unassigned": "Other / Unassigned",
}

# ── Chart theme (matches sales.py) ─────────────────────────────────────────
_BG     = "rgba(0,0,0,0)"
_GOLD   = "#E8A838"
_GRID   = dict(gridcolor="#E8E8E8")
_LAYOUT = dict(
    paper_bgcolor=_BG,
    plot_bgcolor=_BG,
    font=dict(color="#1A1A1A"),
    template="plotly_white",
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
            /* Priority: principal ALWAYS wins for USL & Diageo.
               For UBL & BF, AcType3ID (institution) overrides channel. */
            CASE
              -- USL (C00025) — principal always wins, LicenseTypeID determines team
              WHEN b.CompanyID = 'C00025' AND p.LicenseTypeID = '180001'
                THEN 'Shashank / Sachin (USL - Wine Shops FL-II)'
              WHEN b.CompanyID = 'C00025' AND p.LicenseTypeID = '180002'
                THEN 'Tulsiram / Saurabh / Miran / Prashant / Atish (USL - Permit Rooms FL-III)'
              WHEN b.CompanyID = 'C00025' AND p.AcType3ID = '130007' AND p.LicenseTypeID = '180001'
                THEN 'Shashank / Sachin (USL - Cross Supply Wine Shop)'
              WHEN b.CompanyID = 'C00025' AND p.AcType3ID = '130007' AND p.LicenseTypeID = '180002'
                THEN 'Tulsiram / Saurabh / Miran / Prashant / Atish (USL - Cross Supply Permit Room)'
              WHEN b.CompanyID = 'C00025'
                THEN 'Shashank / Sachin (USL - Other)'

              -- DIAGEO (C00040) — principal always wins
              WHEN b.CompanyID = 'C00040' AND p.LicenseTypeID = '180001'
                THEN 'Ajay / Deepak Patil (Diageo - Wine Shops FL-II)'
              WHEN b.CompanyID = 'C00040' AND p.LicenseTypeID = '180002'
                THEN 'Tulsiram / Saurabh / Miran / Prashant / Atish (Diageo - Permit Rooms FL-III)'
              WHEN b.CompanyID = 'C00040' AND p.AcType3ID = '130007' AND p.LicenseTypeID = '180001'
                THEN 'Ajay / Deepak Patil (Diageo - Cross Supply Wine Shop)'
              WHEN b.CompanyID = 'C00040' AND p.AcType3ID = '130007' AND p.LicenseTypeID = '180002'
                THEN 'Tulsiram / Saurabh / Miran / Prashant / Atish (Diageo - Cross Supply Permit Room)'
              WHEN b.CompanyID = 'C00040'
                THEN 'Ajay / Deepak Patil (Diageo - Other)'

              -- BROWN-FORMAN (C00056) — AcType3ID overrides for institution
              WHEN b.CompanyID = 'C00056' AND p.LicenseTypeID = '180001'
                THEN 'Ajay / Deepak Patil / Aabid / Omkar (BF - Wine Shops FL-II)'
              WHEN b.CompanyID = 'C00056' AND p.AcType3ID = '130004'
                THEN 'Anand Raj / Deepak Pangare / Shashank Desai / Pranav (BF - KW Institution)'
              WHEN b.CompanyID = 'C00056' AND p.AcType3ID = '130002'
                THEN 'Gajendra Das / Amol Sathe / Rahul Ghone (BF - PCMC Institution)'
              WHEN b.CompanyID = 'C00056' AND p.LicenseTypeID = '180002'
                THEN 'Anand Raj / Deepak Pangare / Shashank Desai / Pranav (BF - FL-III KW Beer)'
              WHEN b.CompanyID = 'C00056'
                THEN 'Ajay / Deepak Patil / Aabid / Omkar (BF - Other)'

              -- UNITED BREWERIES (C00039) — AcType3ID determines team
              WHEN b.CompanyID = 'C00039' AND p.AcType3ID IN ('130004','130006')
                THEN 'Anand Raj / Deepak Pangare / Shashank Desai / Pranav (UBL - KW Institution)'
              WHEN b.CompanyID = 'C00039' AND p.AcType3ID = '130002'
                THEN 'Gajendra Das / Amol Sathe / Rahul Ghone (UBL - PCMC Institution)'
              WHEN b.CompanyID = 'C00039' AND p.AcType3ID = '130007'
                THEN 'Cross Supply - No Assigned Salesman (UBL)'
              WHEN b.CompanyID = 'C00039'
                THEN 'Aabid / Omkar (UBL - KW Beer)'

              ELSE 'Other / Unassigned'
            END AS SalesmanTeam,

            /* ── Channel label (LicenseTypeID-first, then AcType3ID fallback) ── */
            CASE
              WHEN p.LicenseTypeID = '180001'                                THEN 'FL-II Wine Shop'
              WHEN p.LicenseTypeID = '180004'                                THEN 'FL-BR-II Beer Shopee'
              WHEN p.LicenseTypeID = '180005'                                THEN 'FL-IV Club'
              WHEN p.LicenseTypeID = '180007'                                THEN 'FL-IV One Day'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130004'    THEN 'FL-III KW Institution'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130006'    THEN 'FL-III KW Insti One Day'
              WHEN p.LicenseTypeID = '180002' AND p.AcType3ID = '130002'    THEN 'FL-III PCMC Institution'
              WHEN p.LicenseTypeID = '180002'                                THEN 'FL-III Permit Room (Regular)'
              WHEN p.LicenseTypeID = '180003'                                THEN 'Form E'
              WHEN p.LicenseTypeID = '180008'                                THEN 'FL-I'
              WHEN p.AcType3ID     = '130007'                                THEN 'Beer Cross Supply'
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
                      AND PartyID LIKE 'D%'   -- only customer (outlet) parties
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
    agg["RevText"] = agg["TotalAmount"].apply(format_inr)
    # X-axis in Crores for clean tick labels
    agg["RevCr"] = agg["TotalAmount"] / 1e7

    fig = px.bar(
        agg,
        x="RevCr", y="ShortLabel",
        orientation="h",
        color="RevCr",
        color_continuous_scale=["#D6E8F7", _GOLD],
        text="RevText",
        custom_data=["RevText"],
        labels={"RevCr": "Revenue (₹ Cr)", "ShortLabel": ""},
    )
    fig.update_layout(
        **_LAYOUT,
        coloraxis_showscale=False,
        height=max(320, len(agg) * 48),
        xaxis=dict(ticksuffix=" Cr", **_GRID),
        yaxis=dict(**_GRID),
    )
    fig.update_traces(
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>%{customdata[0]}<extra></extra>",
    )
    return fig


_CHANNEL_ORDER = [
    "FL-II Wine Shop",
    "FL-BR-II Beer Shopee",
    "FL-III Permit Room (Regular)",
    "FL-III KW Institution",
    "FL-III KW Insti One Day",
    "FL-III PCMC Institution",
    "FL-IV Club",
    "FL-IV One Day",
    "Beer Cross Supply",
    "Form E",
    "FL-I",
    "Other",
]

_CHANNEL_COLORS = {
    "FL-II Wine Shop":              "#1f77b4",   # blue
    "FL-BR-II Beer Shopee":         "#26C6DA",   # teal
    "FL-III Permit Room (Regular)": "#FF7F0E",   # orange
    "FL-III KW Institution":        "#E8A838",   # amber
    "FL-III KW Insti One Day":      "#FFD54F",   # amber lighter
    "FL-III PCMC Institution":      "#FFEB3B",   # yellow
    "FL-IV Club":                   "#9467bd",   # purple
    "FL-IV One Day":                "#CE93D8",   # purple lighter
    "Beer Cross Supply":            "#E53935",   # red
    "Form E":                       "#9E9E9E",   # grey
    "FL-I":                         "#757575",   # grey medium
    "Other":                        "#616161",   # grey dark
}

_FL3_CHANNELS = [
    "FL-III Permit Room (Regular)",
    "FL-III KW Institution",
    "FL-III KW Insti One Day",
    "FL-III PCMC Institution",
]


def _chart_channel_bar(df: pd.DataFrame) -> go.Figure:
    """Horizontal bar — revenue by channel in fixed license-type order."""
    agg = (
        df.groupby("Channel")["TotalAmount"].sum()
        .reset_index()
    )
    total = agg["TotalAmount"].sum()
    # Build ordered frame — include all channels in spec order, skip zeros
    ordered = [c for c in _CHANNEL_ORDER if c in agg["Channel"].values]
    agg = agg.set_index("Channel").reindex(ordered).dropna().reset_index()
    agg["RevCr"]   = agg["TotalAmount"] / 1e7
    agg["Pct"]     = (agg["TotalAmount"] / total * 100).round(1)
    agg["RevText"] = agg["TotalAmount"].apply(format_inr)
    agg["HoverTxt"] = agg.apply(
        lambda r: f"{r['Channel']}<br>{r['RevText']} ({r['Pct']}%)", axis=1
    )
    colors = [_CHANNEL_COLORS.get(c, "#888888") for c in agg["Channel"]]

    fig = go.Figure(go.Bar(
        x=agg["RevCr"],
        y=agg["Channel"],
        orientation="h",
        marker_color=colors,
        text=[f"₹{v:.2f} Cr  {p}%" for v, p in zip(agg["RevCr"], agg["Pct"])],
        textposition="outside",
        customdata=list(zip(agg["RevText"], agg["Pct"])),
        hovertemplate="<b>%{y}</b><br>%{customdata[0]}  (%{customdata[1]}%)<extra></extra>",
    ))
    fig.update_layout(
        **_LAYOUT,
        height=max(300, len(agg) * 44),
        xaxis=dict(title="Revenue (₹ Cr)", ticksuffix=" Cr", **_GRID),
        yaxis=dict(autorange="reversed", **_GRID),  # top-to-bottom ordering
    )
    return fig


def _chart_fl3_donut(df: pd.DataFrame) -> go.Figure | None:
    """Donut — FL-III Regular vs Institution breakdown. Returns None if no data."""
    fl3 = df[df["Channel"].isin(_FL3_CHANNELS)]
    if fl3.empty:
        return None
    agg = (
        fl3.groupby("Channel")["TotalAmount"].sum()
        .reset_index()
    )
    total = agg["TotalAmount"].sum()
    # Keep spec order, only present segments
    ordered = [c for c in _FL3_CHANNELS if c in agg["Channel"].values]
    agg = agg.set_index("Channel").reindex(ordered).dropna().reset_index()
    agg["RevCr"] = agg["TotalAmount"] / 1e7
    agg["Pct"]   = (agg["TotalAmount"] / total * 100).round(1)
    colors = [_CHANNEL_COLORS[c] for c in agg["Channel"]]

    fig = go.Figure(go.Pie(
        labels=agg["Channel"],
        values=agg["TotalAmount"],
        hole=0.44,
        marker_colors=colors,
        text=[f"₹{v:.2f} Cr" for v in agg["RevCr"]],
        customdata=list(zip(agg["RevCr"], agg["Pct"])),
        textinfo="label+percent",
        hovertemplate=(
            "<b>%{label}</b><br>₹%{customdata[0]:.2f} Cr "
            "(%{customdata[1]:.1f}%)<extra></extra>"
        ),
        pull=[0.05 if "Regular" in c else 0 for c in agg["Channel"]],
    ))
    fig.update_layout(
        **_LAYOUT,
        title=dict(
            text="FL-III Breakdown — Regular vs Institution",
            font=dict(size=14, color="#1A1A1A"),
            x=0.5,
        ),
        annotations=[dict(
            text=f"<b>{format_inr(total)}</b>",
            x=0.5, y=0.5, font_size=13,
            showarrow=False, font_color="#1A1A1A",
        )],
        legend=dict(font=dict(size=11)),
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
    rev_cr = agg["TotalAmount"] / 1e7
    fig = go.Figure(go.Bar(
        x=agg["Principal"],
        y=rev_cr,
        marker_color=colors,
        text=[f"₹{v:.2f} Cr" for v in rev_cr],
        textposition="outside",
        customdata=agg["TotalAmount"].apply(format_inr),
        hovertemplate="<b>%{x}</b><br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        **_LAYOUT,
        showlegend=False,
        xaxis=dict(tickangle=-20, **_GRID),
        yaxis=dict(title="Revenue (₹ Cr)", ticksuffix=" Cr", **_GRID),
    )
    return fig



def _chart_institution_bar(df: pd.DataFrame) -> go.Figure:
    """Side-by-side bar — FL-III institution revenue by channel × principal."""
    agg = (
        df[df["Channel"].isin(_FL3_CHANNELS)]
        .groupby(["Channel", "Principal"])["TotalAmount"]
        .sum()
        .reset_index()
    )
    agg["RevCr"]   = agg["TotalAmount"] / 1e7
    agg["RevText"] = agg["TotalAmount"].apply(format_inr)
    fig = px.bar(
        agg, x="Channel", y="RevCr", color="Principal",
        barmode="group",
        color_discrete_map=PRINCIPAL_COLORS,
        text="RevText",
        custom_data=["RevText"],
        labels={"RevCr": "Revenue (₹ Cr)", "Channel": ""},
    )
    fig.update_layout(
        **_LAYOUT,
        xaxis=_GRID,
        yaxis=dict(ticksuffix=" Cr", **_GRID),
    )
    fig.update_traces(
        textposition="outside",
        hovertemplate="<b>%{x}</b> · %{fullData.name}<br>%{customdata[0]}<extra></extra>",
    )
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
    # Convert to Crores for readable Y-axis
    agg["RevCr"]   = agg["TotalAmount"] / 1e7
    agg["RevText"] = agg["TotalAmount"].apply(format_inr)

    color_map = {
        p: PRINCIPAL_COLORS.get(p, _PAL[i % len(_PAL)])
        for i, p in enumerate(top_principals)
    }
    fig = px.line(
        agg, x="Date", y="RevCr", color="Principal",
        markers=True,
        color_discrete_map=color_map,
        custom_data=["RevText"],
        labels={"RevCr": "Revenue (₹ Cr)", "Date": ""},
    )
    fig.update_layout(
        **_LAYOUT,
        xaxis=_GRID,
        yaxis=dict(ticksuffix=" Cr", **_GRID),
    )
    fig.update_traces(
        line_width=2.5,
        marker_size=4,
        hovertemplate="<b>%{x}</b><br>%{customdata[0]}<extra></extra>",
    )
    return fig


# ── Page entry point ────────────────────────────────────────────────────────

def render():
    st.title("Salesman & Channel Performance")
    st.caption("Revenue attribution by salesman team and channel | Source: TEKNIK ERP")
    st.divider()

    # ── Inline filter row ─────────────────────────────────────────────────
    today    = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

    with st.container():
        fc1, fc2, fc3, fc4 = st.columns([1.1, 1.1, 2.4, 2.4])
        with fc1:
            start = st.date_input("From", value=fy_start, key="sm_from")
        with fc2:
            end = st.date_input("To", value=today, key="sm_to")
        with fc3:
            sel_principals = st.multiselect(
                "Principal",
                options=["United Spirits", "Diageo", "Brown-Forman", "United Breweries", "Others"],
                key="sm_principals",
            )
        with fc4:
            sel_channels = st.multiselect(
                "Channel",
                options=_CHANNEL_ORDER[:-1] + ["Others"],
                key="sm_channels",
            )

    if start > end:
        st.warning("Start date must be before end date.")
        return

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

    # ── Chart 2: Revenue by Channel (fixed license-type order) ───────────
    st.markdown("---")
    section_header("Revenue by Channel",
                   "Ordered by license type · bars coloured by category")
    st.plotly_chart(_chart_channel_bar(df), use_container_width=True)

    # ── Chart 3: FL-III breakdown donut (conditional) ─────────────────────
    fl3_fig = _chart_fl3_donut(df)
    if fl3_fig is not None:
        st.markdown("---")
        col_fl3, col_pr = st.columns(2)
        with col_fl3:
            st.plotly_chart(fl3_fig, use_container_width=True)
        with col_pr:
            st.subheader("Revenue by Principal")
            st.plotly_chart(_chart_principal_bar(df), use_container_width=True)
    else:
        st.markdown("---")
        section_header("Revenue by Principal")
        st.plotly_chart(_chart_principal_bar(df), use_container_width=True)

    # ── Chart 4: Institution breakdown (shown only when data exists) ───────
    inst_df = df[df["Channel"].isin(_FL3_CHANNELS)]
    if not inst_df.empty:
        st.markdown("---")
        section_header(
            "Institution Breakdown",
            "FL-III KW & PCMC institutional revenue by principal",
        )
        st.plotly_chart(_chart_institution_bar(df), use_container_width=True)

    # ── Chart 5: Daily revenue trend by principal ──────────────────────────
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


