"""pages/distribution.py — Width of Distribution (WOD) and Depth of Distribution (DOD).

WOD % = Unique outlets billed in a month / Universe × 100
DOD   = Revenue / Cases / Invoices per billed outlet
"""
from __future__ import annotations

import calendar
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr, section_header

# ── Sales transaction type IDs ─────────────────────────────────────────────
SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── AcType3ID codes that mark institutional outlets ────────────────────────
INSTITUTION_CODES: frozenset[str] = frozenset({"130004", "130006", "130002"})

# ── Chart theme ────────────────────────────────────────────────────────────
_BG     = "rgba(0,0,0,0)"
_GOLD   = "#E8A838"
_GRID   = dict(gridcolor="#2a2d3e")
_LAYOUT = dict(
    paper_bgcolor=_BG, plot_bgcolor=_BG,
    font=dict(color="#FAFAFA"), margin=dict(t=40, b=20),
)
_PAL = px.colors.qualitative.Bold

# ── License type → channel label ───────────────────────────────────────────
_LT_LABEL = {
    "180001": "FL-II Wine Shop",
    "180002": "FL-III Permit Room",
    "180004": "FL-BR-II Beer Shopee",
    "180005": "FL-IV Club",
    "180007": "FL-IV One Day",
}

# ── Individual salesman → territory definition ─────────────────────────────
SALESMAN_MAP: dict[str, dict] = {
    # USL Wine Shops
    "Shashank":       {"principals": ["C00025"],          "license_types": ["180001"],                    "ac_types": []},
    "Sachin":         {"principals": ["C00025"],          "license_types": ["180001"],                    "ac_types": []},
    # USL + Diageo Permit Rooms (regular only)
    "Tulsiram":       {"principals": ["C00025","C00040"], "license_types": ["180002"],                    "ac_types": ["regular"]},
    "Saurabh":        {"principals": ["C00025","C00040"], "license_types": ["180002"],                    "ac_types": ["regular"]},
    "Miran":          {"principals": ["C00025","C00040"], "license_types": ["180002"],                    "ac_types": ["regular"]},
    "Prashant":       {"principals": ["C00025","C00040"], "license_types": ["180002"],                    "ac_types": ["regular"]},
    "Atish":          {"principals": ["C00025","C00040"], "license_types": ["180002"],                    "ac_types": ["regular"]},
    # Diageo + BF Wine Shops
    "Ajay":           {"principals": ["C00040","C00056"], "license_types": ["180001"],                    "ac_types": []},
    "Deepak Patil":   {"principals": ["C00040","C00056"], "license_types": ["180001"],                    "ac_types": []},
    # UBL — all channels except institution
    "Aabid":          {"principals": ["C00039"],          "license_types": ["180001","180002","180004"],  "ac_types": ["130001","130007","regular"]},
    "Omkar":          {"principals": ["C00039"],          "license_types": ["180001","180002","180004"],  "ac_types": ["130001","130007","regular"]},
    # KW Institution team
    "Anand Raj":      {"principals": ["C00039","C00056"], "license_types": ["180002"],                    "ac_types": ["130004","130006"]},
    "Deepak Pangare": {"principals": ["C00039","C00056"], "license_types": ["180002"],                    "ac_types": ["130004","130006"]},
    "Shashank Desai": {"principals": ["C00039","C00056"], "license_types": ["180002"],                    "ac_types": ["130004","130006"]},
    "Pranav":         {"principals": ["C00039","C00056"], "license_types": ["180002"],                    "ac_types": ["130004","130006"]},
    # PCMC Institution team
    "Gajendra Das":   {"principals": ["C00039"],          "license_types": ["180002"],                    "ac_types": ["130002"]},
    "Amol Sathe":     {"principals": ["C00039"],          "license_types": ["180002"],                    "ac_types": ["130002"]},
    "Rahul Ghone":    {"principals": ["C00039"],          "license_types": ["180002"],                    "ac_types": ["130002"]},
}

