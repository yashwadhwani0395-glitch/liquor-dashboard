"""pages/distribution.py — Width of Distribution (WOD) and Depth of Distribution (DOD).

Universe definition (per salesman):
    Distinct outlets (PartyIDs) that billed their principal's products
    in their assigned channel AT ANY POINT in the last 12 months.
    100 % derived from billing history — MsPartyMaster is NOT the source.

WOD % = Unique billed outlets in a month / Universe (12-month baseline) × 100
DOD   = Revenue / Cases / Invoices per billed outlet in the month
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, section_header

# ── Sales transaction type IDs ─────────────────────────────────────────────
SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Chart theme ────────────────────────────────────────────────────────────
_BG     = "rgba(0,0,0,0)"
_GRID   = dict(gridcolor="#2a2d3e")
_LAYOUT = dict(
    paper_bgcolor=_BG, plot_bgcolor=_BG,
    font=dict(color="#FAFAFA"), margin=dict(t=40, b=20),
)
_PAL = px.colors.qualitative.Bold

# ── License type → display label ──────────────────────────────────────────
_LT_LABEL: dict[str, str] = {
    "180001": "FL-II Wine Shop",
    "180002": "FL-III Permit Room",
    "180004": "FL-BR-II Beer Shopee",
    "180005": "FL-IV Club",
    "180007": "FL-IV One Day",
}

# ── Salesman territory definitions ─────────────────────────────────────────
# principals   : CompanyIDs whose products count for this salesman
# license_types: LicenseTypeIDs (party classification) that count
# ac_include   : if present, AcType3ID MUST be in this set
# ac_exclude   : if present, AcType3ID must NOT be in this set
SALESMAN_TERRITORY: dict[str, dict] = {
    # USL Wine Shop team
    "Shashank":       dict(principals={"C00025"},           license_types={"180001"},                    ac_exclude={"130004","130002"}),
    "Sachin":         dict(principals={"C00025"},           license_types={"180001"},                    ac_exclude={"130004","130002"}),
    # Diageo + BF Wine Shop team
    "Ajay":           dict(principals={"C00040","C00056"},  license_types={"180001"},                    ac_exclude={"130004","130002"}),
    "Deepak Patil":   dict(principals={"C00040","C00056"},  license_types={"180001"},                    ac_exclude={"130004","130002"}),
    # USL + Diageo Permit Room team (regular only)
    "Tulsiram":       dict(principals={"C00025","C00040"},  license_types={"180002"},                    ac_exclude={"130004","130002","130006"}),
    "Saurabh":        dict(principals={"C00025","C00040"},  license_types={"180002"},                    ac_exclude={"130004","130002","130006"}),
    "Miran":          dict(principals={"C00025","C00040"},  license_types={"180002"},                    ac_exclude={"130004","130002","130006"}),
    "Prashant":       dict(principals={"C00025","C00040"},  license_types={"180002"},                    ac_exclude={"130004","130002","130006"}),
    "Atish":          dict(principals={"C00025","C00040"},  license_types={"180002"},                    ac_exclude={"130004","130002","130006"}),
    # UBL all channels (non-institution)
    "Aabid":          dict(principals={"C00039"},           license_types={"180001","180002","180004"},  ac_exclude={"130004","130002","130006"}),
    "Omkar":          dict(principals={"C00039"},           license_types={"180001","180002","180004"},  ac_exclude={"130004","130002","130006"}),
    # KW Institution team (UBL + BF)
    "Anand Raj":      dict(principals={"C00039","C00056"},  license_types={"180002"},                    ac_include={"130004","130006"}),
    "Deepak Pangare": dict(principals={"C00039","C00056"},  license_types={"180002"},                    ac_include={"130004","130006"}),
    "Shashank Desai": dict(principals={"C00039","C00056"},  license_types={"180002"},                    ac_include={"130004","130006"}),
    "Pranav":         dict(principals={"C00039","C00056"},  license_types={"180002"},                    ac_include={"130004","130006"}),
    # PCMC Institution team (UBL)
    "Gajendra Das":   dict(principals={"C00039"},           license_types={"180002"},                    ac_include={"130002"}),
    "Amol Sathe":     dict(principals={"C00039"},           license_types={"180002"},                    ac_include={"130002"}),
    "Rahul Ghone":    dict(principals={"C00039"},           license_types={"180002"},                    ac_include={"130002"}),
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _month_label(yr: int, mo: int) -> str:
    return date(yr, mo, 1).strftime("%b %Y")


def _last_n_months(n: int = 12) -> list[tuple[int, int]]:
    """Return (year, month) tuples for the last n months, oldest → newest."""
    today = date.today()
    result = []
    for i in range(n - 1, -1, -1):
        mo = today.month - i
        yr = today.year
        while mo <= 0:
            mo += 12
            yr -= 1
        result.append((yr, mo))
    return result


def _prev_month(yr: int, mo: int) -> tuple[int, int]:
    return (yr, mo - 1) if mo > 1 else (yr - 1, 12)


def _filter_billing(billing: pd.DataFrame, sm_key: str) -> pd.DataFrame:
    """Filter billing to rows matching this salesman's territory."""
    t    = SALESMAN_TERRITORY[sm_key]
    mask = (
        billing["CompanyID"].isin(t["principals"])
        & billing["LicenseTypeID"].isin(t["license_types"])
    )
    if "ac_include" in t:
        mask &= billing["AcType3ID"].isin(t["ac_include"])
    elif "ac_exclude" in t:
        mask &= ~billing["AcType3ID"].isin(t["ac_exclude"])
    return billing[mask]


