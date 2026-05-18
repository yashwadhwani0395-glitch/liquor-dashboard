"""src/debtors.py — Debtors Ageing page (bill-wise FIFO + brand credit days).

Architecture
------------
1. Pull every non-cancelled sales bill in TrVocHead/TrVocItem/TrVocDetail
   joined with the debtor's MsPartyMaster row.
2. For each bill compute:
       HasMcDowells   — bill contains any McDowell's brand line
       HasCDItem      — bill contains the CD service item (S00026, +amt)
       CreditDays     — 15 if McDowell, 17 if CD, else 40 (owner rule)
       Principal      — by dominant CompanyID share of the bill
3. Pull every non-cancelled receipt (TT 2 Bank Reciept, 29 Cash, 55
   Receipt Order) per debtor; sum to a CR pool.
4. FIFO knock-off: apply each party's CR pool to its DR (sales) rows
   oldest-first. Anything remaining is unpaid → drives ageing.
5. Eight UI sections: hero KPIs, ageing bucket chart, by-Principal,
   by-Channel, by-Salesman collection efficiency, Party-risk action
   list, renegotiation candidates, and a sanity-check footer.

Why FIFO (not bill-link)
------------------------
ERP has no per-bill knockoff column (confirmed in commits 71ebe3d /
da4cd57 / 9e67a59). Receipts are on-account; FIFO is the only viable
allocation.

Key discovered constants (from _discover_cd_item.out)
-----------------------------------------------------
CD_SERVICE_ITEMID = "S00026"
    The line-item code used by KWPL when a Cash Discount is computed
    on a bill. 24,549 lines totalling +Rs18.2M in FY26-27 — appears on
    every CD-eligible bill at a positive amount.

McDowell brand IDs (from MsBrandMaster): 213, 217, 218, 223, 360, 477,
    555, 556, 559, 569, 570, 582, 591, 594. We match by BrandName
    pattern at query time for robustness against new brand IDs.
"""
from __future__ import annotations

import sys
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import safe_section

try:
    import plotly.express as px
    _HAS_PLOTLY = True
except Exception:
    _HAS_PLOTLY = False


# ── Constants (from ERP discovery) ──────────────────────────────────────────
CD_SERVICE_ITEMID = "S00026"

SALES_TT: tuple[int, ...] = (
    18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53,
)

# Bank Reciept (TT2) + Cash Receipt - SACHIN (TT29) + Receipt Order (TT55).
# These are the three non-cancelled inbound-cash TransTypes that hit a
# debtor PartyID on the credit side. Confirmed in _discover_debtors.out.
RECEIPT_TT: tuple[int, ...] = (2, 29, 55)

PRINCIPAL_MAP: dict[str, str] = {
    "C00025": "United Spirits",
    "C00039": "United Breweries",
    "C00040": "Diageo",
    "C00056": "Brown-Forman",
}

# License-type → channel label (matches src/sales_plan.py:_LT_LABEL).
CHANNEL_MAP: dict[str, str] = {
    "180001": "Wine Shop (FL-II)",
    "180002": "Permit Room (FL-III)",
    "180004": "Beer Shopee (FL-BR-II)",
    "180005": "Club (FL-IV)",
    "180007": "One-Day (FL-IV)",
}

# AcType3ID '130007' = Cross-Supply (special UBL bucket). Other AcType3
# values broadly classify the account but Cross-Supply is the one that
# meaningfully shifts the channel label.
CROSS_SUPPLY_AC = "130007"


# ── Per-principal salesman-field map. Each principal cares about a
#    different field in MsPartyMaster — the field that holds the *handler*
#    salesman for that principal's universe. Mirrors the SM-routing logic
#    in src/sales_plan.py / distribution.py.
SALESMAN_FIELD_BY_PRINCIPAL: dict[str, str] = {
    "United Spirits":   "SalesManID",   # SM1
    "Diageo":           "SalesManID1",  # SM2
    "United Breweries": "SalesManID2",  # SM3
    # Brown-Forman is split: wine shops via SM2, institutions via SM3.
    # Handled separately in _attribute_salesman() below.
    "Brown-Forman":     "__bf_split__",
}