# ── DB name patterns (longest first = highest priority in matching) ─────────
_DB_NAME_MAP: dict[str, str] = {
    "Shashank Desai": "SHASHANK DESAI",
    "Deepak Pangare": "DEEPAK PANGARE",
    "Gajendra Das":   "GAJENDRA DAS",
    "Deepak Patil":   "DEEPAK PATIL",
    "Amol Sathe":     "AMOL SATHE",
    "Rahul Ghone":    "RAHUL GHONE",
    "Anand Raj":      "ANAND RAJ",
    "Shashank":       "SHASHANK",
    "Tulsiram":       "TULSIRAM",
    "Prashant":       "PRASHANT",
    "Saurabh":        "SAURABH",
    "Pranav":         "PRANAV",
    "Sachin":         "SACHIN",
    "Omkar":          "OMKAR",
    "Miran":          "MIRAN",
    "Atish":          "ATISH",
    "Aabid":          "ABID",
    "Ajay":           "AJAY",
}
_SORTED_PATTERNS = sorted(_DB_NAME_MAP.items(), key=lambda x: -len(x[1]))


# ── Helpers ────────────────────────────────────────────────────────────────

def _match_db_name(fullname: str) -> str | None:
    """Match a DB FullName to a SALESMAN_MAP key. Longest pattern wins."""
    upper = fullname.strip().upper()
    for sm_key, pattern in _SORTED_PATTERNS:
        if pattern in upper:
            return sm_key
    return None


def _filter_universe(parties: pd.DataFrame, sm_key: str) -> pd.DataFrame:
    """Return party rows matching this salesman's territory definition."""
    cfg   = SALESMAN_MAP[sm_key]
    lts   = cfg["license_types"]
    acs   = cfg.get("ac_types", [])

    mask = parties["LicenseTypeID"].isin(lts)

    if acs:
        specific  = [a for a in acs if a != "regular"]
        has_reg   = "regular" in acs
        if specific and has_reg:
            # Include explicit codes + any non-institution code
            ac_mask = (
                parties["AcType3ID"].isin(specific)
                | ~parties["AcType3ID"].isin(INSTITUTION_CODES)
            )
        elif specific:
            ac_mask = parties["AcType3ID"].isin(specific)
        else:
            ac_mask = ~parties["AcType3ID"].isin(INSTITUTION_CODES)
        mask = mask & ac_mask

    return parties[mask]


def _month_label(yr: int, mo: int) -> str:
    return date(yr, mo, 1).strftime("%b %Y")


def _last_n_months(n: int = 12) -> list[tuple[int, int]]:
    """(year, month) for last n months, oldest → newest."""
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


# ── Data loaders ───────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _load_parties() -> pd.DataFrame:
    """All active (not banned) parties with classification fields."""
    return run_query("""
        SELECT
            PartyID,
            PartyName,
            ISNULL(LicenseTypeID, '') AS LicenseTypeID,
            ISNULL(AcType3ID, '')     AS AcType3ID,
            ISNULL(ClassID,   '')     AS ClassID
        FROM MsPartyMaster
        WHERE BannedPartyYN = 'N'
          AND LicenseTypeID IN ('180001','180002','180004','180005','180007')
    """)


@st.cache_data(ttl=3600, show_spinner=False)
def _load_salesman_db_map() -> dict[str, str]:
    """Return {SalesManID (str): sm_key} by matching DB FullNames."""
    df = run_query(
        "SELECT SalesManID, FullName FROM MsSalesmanMaster "
        "WHERE ResignDate IS NULL ORDER BY FullName"
    )
    result: dict[str, str] = {}
    for _, row in df.iterrows():
        key = _match_db_name(str(row["FullName"]))
        if key:
            result[str(row["SalesManID"])] = key
    return result