# ── Data loader ────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _load_billing(months_back: int = 13) -> pd.DataFrame:
    """Load billing history: (yr, mo, PartyID, PartyName, LicenseTypeID,
    AcType3ID, CompanyID, Invoices, Cases, Revenue).

    Grouped by month × party × principal — so a party billed for USL *and*
    UBL in the same month produces two rows (one per principal).  This lets
    each salesman's territory filter work correctly in Python.

    Notes:
    - Party IDs are D-prefixed in this DB — no LIKE filter applied.
    - SalesManID is blank in TrVocHead, so attribution is territory-based.
    - FY deduplication on TrVocItem via computed FinancialYear.
    """
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    df = run_query(f"""
        SELECT
            YEAR(h.VoucherDate)                    AS yr,
            MONTH(h.VoucherDate)                   AS mo,
            d.PartyID,
            ISNULL(p.PartyName,    'Unknown')      AS PartyName,
            ISNULL(p.LicenseTypeID,'')             AS LicenseTypeID,
            ISNULL(p.AcType3ID,    '')             AS AcType3ID,
            b.CompanyID,
            COUNT(DISTINCT h.VoucherNo)            AS Invoices,
            SUM(CAST(vi.CaseQty     AS BIGINT))    AS Cases,
            SUM(CAST(vi.TotalAmount AS FLOAT))     AS Revenue
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FreeItemYN  = 'N'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
            END
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID
            FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (
                           PARTITION BY TransTypeID, VoucherNo
                           ORDER BY Amount DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator = 'D'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsPartyMaster p  ON p.PartyID  = d.PartyID
        JOIN MsItemMaster  im ON im.ItemID  = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND d.PartyID     IS NOT NULL
          AND p.LicenseTypeID IN ('180001','180002','180004','180005')
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY
            YEAR(h.VoucherDate), MONTH(h.VoucherDate),
            d.PartyID, p.PartyName, p.LicenseTypeID, p.AcType3ID, b.CompanyID
    """)
    if df.empty:
        return df
    for col in ("yr", "mo", "Invoices", "Cases"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["Revenue"]       = pd.to_numeric(df["Revenue"],       errors="coerce").fillna(0.0)
    df["LicenseTypeID"] = df["LicenseTypeID"].fillna("").astype(str)
    df["AcType3ID"]     = df["AcType3ID"].fillna("").astype(str)
    df["CompanyID"]     = df["CompanyID"].fillna("").astype(str)
    return df


# ── Analytics ──────────────────────────────────────────────────────────────

def _compute_wod(
    billing: pd.DataFrame,
    months: list[tuple[int, int]],
    sel_sm: list[str],
) -> pd.DataFrame:
    """Compute WOD per (salesman, month).

    Universe  = distinct PartyIDs that billed the salesman's principal's
                products in their channel any time in the 13-month window.
    Billed(m) = those PartyIDs that billed in month m specifically.
    Revenue / Cases / Invoices from that salesman's principal's products only.
    """
    # Pre-filter once per salesman; build frozen universe sets
    sm_bill: dict[str, pd.DataFrame] = {}
    uni_ids: dict[str, frozenset]    = {}
    for sm in sel_sm:
        tb          = _filter_billing(billing, sm)
        sm_bill[sm] = tb
        uni_ids[sm] = frozenset(tb["PartyID"].unique())

    rows = []
    for yr, mo in months:
        for sm in sel_sm:
            tb         = sm_bill[sm]
            universe   = uni_ids[sm]
            uni_size   = len(universe)
            month_tb   = tb[(tb["yr"] == yr) & (tb["mo"] == mo)]
            billed_ids = frozenset(month_tb["PartyID"].unique())
            billed_cnt = len(billed_ids)
            wod_pct    = billed_cnt / uni_size * 100 if uni_size else 0.0
            rows.append({
                "sm_key":      sm,
                "month_label": _month_label(yr, mo),
                "yr":          yr,
                "mo":          mo,
                "universe":    uni_size,
                "billed":      billed_cnt,
                "unbilled":    uni_size - billed_cnt,
                "wod_pct":     round(wod_pct, 1),
                "revenue":     float(month_tb["Revenue"].sum()),
                "cases":       int(month_tb["Cases"].sum()),
                "invoices":    int(month_tb["Invoices"].sum()),
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _unbilled_outlets(
    billing: pd.DataFrame,
    sel_month: tuple[int, int],
    sm_key: str,
) -> pd.DataFrame:
    """Universe outlets that did NOT bill in sel_month for this salesman."""
    yr, mo = sel_month
    tb      = _filter_billing(billing, sm_key)
    universe = (
        tb[["PartyID","PartyName","LicenseTypeID","AcType3ID"]]
        .drop_duplicates("PartyID")
    )
    billed_ids = frozenset(tb[(tb["yr"] == yr) & (tb["mo"] == mo)]["PartyID"])
    unbilled   = universe[~universe["PartyID"].isin(billed_ids)].copy()

    if unbilled.empty:
        return unbilled

    # Last billed month from history (any month in window)
    tb2          = tb.copy()
    tb2["yrmo"]  = tb2["yr"] * 100 + tb2["mo"]
    last_bill    = tb2.groupby("PartyID")["yrmo"].max().reset_index()
    last_bill["last_bill_date"] = last_bill["yrmo"].apply(
        lambda v: date(int(v) // 100, int(v) % 100, 1)
    )
    unbilled = unbilled.merge(last_bill[["PartyID","last_bill_date"]], on="PartyID", how="left")

    today = date.today()
    unbilled["last_bill_date"] = pd.to_datetime(unbilled["last_bill_date"], errors="coerce")
    unbilled["DaysSince"]  = unbilled["last_bill_date"].apply(
        lambda d: (today - d.date()).days if pd.notna(d) else 9999
    )
    unbilled["LastBilled"] = unbilled["last_bill_date"].apply(
        lambda d: d.strftime("%b %Y") if pd.notna(d) else "Never"
    )
    unbilled["Channel"]    = unbilled["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return unbilled.sort_values("DaysSince", ascending=False)


def _outlet_detail(
    billing: pd.DataFrame,
    sel_month: tuple[int, int],
    sm_key: str,
) -> pd.DataFrame:
    """Per-outlet billing detail for a salesman in one month (universe only)."""
    yr, mo   = sel_month
    tb       = _filter_billing(billing, sm_key)
    month_tb = tb[(tb["yr"] == yr) & (tb["mo"] == mo)].copy()
    if month_tb.empty:
        return month_tb

    detail = (
        month_tb
        .groupby(["PartyID","PartyName","LicenseTypeID"], as_index=False)
        .agg(Invoices=("Invoices","sum"), Cases=("Cases","sum"), Revenue=("Revenue","sum"))
    )
    detail["AvgOrder"] = detail["Revenue"] / detail["Invoices"].clip(lower=1)
    detail["Channel"]  = detail["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return (
        detail[["PartyName","Channel","Invoices","Cases","Revenue","AvgOrder"]]
        .rename(columns={"PartyName":"Party","AvgOrder":"Avg Order Value"})
        .sort_values("Revenue", ascending=False)
    )


# ── Chart builders ─────────────────────────────────────────────────────────

def _chart_wod_trend(
    wod_df: pd.DataFrame,
    months: list[tuple[int, int]],
    target_pct: float,
) -> go.Figure:
    ordered_labels = [_month_label(y, m) for y, m in months]
    plot_df = wod_df.copy()
    plot_df["month_ord"] = plot_df["yr"] * 100 + plot_df["mo"]
    plot_df = plot_df.sort_values("month_ord")

    fig = px.line(
        plot_df,
        x="month_label", y="wod_pct", color="sm_key",
        markers=True,
        labels={"wod_pct": "WOD %", "month_label": "", "sm_key": "Salesman"},
        color_discrete_sequence=_PAL,
        category_orders={"month_label": ordered_labels},
    )
    fig.add_hline(
        y=target_pct,
        line_dash="dash", line_color="#FF4444", line_width=1.5,
        annotation_text=f"Target {target_pct:.0f}%",
        annotation_font_color="#FF4444",
    )
    fig.update_layout(
        **_LAYOUT,
        xaxis=dict(tickangle=-30, **_GRID),
        yaxis=dict(range=[0, 105], ticksuffix="%", **_GRID),
        legend=dict(font=dict(size=10), title=""),
    )
    fig.update_traces(line_width=2, marker_size=5)
    return fig


# ── Page entry point ───────────────────────────────────────────────────────

def render():
    st.title("Distribution — WOD & DOD")

    all_sm = sorted(SALESMAN_TERRITORY.keys())

    # ── Sidebar ───────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("#### Filters")
        sel_sm = st.multiselect("Salesman", options=all_sm, default=all_sm, key="dist_sm")
        if not sel_sm:
            sel_sm = all_sm

        months_12    = _last_n_months(12)
        month_labels = [_month_label(y, m) for y, m in months_12]
        label_newest = list(reversed(month_labels))

        sel_label = st.selectbox(
            "Detail Month", options=label_newest, index=0, key="dist_month"
        )
        sel_month = months_12[month_labels.index(sel_label)]

        target_wod = st.number_input(
            "Target WOD %",
            min_value=10.0, max_value=100.0, value=70.0, step=5.0,
            key="dist_target",
        )

    # ── Load billing data ─────────────────────────────────────────────────
    with st.spinner("Loading 13-month billing data…"):
        billing = _load_billing(13)

    if billing.empty:
        st.error("No billing data returned. Check database connection.")
        return

    # ── Compute WOD ───────────────────────────────────────────────────────
    with st.spinner("Computing WOD…"):
        wod_df = _compute_wod(billing, months_12, sel_sm)

    sel_yr, sel_mo   = sel_month
    prev_yr, prev_mo = _prev_month(sel_yr, sel_mo)

    # ── KPI row: unique parties across selected salesmen's territories ────
    all_uni_ids = set()
    for sm in sel_sm:
        all_uni_ids |= set(_filter_billing(billing, sm)["PartyID"].unique())

    billed_this_month = set(
        billing[(billing["yr"] == sel_yr) & (billing["mo"] == sel_mo)]["PartyID"]
    )
    total_uni      = len(all_uni_ids)
    total_billed   = len(all_uni_ids & billed_this_month)
    total_unbilled = total_uni - total_billed
    overall_wod    = total_billed / total_uni * 100 if total_uni else 0.0

    section_header("Width & Depth of Distribution", f"Detail month: {sel_label}")
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("Total Universe", f"{total_uni:,}")
    with k2:
        st.metric("Billed Outlets", f"{total_billed:,}")
    with k3:
        st.metric(
            "Overall WOD %", f"{overall_wod:.1f}%",
            delta=f"{overall_wod - target_wod:+.1f}% vs target",
            delta_color="normal",
        )
    with k4:
        st.metric("Unbilled Outlets", f"{total_unbilled:,}")

    st.divider()

    # ── Section 1: WOD Trend ──────────────────────────────────────────────
    section_header("Width of Distribution — Monthly Trend")
    if not wod_df.empty:
        st.plotly_chart(_chart_wod_trend(wod_df, months_12, target_wod), use_container_width=True)
    else:
        st.info("No WOD data to chart.")

    # ── Section 2: WOD Summary Table ─────────────────────────────────────
    st.markdown("---")
    section_header("WOD Summary", f"{sel_label} · sorted worst first")
    sum_df = (
        wod_df[(wod_df["yr"] == sel_yr) & (wod_df["mo"] == sel_mo)]
        .copy()
        .sort_values("wod_pct")
    )
    if not sum_df.empty:
        sum_df["Target %"]  = target_wod
        sum_df["vs Target"] = (sum_df["wod_pct"] - target_wod).round(1)
        disp = sum_df[[
            "sm_key","universe","billed","unbilled","wod_pct","Target %","vs Target"
        ]].copy()
        disp.columns = ["Salesman","Universe","Billed","Unbilled","WOD %","Target %","vs Target"]
        disp["WOD %"]     = disp["WOD %"].apply(lambda x: f"{x:.1f}%")
        disp["Target %"]  = disp["Target %"].apply(lambda x: f"{x:.0f}%")
        disp["vs Target"] = disp["vs Target"].apply(lambda x: f"{x:+.1f}%")
        st.dataframe(disp, use_container_width=True, hide_index=True)
    else:
        st.info("No data for selected month.")

    # ── Section 3: DOD Metrics ────────────────────────────────────────────
    st.markdown("---")
    section_header(
        "Depth of Distribution — Avg per Billed Outlet",
        f"{sel_label} vs {_month_label(prev_yr, prev_mo)}",
    )

    def _dod(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        bs  = out["billed"].clip(lower=1)
        out["rev_out"] = out["revenue"] / bs
        out["cas_out"] = out["cases"]   / bs
        out["inv_out"] = out["invoices"] / bs
        return out[["sm_key","rev_out","cas_out","inv_out"]]

    cur_dod  = _dod(wod_df[(wod_df["yr"] == sel_yr)  & (wod_df["mo"] == sel_mo)])
    prev_dod = _dod(wod_df[(wod_df["yr"] == prev_yr) & (wod_df["mo"] == prev_mo)])

    if not cur_dod.empty:
        dod = cur_dod.merge(
            prev_dod.rename(columns={
                "rev_out":"prev_rev","cas_out":"prev_cas","inv_out":"prev_inv"
            }),
            on="sm_key", how="left",
        )
        dod["delta_rev"] = dod["rev_out"] - dod["prev_rev"].fillna(0)
        dod["delta_cas"] = dod["cas_out"] - dod["prev_cas"].fillna(0)

        disp_dod = dod[["sm_key","rev_out","cas_out","inv_out","delta_rev","delta_cas"]].copy()
        disp_dod.columns = [
            "Salesman","Avg Rev/Outlet","Avg Cases/Outlet",
            "Avg Inv/Outlet","Δ Rev/Outlet","Δ Cases/Outlet",
        ]
        disp_dod["Avg Rev/Outlet"]   = disp_dod["Avg Rev/Outlet"].apply(format_inr)
        disp_dod["Avg Cases/Outlet"] = disp_dod["Avg Cases/Outlet"].apply(lambda x: f"{x:.1f}")
        disp_dod["Avg Inv/Outlet"]   = disp_dod["Avg Inv/Outlet"].apply(lambda x: f"{x:.1f}")
        disp_dod["Δ Rev/Outlet"]     = disp_dod["Δ Rev/Outlet"].apply(
            lambda x: ("+" if x >= 0 else "−") + format_inr(abs(x))
        )
        disp_dod["Δ Cases/Outlet"]   = disp_dod["Δ Cases/Outlet"].apply(lambda x: f"{x:+.1f}")
        st.dataframe(disp_dod, use_container_width=True, hide_index=True)
    else:
        st.info("No DOD data for selected month.")

    # ── Section 4: Unbilled Outlets (Action List) ─────────────────────────
    st.markdown("---")
    focus_sm = sel_sm[0] if sel_sm else None
    section_header(
        f"Unbilled Outlets — {focus_sm or '—'}",
        f"{sel_label} · longest gap first · select one salesman in sidebar for focus",
    )
    if focus_sm:
        with st.spinner("Computing unbilled outlets…"):
            unbilled_df = _unbilled_outlets(billing, sel_month, focus_sm)
        if unbilled_df.empty:
            st.success(f"All universe outlets billed in {sel_label}!")
        else:
            uni_size = int(wod_df[
                (wod_df["yr"] == sel_yr) & (wod_df["mo"] == sel_mo)
                & (wod_df["sm_key"] == focus_sm)
            ]["universe"].sum()) or len(_filter_billing(billing, focus_sm)["PartyID"].unique())
            disp_ub = unbilled_df[["PartyName","Channel","LastBilled","DaysSince"]].copy()
            disp_ub["DaysSince"] = disp_ub["DaysSince"].apply(
                lambda x: "—" if x == 9999 else str(x)
            )
            disp_ub.columns = ["Party","Channel","Last Billed","Days Since Last Bill"]
            st.dataframe(disp_ub, use_container_width=True, hide_index=True)
            st.caption(
                f"{len(disp_ub)} unbilled out of {uni_size} universe outlets "
                f"({len(disp_ub)/uni_size*100:.1f}% not covered)" if uni_size else ""
            )

    # ── Section 5: Outlet-level Detail (expander) ─────────────────────────
    st.markdown("---")
    with st.expander(
        f"Outlet Detail — {focus_sm or '—'} · {sel_label}", expanded=False
    ):
        if focus_sm:
            det_df = _outlet_detail(billing, sel_month, focus_sm)
            if not det_df.empty:
                d = det_df.copy()
                d["Revenue"]         = d["Revenue"].apply(format_inr)
                d["Avg Order Value"] = d["Avg Order Value"].apply(format_inr)
                st.dataframe(d, use_container_width=True, hide_index=True)
            else:
                st.info(f"No billed outlets for {focus_sm} in {sel_label}.")
        else:
            st.info("Select a salesman to see outlet detail.")