# ═══════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_bills() -> pd.DataFrame:
    """One row per sales bill (TrVocHead level), with:
       - Debtor PartyID, PartyName, LicenseTypeID, AcType3ID, CreditDays
         (default), CDPercent, SalesManID, SalesManID1, SalesManID2
       - BillAmount (sum of Dr-side rows from TrVocDetail, the cleanest
         representation of what the customer owes for this voucher)
       - HasMcDowells (1/0), HasCDItem (1/0), DominantPrincipal CompanyID
    """
    type_ph = ",".join(str(t) for t in SALES_TT)
    sql = f"""
        WITH bill_party AS (
            -- One PartyID per voucher, picked as the debtor row with the
            -- largest DR amount (matches the pattern used in sales_plan).
            SELECT TransTypeID, VoucherNo, PartyID, BillAmount FROM (
                SELECT
                    d.TransTypeID, d.VoucherNo, d.PartyID,
                    SUM(CAST(d.Amount AS float)) OVER
                        (PARTITION BY d.TransTypeID, d.VoucherNo, d.PartyID)
                        AS BillAmount,
                    ROW_NUMBER() OVER
                        (PARTITION BY d.TransTypeID, d.VoucherNo
                         ORDER BY d.Amount DESC) AS rn
                FROM TrVocDetail d
                WHERE d.DrCrIndicator = 'D'
                  AND d.PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ),
        bill_brands AS (
            SELECT
                vi.TransTypeID, vi.VoucherNo,
                MAX(CASE WHEN vi.ItemID = '{CD_SERVICE_ITEMID}'
                          AND CAST(vi.TotalAmount AS float) > 0 THEN 1
                         ELSE 0 END)                                AS HasCDItem,
                MAX(CASE WHEN b.BrandName LIKE '%McDowell%'
                           OR b.BrandName LIKE '%MCDOWELL%'
                           OR b.BrandName LIKE 'MCD%'        THEN 1
                         ELSE 0 END)                                AS HasMcDowells
            FROM TrVocItem vi
            LEFT JOIN MsItemMaster  im ON im.ItemID  = vi.ItemID
            LEFT JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
            WHERE vi.FreeItemYN = 'N'
            GROUP BY vi.TransTypeID, vi.VoucherNo
        ),
        bill_principal AS (
            -- Dominant CompanyID by Rs share within the bill.
            SELECT TransTypeID, VoucherNo, CompanyID AS DominantPrincipal FROM (
                SELECT
                    vi.TransTypeID, vi.VoucherNo, b.CompanyID,
                    SUM(CAST(vi.TotalAmount AS float)) AS Amt,
                    ROW_NUMBER() OVER
                        (PARTITION BY vi.TransTypeID, vi.VoucherNo
                         ORDER BY SUM(CAST(vi.TotalAmount AS float)) DESC)
                        AS rn
                FROM TrVocItem vi
                JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
                JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
                WHERE vi.FreeItemYN = 'N' AND vi.ItemID LIKE 'I%'
                GROUP BY vi.TransTypeID, vi.VoucherNo, b.CompanyID
            ) x WHERE rn = 1
        )
        SELECT
            h.TransTypeID, h.VoucherNo,
            CAST(h.VoucherDate AS date)                AS VoucherDate,
            bp.PartyID,
            p.PartyName,
            ISNULL(p.LicenseTypeID, '')                AS LicenseTypeID,
            ISNULL(p.AcType3ID, '')                    AS AcType3ID,
            ISNULL(p.SalesManID, '')                   AS SalesManID,
            ISNULL(p.SalesManID1, '')                  AS SalesManID1,
            ISNULL(p.SalesManID2, '')                  AS SalesManID2,
            ISNULL(p.CreditDays, 0)                    AS PartyDefaultCD,
            CAST(ISNULL(p.CDPercent, 0) AS float)      AS CDPercent,
            CAST(bp.BillAmount AS float)               AS BillAmount,
            ISNULL(bb.HasMcDowells, 0)                 AS HasMcDowells,
            ISNULL(bb.HasCDItem,    0)                 AS HasCDItem,
            ISNULL(bpr.DominantPrincipal, '')          AS DominantPrincipal
        FROM TrVocHead h
        JOIN bill_party    bp  ON bp.TransTypeID  = h.TransTypeID
                              AND bp.VoucherNo    = h.VoucherNo
        JOIN MsPartyMaster p   ON p.PartyID       = bp.PartyID
        LEFT JOIN bill_brands  bb  ON bb.TransTypeID  = h.TransTypeID
                                  AND bb.VoucherNo    = h.VoucherNo
        LEFT JOIN bill_principal bpr ON bpr.TransTypeID = h.TransTypeID
                                    AND bpr.VoucherNo   = h.VoucherNo
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled    = 'N'
    """
    df = run_query(sql)
    if df.empty:
        raise RuntimeError(
            "Empty bills query — likely transient DB error. "
            "Click 🔄 Refresh in the header to retry."
        )
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["BillAmount"]  = pd.to_numeric(df["BillAmount"], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_receipts() -> pd.DataFrame:
    """Per-debtor receipt totals (Bank/Cash/Receipt Order, CR side)."""
    type_ph = ",".join(str(t) for t in RECEIPT_TT)
    sql = f"""
        SELECT
            d.PartyID,
            SUM(CAST(d.Amount AS float)) AS ReceiptAmount
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID
                          AND h.VoucherNo   = d.VoucherNo
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled    = 'N'
          AND d.DrCrIndicator = 'C'
          AND d.PartyID LIKE 'D%'
        GROUP BY d.PartyID
    """
    df = run_query(sql)
    if df.empty:
        # Receipts can legitimately be 0 if no payments ever — fall back
        # to empty DataFrame, callers handle it.
        return pd.DataFrame({"PartyID": [], "ReceiptAmount": []})
    df["ReceiptAmount"] = pd.to_numeric(df["ReceiptAmount"], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=86400, show_spinner=False)
def _load_salesman_master() -> pd.DataFrame:
    df = run_query("""
        SELECT
            ISNULL(SalesManID, '') AS SalesManID,
            ISNULL(FullName,
                   LTRIM(RTRIM(
                       ISNULL(FirstName,'') + ' ' + ISNULL(LastName,'')
                   ))) AS FullName
        FROM MsSalesmanMaster
    """)
    if df.empty:
        return pd.DataFrame({"SalesManID": [], "FullName": []})
    return df


# ═══════════════════════════════════════════════════════════════════════════
# CORE COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════

def _credit_days(row: pd.Series) -> int:
    """McDowell ⇒ 15, CD bill ⇒ 17, else ⇒ 40."""
    if int(row["HasMcDowells"]) == 1:
        return 15
    if int(row["HasCDItem"]) == 1:
        return 17
    return 40


def _age_bucket(age: int) -> str:
    if age <= 30:  return "0-30"
    if age <= 60:  return "31-60"
    if age <= 90:  return "61-90"
    return "90+"


def _overdue_bucket(overdue: int) -> str:
    if overdue <= 0:  return "Within terms"
    if overdue <= 30: return "Overdue 1-30d"
    if overdue <= 60: return "Overdue 31-60d"
    return "Overdue 60d+"


def _fifo_unpaid(bills: pd.DataFrame,
                 receipts: pd.DataFrame,
                 today: pd.Timestamp) -> pd.DataFrame:
    """Apply FIFO knock-off party-by-party and return ONE row per
    unpaid bill (or partially-paid). Vectorized within each party
    via cumulative-sum, so total wall time is O(n) on the bill set."""

    # Pre-compute credit days and principal label
    bills = bills.copy()
    bills["CreditDays"] = np.where(
        bills["HasMcDowells"] == 1, 15,
        np.where(bills["HasCDItem"] == 1, 17, 40),
    )
    bills["Principal"] = bills["DominantPrincipal"].map(PRINCIPAL_MAP).fillna("Other")

    # Index receipts by PartyID
    rec_pool = dict(zip(receipts["PartyID"], receipts["ReceiptAmount"]))

    # FIFO within each party (sort, cumsum, apply pool)
    bills = bills.sort_values(["PartyID", "VoucherDate", "VoucherNo"]).reset_index(drop=True)
    bills["CumDr"] = bills.groupby("PartyID")["BillAmount"].cumsum()

    # PoolApplied per row = the running CR cap from rec_pool
    party_pool = bills["PartyID"].map(rec_pool).fillna(0.0).astype(float)

    # Remaining for each bill = max(0, min(BillAmount, CumDr - pool))
    cum_unpaid    = (bills["CumDr"] - party_pool).clip(lower=0.0)
    cum_unpaid_prev = (cum_unpaid - bills["BillAmount"]).clip(lower=0.0)
    bills["Remaining"] = (cum_unpaid - cum_unpaid_prev).clip(lower=0.0)

    # Drop bills that are fully paid
    unpaid = bills[bills["Remaining"] > 0.5].copy()
    if unpaid.empty:
        return unpaid

    unpaid["AgeDays"]       = (today - unpaid["VoucherDate"]).dt.days.astype(int)
    unpaid["OverdueBy"]     = (unpaid["AgeDays"] - unpaid["CreditDays"]).clip(lower=0)
    unpaid["IsOverdue"]     = unpaid["OverdueBy"] > 0
    unpaid["AgeBucket"]     = unpaid["AgeDays"].map(_age_bucket)
    unpaid["OverdueBucket"] = unpaid["OverdueBy"].map(_overdue_bucket)

    # Channel label (Cross-Supply override beats license type)
    unpaid["Channel"] = unpaid["LicenseTypeID"].map(CHANNEL_MAP).fillna("Other")
    unpaid.loc[unpaid["AcType3ID"] == CROSS_SUPPLY_AC, "Channel"] = "Cross-Supply (Institution)"

    return unpaid.drop(columns=["CumDr"], errors="ignore")


def _attribute_salesman(row: pd.Series) -> str:
    """Per-principal salesman attribution. Brown-Forman splits by
    AcType3 (institution → SM3 / SalesManID2, else SM2 / SalesManID1)."""
    p = row["Principal"]
    if p == "Brown-Forman":
        return row["SalesManID2"] if row["AcType3ID"] == CROSS_SUPPLY_AC \
               else row["SalesManID1"]
    field = SALESMAN_FIELD_BY_PRINCIPAL.get(p)
    if not field or field == "__bf_split__":
        return ""
    return row.get(field, "") or ""


# ═══════════════════════════════════════════════════════════════════════════
# UI SECTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_inr_cr(v: float) -> str:
    return f"₹{v/1e7:.2f} Cr"


def _fmt_inr_l(v: float) -> str:
    return f"₹{v/1e5:.1f} L"


def _section_hero(df: pd.DataFrame) -> None:
    total       = float(df["Remaining"].sum())
    parties_cnt = int(df["PartyID"].nunique())
    overdue_amt = float(df.loc[df["IsOverdue"], "Remaining"].sum())
    overdue_pct = (overdue_amt / total * 100) if total else 0.0
    dso         = (df["AgeDays"] * df["Remaining"]).sum() / total if total else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Outstanding", _fmt_inr_cr(total))
    c2.metric("Parties with Dues", f"{parties_cnt:,}",
              f"{len(df):,} unpaid bills")
    c3.metric("Overdue Amount", _fmt_inr_cr(overdue_amt),
              f"{overdue_pct:.1f}% of total", delta_color="inverse")
    c4.metric("Avg Days Outstanding", f"{dso:.0f} d",
              "weighted by Rs")


def _section_ageing_buckets(df: pd.DataFrame) -> None:
    st.subheader("📊 Ageing distribution")
    bucket_summary = (
        df.groupby("AgeBucket", observed=True)
          .agg(Bills=("VoucherNo", "count"),
               Amount=("Remaining", "sum"),
               Parties=("PartyID", "nunique"))
          .reindex(["0-30", "31-60", "61-90", "90+"]).fillna(0)
          .reset_index()
    )
    bucket_summary["Amount_Cr"] = bucket_summary["Amount"] / 1e7

    if _HAS_PLOTLY:
        fig = px.bar(
            bucket_summary, x="AgeBucket", y="Amount_Cr",
            color="AgeBucket",
            color_discrete_map={
                "0-30":  "#16a34a",
                "31-60": "#fbbf24",
                "61-90": "#f97316",
                "90+":   "#dc2626",
            },
            text="Amount_Cr",
        )
        fig.update_traces(texttemplate="₹%{text:.2f} Cr",
                          textposition="outside", cliponaxis=False)
        fig.update_layout(template="plotly_white", showlegend=False,
                          xaxis_title="Bill age", yaxis_title="Outstanding (Rs Cr)",
                          height=320, margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.bar_chart(bucket_summary.set_index("AgeBucket")["Amount_Cr"])

    styled = (
        bucket_summary.style.format({
            "Bills":     "{:,.0f}",
            "Amount":    "₹{:,.0f}",
            "Amount_Cr": "₹{:.2f} Cr",
            "Parties":   "{:,.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _grouped_summary(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """Per-group: Outstanding, Bills, Parties, AvgAge, Overdue total + %."""
    grp = df.groupby(by, observed=True)
    out = grp.agg(
        Outstanding=("Remaining", "sum"),
        Bills=("VoucherNo", "count"),
        Parties=("PartyID", "nunique"),
        AvgAge=("AgeDays", "mean"),
    )
    # Overdue subtotals via boolean masking
    od_mask = df["IsOverdue"]
    od_sum = (df[od_mask].groupby(by, observed=True)["Remaining"].sum()
              .reindex(out.index).fillna(0.0))
    out["OverdueAmt"] = od_sum
    out["OverduePct"] = (out["OverdueAmt"] / out["Outstanding"] * 100).fillna(0.0)
    out["Outstanding_Cr"] = out["Outstanding"] / 1e7
    return out.sort_values("Outstanding", ascending=False).reset_index()


def _section_by_principal(df: pd.DataFrame) -> None:
    st.subheader("🏷️ Outstanding by principal")
    by_p = _grouped_summary(df, "Principal")
    disp = by_p[["Principal", "Outstanding_Cr", "Bills",
                 "Parties", "AvgAge", "OverduePct"]]
    styled = (
        disp.style.format({
            "Outstanding_Cr": "₹{:.2f} Cr",
            "AvgAge":         "{:.0f} d",
            "OverduePct":     "{:.1f}%",
            "Bills":          "{:,.0f}",
            "Parties":        "{:,.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_by_channel(df: pd.DataFrame) -> None:
    st.subheader("🏪 Outstanding by channel")
    by_c = _grouped_summary(df, "Channel")
    disp = by_c[["Channel", "Outstanding_Cr", "Bills",
                 "Parties", "AvgAge", "OverduePct"]]
    styled = (
        disp.style.format({
            "Outstanding_Cr": "₹{:.2f} Cr",
            "AvgAge":         "{:.0f} d",
            "OverduePct":     "{:.1f}%",
            "Bills":          "{:,.0f}",
            "Parties":        "{:,.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_by_salesman(df: pd.DataFrame) -> None:
    st.subheader("👥 Salesman collection efficiency")
    st.caption(
        "Outstanding mapped via the per-principal handler-salesman field "
        "(SM1 for USL, SM2 for Diageo, SM3 for UBL; BF wine-shops via SM2 "
        "and BF Cross-Supply institutions via SM3). Unmapped bills are "
        "rolled into 'Unmapped'."
    )

    sm_master = _load_salesman_master()
    sm_lookup = dict(zip(sm_master["SalesManID"], sm_master["FullName"]))

    work = df.copy()
    work["SalesmanID"] = work.apply(_attribute_salesman, axis=1)
    work["Salesman"]   = work["SalesmanID"].map(sm_lookup).fillna("")
    work.loc[work["Salesman"].str.strip() == "", "Salesman"] = "Unmapped"

    grouped = work.groupby(["Salesman", "Principal"], observed=True)
    by_sm = grouped.agg(
        Outstanding=("Remaining", "sum"),
        AvgAge=("AgeDays", "mean"),
        Parties=("PartyID", "nunique"),
    )
    od_sum = (work[work["IsOverdue"]]
              .groupby(["Salesman", "Principal"], observed=True)["Remaining"].sum()
              .reindex(by_sm.index).fillna(0.0))
    by_sm["OverdueAmt"] = od_sum
    by_sm["Outstanding_Cr"] = by_sm["Outstanding"] / 1e7
    by_sm["CollectionEff%"] = (
        100 - (by_sm["OverdueAmt"] / by_sm["Outstanding"] * 100)
    ).fillna(0.0).round(1)
    by_sm = by_sm.reset_index().sort_values("Outstanding", ascending=False)

    disp = by_sm[["Salesman", "Principal", "Outstanding_Cr",
                  "Parties", "AvgAge", "CollectionEff%"]]
    styled = (
        disp.style.format({
            "Outstanding_Cr": "₹{:.2f} Cr",
            "AvgAge":         "{:.0f}",
            "CollectionEff%": "{:.1f}%",
            "Parties":        "{:,.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=420)


def _section_party_risk(df: pd.DataFrame) -> None:
    st.subheader("🚨 Party risk — action list")
    st.caption(
        "Who to chase, who to renegotiate, who to hold supply for. "
        "Status logic: oldest bill > 90d → Hold Supply; oldest > 60d "
        "AND overdue % > 50% → Renegotiate; overdue % > 25% OR oldest > "
        "45d → Watch; else Good."
    )

    by_p = df.groupby(["PartyID", "PartyName"], observed=True).agg(
        Outstanding=("Remaining", "sum"),
        OldestBillAge=("AgeDays", "max"),
        AvgAge=("AgeDays", "mean"),
        BillCount=("VoucherNo", "count"),
    )
    od_sum = (df[df["IsOverdue"]]
              .groupby(["PartyID", "PartyName"], observed=True)["Remaining"].sum()
              .reindex(by_p.index).fillna(0.0))
    by_p["OverdueAmt"] = od_sum
    by_p["Outstanding_L"] = by_p["Outstanding"] / 1e5
    by_p["OverduePct"] = (by_p["OverdueAmt"] / by_p["Outstanding"] * 100).fillna(0.0)
    by_p = by_p.reset_index().sort_values("Outstanding", ascending=False)

    def _classify(r: pd.Series) -> str:
        if r["OldestBillAge"] > 90:
            return "⛔ Hold Supply"
        if r["OldestBillAge"] > 60 and r["OverduePct"] > 50:
            return "🔴 Renegotiate"
        if r["OverduePct"] > 25 or r["OldestBillAge"] > 45:
            return "🟡 Watch"
        return "🟢 Good"

    by_p["Status"] = by_p.apply(_classify, axis=1)

    f1, f2 = st.columns([2, 1])
    with f1:
        status_filter = st.multiselect(
            "Status filter",
            ["🟢 Good", "🟡 Watch", "🔴 Renegotiate", "⛔ Hold Supply"],
            default=["🔴 Renegotiate", "⛔ Hold Supply"],
            key="dbt_status_filter",
        )
    with f2:
        min_amount = st.number_input(
            "Min outstanding (₹ Lakhs)", min_value=0.0, value=1.0, step=0.5,
            key="dbt_min_amount",
        )

    filtered = by_p[
        (by_p["Status"].isin(status_filter)) &
        (by_p["Outstanding_L"] >= float(min_amount))
    ]

    disp = filtered[[
        "Status", "PartyID", "PartyName", "Outstanding_L",
        "OldestBillAge", "AvgAge", "BillCount", "OverduePct",
    ]]
    styled = (
        disp.style.format({
            "Outstanding_L": "₹{:.1f} L",
            "OldestBillAge": "{:.0f} d",
            "AvgAge":        "{:.0f} d",
            "BillCount":     "{:,.0f}",
            "OverduePct":    "{:.1f}%",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=480)

    csv = disp.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download action list (CSV)", csv,
        f"debtors_action_{date.today()}.csv", "text/csv",
        key="dbt_action_dl",
    )


def _section_renegotiate(df: pd.DataFrame) -> None:
    st.subheader("🔄 Renegotiation candidates")
    st.caption(
        "Parties whose actual payment behavior (weighted-avg bill age) "
        "exceeds their stored MsPartyMaster.CreditDays by >20 days "
        "AND who have >₹1L outstanding. These are the formal terms "
        "your accountant should update — or stop credit."
    )

    by_p = df.groupby(
        ["PartyID", "PartyName", "PartyDefaultCD"], observed=True,
    ).agg(
        AvgAge=("AgeDays", "mean"),
        AvgOverdue=("OverdueBy", "mean"),
        Outstanding=("Remaining", "sum"),
    ).reset_index()

    by_p["GapVsTerms"] = (by_p["AvgAge"] - by_p["PartyDefaultCD"]).round(0)
    cand = by_p[(by_p["GapVsTerms"] > 20)
                & (by_p["Outstanding"] > 100_000)].copy()
    cand["Outstanding_L"] = cand["Outstanding"] / 1e5
    cand = cand.sort_values("GapVsTerms", ascending=False).head(30)

    if cand.empty:
        st.info("No renegotiation candidates — everyone is paying within ~20d of terms.")
        return

    cand["Recommendation"] = cand.apply(
        lambda r: f"Update from {int(r['PartyDefaultCD'])}d → "
                  f"{int(r['AvgAge'])}d, or pull credit",
        axis=1,
    )
    disp = cand[[
        "PartyID", "PartyName", "PartyDefaultCD", "AvgAge", "GapVsTerms",
        "Outstanding_L", "Recommendation",
    ]]
    styled = (
        disp.style.format({
            "PartyDefaultCD": "{:.0f} d",
            "AvgAge":         "{:.0f} d",
            "GapVsTerms":     "+{:.0f} d",
            "Outstanding_L":  "₹{:.1f} L",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _section_sanity(df: pd.DataFrame) -> None:
    with st.expander("🔍 Sanity check vs Balance Sheet", expanded=False):
        total_cr = float(df["Remaining"].sum()) / 1e7
        bills_n  = len(df)
        parties_n = df["PartyID"].nunique()
        st.markdown(
            f"- **Dashboard total outstanding**: ₹{total_cr:.2f} Cr  \n"
            f"- **Unpaid bill lines**: {bills_n:,}  \n"
            f"- **Parties with dues**: {parties_n:,}  \n"
            f"- **Methodology**: FIFO knock-off of receipts (TT 2/29/55) "
            f"against sales DR rows oldest-first."
        )
        st.caption(
            "Compare the total against your Sundry Debtors balance from "
            "the Balance Sheet for the same date. They should agree "
            "within ~5% — larger gaps indicate (a) cancelled vouchers "
            "that were never re-keyed, (b) opening-balance imports that "
            "predate the FY range, or (c) JV/Credit-Note movements we "
            "don't currently include in the FIFO pool."
        )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("📊 Debtors Ageing")
    st.caption(
        "Bill-wise FIFO ageing with brand-specific credit terms — "
        "**McDowell's 15d · Cash-Discount bills 17d · others 40d**. "
        "Refresh from the header to bust cache after new receipts."
    )

    with st.spinner("Loading bills + receipts (may take 30-60 s on first load)…"):
        bills    = _load_bills()
        receipts = _load_receipts()

    today = pd.Timestamp.today().normalize()
    with st.spinner("Computing FIFO ageing…"):
        unpaid = _fifo_unpaid(bills, receipts, today)

    # Telemetry to Streamlit Cloud logs — same pattern as sales_plan.py
    print(
        f"[debtors] bills={len(bills):,} receipts_parties={len(receipts):,} "
        f"unpaid_lines={len(unpaid):,} "
        f"total_outstanding_cr={float(unpaid['Remaining'].sum())/1e7:.2f}",
        file=sys.stderr,
    )

    if unpaid.empty:
        st.success("🎉 No outstanding debtor balances. All bills paid.")
        return

    safe_section("Hero KPIs",         _section_hero,           unpaid)
    st.divider()
    safe_section("Ageing buckets",    _section_ageing_buckets, unpaid)
    st.divider()
    safe_section("By principal",      _section_by_principal,   unpaid)
    safe_section("By channel",        _section_by_channel,     unpaid)
    st.divider()
    safe_section("By salesman",       _section_by_salesman,    unpaid)
    st.divider()
    safe_section("Party risk",        _section_party_risk,     unpaid)
    st.divider()
    safe_section("Renegotiation",     _section_renegotiate,    unpaid)
    safe_section("Sanity check",      _section_sanity,         unpaid)