@st.cache_data(ttl=300, show_spinner=False)
def _load_billing(months_back: int = 13) -> pd.DataFrame:
    """Billing rows for last N months: (yr, mo, SalesManID, PartyID, ...).

    FinancialYear is computed from VoucherDate to deduplicate TrVocItem rows
    — both FY tags exist for the same vouchers across the fiscal-year boundary.
    """
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    df = run_query(f"""
        SELECT
            YEAR(h.VoucherDate)                         AS yr,
            MONTH(h.VoucherDate)                        AS mo,
            h.SalesManID,
            d.PartyID,
            ISNULL(p.PartyName,     'Unknown')          AS PartyName,
            ISNULL(p.LicenseTypeID, '')                 AS LicenseTypeID,
            ISNULL(p.AcType3ID,     '')                 AS AcType3ID,
            COUNT(DISTINCT h.VoucherNo)                 AS Invoices,
            SUM(CAST(vi.CaseQty     AS BIGINT))         AS Cases,
            SUM(CAST(vi.TotalAmount AS FLOAT))          AS Revenue
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
        LEFT JOIN (
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
        ) d  ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        LEFT JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND h.SalesManID  IS NOT NULL
          AND d.PartyID     IS NOT NULL
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY
            YEAR(h.VoucherDate), MONTH(h.VoucherDate),
            h.SalesManID, d.PartyID, p.PartyName,
            p.LicenseTypeID, p.AcType3ID
    """)
    if df.empty:
        return df
    for col in ("yr", "mo", "Invoices", "Cases"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["Revenue"]    = pd.to_numeric(df["Revenue"],    errors="coerce").fillna(0.0)
    df["SalesManID"] = df["SalesManID"].astype(str)
    return df


# ── Analytics ──────────────────────────────────────────────────────────────

def _compute_wod(
    billing: pd.DataFrame,
    parties: pd.DataFrame,
    db_map: dict[str, str],
    months: list[tuple[int, int]],
    sel_sm: list[str],
) -> pd.DataFrame:
    """Compute WOD metrics per (salesman, month).

    Only bills at universe parties are counted — bills outside a
    salesman's territory are excluded from WOD.
    """
    bill = billing.copy()
    bill["sm_key"] = bill["SalesManID"].map(db_map)
    bill = bill[bill["sm_key"].isin(sel_sm)]

    # Pre-build universe sets (expensive only once)
    uni_sets = {sm: set(_filter_universe(parties, sm)["PartyID"]) for sm in sel_sm}

    rows = []
    for sm in sel_sm:
        uni_ids  = uni_sets[sm]
        uni_size = len(uni_ids)
        sm_bill  = bill[bill["sm_key"] == sm]

        for yr, mo in months:
            mb = sm_bill[(sm_bill["yr"] == yr) & (sm_bill["mo"] == mo)]
            mb_in_uni    = mb[mb["PartyID"].isin(uni_ids)]
            billed_count = mb_in_uni["PartyID"].nunique()
            wod_pct      = billed_count / uni_size * 100 if uni_size else 0.0
            rows.append({
                "sm_key":       sm,
                "month_label":  _month_label(yr, mo),
                "yr":           yr,
                "mo":           mo,
                "universe":     uni_size,
                "billed":       billed_count,
                "unbilled":     uni_size - billed_count,
                "wod_pct":      round(wod_pct, 1),
                "revenue":      float(mb_in_uni["Revenue"].sum()),
                "cases":        int(mb_in_uni["Cases"].sum()),
                "invoices":     int(mb_in_uni["Invoices"].sum()),
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _unbilled_outlets(
    billing: pd.DataFrame,
    parties: pd.DataFrame,
    db_map: dict[str, str],
    sel_month: tuple[int, int],
    sm_key: str,
) -> pd.DataFrame:
    """Return unbilled universe outlets for one salesman in the selected month."""
    yr, mo = sel_month
    bill = billing.copy()
    bill["sm_key"] = bill["SalesManID"].map(db_map)

    uni_df = _filter_universe(parties, sm_key).copy()

    sm_bill = bill[bill["sm_key"] == sm_key]

    # Parties billed this month
    billed_this = set(
        sm_bill[(sm_bill["yr"] == yr) & (sm_bill["mo"] == mo)]["PartyID"]
    )
    unbilled_df = uni_df[~uni_df["PartyID"].isin(billed_this)].copy()

    if unbilled_df.empty:
        return unbilled_df

    # Last bill date per party (any month in history, by this salesman)
    _tmp = sm_bill.copy()
    _tmp["yrmo"] = _tmp["yr"] * 100 + _tmp["mo"]
    last_bill = _tmp.groupby("PartyID")["yrmo"].max().reset_index()
    last_bill["last_bill_date"] = last_bill["yrmo"].apply(
        lambda v: date(int(v) // 100, int(v) % 100, 1)
    )
    last_bill = last_bill[["PartyID", "last_bill_date"]]

    unbilled_df = unbilled_df.merge(last_bill, on="PartyID", how="left")
    today = date.today()
    unbilled_df["last_bill_date"] = pd.to_datetime(
        unbilled_df["last_bill_date"], errors="coerce"
    )
    unbilled_df["DaysSince"] = unbilled_df["last_bill_date"].apply(
        lambda d: (today - d.date()).days if pd.notna(d) else 9999
    )
    unbilled_df["LastBilled"] = unbilled_df["last_bill_date"].apply(
        lambda d: d.strftime("%b %Y") if pd.notna(d) else "Never"
    )
    unbilled_df["Channel"] = unbilled_df["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return unbilled_df.sort_values("DaysSince", ascending=False)


def _outlet_detail(
    billing: pd.DataFrame,
    parties: pd.DataFrame,
    db_map: dict[str, str],
    sel_month: tuple[int, int],
    sm_key: str,
) -> pd.DataFrame:
    """Per-outlet billing detail for a salesman in a month (universe only)."""
    yr, mo = sel_month
    bill = billing.copy()
    bill["sm_key"] = bill["SalesManID"].map(db_map)

    uni_ids = set(_filter_universe(parties, sm_key)["PartyID"])
    detail = bill[
        (bill["sm_key"] == sm_key)
        & (bill["yr"] == yr)
        & (bill["mo"] == mo)
        & (bill["PartyID"].isin(uni_ids))
    ].copy()

    if detail.empty:
        return detail

    detail["AvgOrder"] = detail["Revenue"] / detail["Invoices"].clip(lower=1)
    detail["Channel"]  = detail["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    return (
        detail[["PartyName", "Channel", "Invoices", "Cases", "Revenue", "AvgOrder"]]
        .rename(columns={"PartyName": "Party", "AvgOrder": "Avg Order Value"})
        .sort_values("Revenue", ascending=False)
    )


# ── Chart builders ──────────────────────────────────────────────────────────

def _chart_wod_trend(
    wod_df: pd.DataFrame,
    months: list[tuple[int, int]],
    target_pct: float,
) -> go.Figure:
    """Line chart: WOD % per salesman over last 12 months."""
    ordered_labels = [_month_label(y, m) for y, m in months]
    # Ensure month order on X axis
    wod_df = wod_df.copy()
    wod_df["month_ord"] = wod_df.apply(lambda r: r["yr"] * 100 + r["mo"], axis=1)
    wod_df = wod_df.sort_values("month_ord")

    fig = px.line(
        wod_df,
        x="month_label", y="wod_pct", color="sm_key",
        markers=True,
        labels={"wod_pct": "WOD %", "month_label": "", "sm_key": "Salesman"},
        color_discrete_sequence=_PAL,
        category_orders={"month_label": ordered_labels},
    )
    # Target dashed line
    fig.add_hline(
        y=target_pct,
        line_dash="dash",
        line_color="#FF4444",
        line_width=1.5,
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


# ── Page entry point ────────────────────────────────────────────────────────

def render():
    st.title("Distribution — WOD & DOD")

    # ── Load static data ──────────────────────────────────────────────────
    with st.spinner("Loading party universe…"):
        parties = _load_parties()
        db_map  = _load_salesman_db_map()

    matched_sm  = sorted({v for v in db_map.values() if v in SALESMAN_MAP})
    unmapped_sm = [s for s in SALESMAN_MAP if s not in matched_sm]

    # ── Sidebar ───────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("#### Filters")

        sel_sm = st.multiselect(
            "Salesman",
            options=matched_sm,
            default=matched_sm,
            key="dist_sm",
        )
        if not sel_sm:
            sel_sm = matched_sm

        months_12     = _last_n_months(12)
        month_labels  = [_month_label(y, m) for y, m in months_12]
        label_newest  = list(reversed(month_labels))

        sel_label = st.selectbox(
            "Detail Month",
            options=label_newest,
            index=0,
            key="dist_month",
        )
        sel_month = months_12[month_labels.index(sel_label)]

        target_wod = st.number_input(
            "Target WOD %",
            min_value=10.0, max_value=100.0,
            value=70.0, step=5.0,
            key="dist_target",
        )

    # ── Load billing data ─────────────────────────────────────────────────
    with st.spinner("Loading 12-month billing data…"):
        billing = _load_billing(13)

    if billing.empty or parties.empty:
        st.error("No data returned. Check database connection.")
        return

    # ── Compute WOD across all 12 months ──────────────────────────────────
    with st.spinner("Computing WOD…"):
        wod_df = _compute_wod(billing, parties, db_map, months_12, sel_sm)

    if wod_df.empty:
        st.warning(
            "No billing records matched for the selected salesmen. "
            "Salesman name matching may need adjustment."
        )
        if unmapped_sm:
            st.info(f"Could not match in DB: {', '.join(unmapped_sm)}")
        return

    sel_yr, sel_mo = sel_month
    prev_yr, prev_mo = _prev_month(sel_yr, sel_mo)

    # ── KPI row ───────────────────────────────────────────────────────────
    month_wod     = wod_df[(wod_df["yr"] == sel_yr) & (wod_df["mo"] == sel_mo)]
    total_uni     = int(month_wod["universe"].sum())
    total_billed  = int(month_wod["billed"].sum())
    total_unbilled = total_uni - total_billed
    overall_wod   = total_billed / total_uni * 100 if total_uni else 0.0

    section_header(
        "Width & Depth of Distribution",
        f"Detail month: {sel_label}",
    )
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("Total Universe", f"{total_uni:,}")
    with k2:
        st.metric("Billed Outlets", f"{total_billed:,}")
    with k3:
        st.metric(
            "Overall WOD %",
            f"{overall_wod:.1f}%",
            delta=f"{overall_wod - target_wod:+.1f}% vs target",
            delta_color="normal",
        )
    with k4:
        st.metric("Unbilled Outlets", f"{total_unbilled:,}")

    if unmapped_sm:
        st.caption(f"Not matched in DB: {', '.join(unmapped_sm)}")

    st.divider()

    # ── Section 1: WOD Trend ──────────────────────────────────────────────
    section_header("Width of Distribution — Monthly Trend")
    st.plotly_chart(
        _chart_wod_trend(wod_df, months_12, target_wod),
        use_container_width=True,
    )

    # ── Section 2: WOD Summary Table ─────────────────────────────────────
    st.markdown("---")
    section_header("WOD Summary", f"{sel_label} · sorted worst first")

    sum_df = wod_df[
        (wod_df["yr"] == sel_yr) & (wod_df["mo"] == sel_mo)
    ].copy().sort_values("wod_pct")

    if not sum_df.empty:
        sum_df["Target %"]   = target_wod
        sum_df["vs Target"]  = (sum_df["wod_pct"] - target_wod).round(1)
        disp_sum = sum_df[[
            "sm_key", "universe", "billed", "unbilled", "wod_pct", "Target %", "vs Target"
        ]].copy()
        disp_sum.columns = [
            "Salesman", "Universe", "Billed", "Unbilled", "WOD %", "Target %", "vs Target"
        ]
        disp_sum["WOD %"]     = disp_sum["WOD %"].apply(lambda x: f"{x:.1f}%")
        disp_sum["Target %"]  = disp_sum["Target %"].apply(lambda x: f"{x:.0f}%")
        disp_sum["vs Target"] = disp_sum["vs Target"].apply(lambda x: f"{x:+.1f}%")
        st.dataframe(disp_sum, use_container_width=True, hide_index=True)
    else:
        st.info("No data for selected month.")

    # ── Section 3: DOD Metrics ────────────────────────────────────────────
    st.markdown("---")
    section_header(
        "Depth of Distribution — Avg per Billed Outlet",
        f"{sel_label} vs {_month_label(prev_yr, prev_mo)}",
    )

    def _dod_metrics(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["billed_safe"] = out["billed"].clip(lower=1)
        out["rev_out"]     = out["revenue"] / out["billed_safe"]
        out["cas_out"]     = out["cases"]   / out["billed_safe"]
        out["inv_out"]     = out["invoices"]/ out["billed_safe"]
        return out[["sm_key", "rev_out", "cas_out", "inv_out"]]

    cur_dod  = _dod_metrics(
        wod_df[(wod_df["yr"] == sel_yr)  & (wod_df["mo"] == sel_mo)]
    )
    prev_dod = _dod_metrics(
        wod_df[(wod_df["yr"] == prev_yr) & (wod_df["mo"] == prev_mo)]
    )

    if not cur_dod.empty:
        dod = cur_dod.merge(
            prev_dod.rename(columns={"rev_out": "prev_rev", "cas_out": "prev_cas",
                                     "inv_out": "prev_inv"}),
            on="sm_key", how="left",
        )
        dod["delta_rev"] = dod["rev_out"] - dod["prev_rev"].fillna(0)
        dod["delta_cas"] = dod["cas_out"] - dod["prev_cas"].fillna(0)

        disp_dod = dod[["sm_key","rev_out","cas_out","inv_out","delta_rev","delta_cas"]].copy()
        disp_dod.columns = [
            "Salesman", "Avg Rev/Outlet", "Avg Cases/Outlet",
            "Avg Inv/Outlet", "Δ Rev/Outlet", "Δ Cases/Outlet",
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
            unbilled_df = _unbilled_outlets(
                billing, parties, db_map, sel_month, focus_sm
            )
        if unbilled_df.empty:
            st.success(f"All universe outlets were billed in {sel_label}!")
        else:
            uni_size = len(_filter_universe(parties, focus_sm))
            disp_ub = unbilled_df[
                ["PartyName", "Channel", "LastBilled", "DaysSince"]
            ].copy()
            disp_ub["DaysSince"] = disp_ub["DaysSince"].apply(
                lambda x: "—" if x == 9999 else str(x)
            )
            disp_ub.columns = ["Party", "Channel", "Last Billed", "Days Since Last Bill"]
            st.dataframe(disp_ub, use_container_width=True, hide_index=True)
            st.caption(
                f"{len(disp_ub)} unbilled out of {uni_size} universe outlets "
                f"({len(disp_ub)/uni_size*100:.1f}% not covered)"
            )

    # ── Section 5: Outlet-level Detail (expander) ─────────────────────────
    st.markdown("---")
    with st.expander(
        f"Outlet Detail — {focus_sm or '—'} · {sel_label}",
        expanded=False,
    ):
        if focus_sm:
            det_df = _outlet_detail(billing, parties, db_map, sel_month, focus_sm)
            if not det_df.empty:
                display_det = det_df.copy()
                display_det["Revenue"]         = display_det["Revenue"].apply(format_inr)
                display_det["Avg Order Value"] = display_det["Avg Order Value"].apply(format_inr)
                st.dataframe(display_det, use_container_width=True, hide_index=True)
            else:
                st.info(f"No billed outlets for {focus_sm} in {sel_label}.")
        else:
            st.info("Select a salesman to see outlet detail.")


render()
