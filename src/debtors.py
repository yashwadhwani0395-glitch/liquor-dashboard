"""src/debtors.py — Debtors Ageing page.

Methodology
-----------
The ERP has NO bill-linkage column. TrVocDetail is a Tally-style
ledger: each row is a single Dr or Cr against an AccHeadID and
(optionally) a PartyID, with no Ref/Bill/Invoice column. Receipts
are entered "on account" against the party — not against a specific
sales invoice.

So per-bill ageing is computed by **FIFO**:
  1. Pull every TrVocDetail row for the party from non-cancelled vouchers.
  2. Sort DR rows (sales) by VoucherDate ascending.
  3. Apply the total CR pool (receipts) to the oldest DR rows first.
  4. The residual Remaining on each DR row = unpaid portion of that bill.
  5. Age = today - bill date; buckets 0-30 / 31-60 / 61-90 / 90+.

Credit-days from MsPartyMaster.CreditDays drives the "overdue" flag
(age beyond credit terms).

Confirmation that FIFO is correct here: the script `_discover_debtors.py`
inspected receipt voucher TT=29 #32 — `PrevVocNo`, `TranstypeIDMS`,
`VoucherNoMS`, `ParentDocDet` and both narration fields are all blank.
Confirmed Tally on-account behavior.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import format_inr, safe_section


# Same SALES_TYPES list used across modules — kept consistent with
# src/sales_plan.py L36 so the "sales" DR side here matches what
# the Sales pages aggregate elsewhere.
SALES_TYPES: tuple[int, ...] = (
    18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53,
)


# ═══════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_party_ledger(min_outstanding: float = 0.0) -> pd.DataFrame:
    """Return every TrVocDetail row for debtors (PartyID LIKE 'D%') joined
    with VoucherDate, TransTypeName, and the party's CreditDays/PartyName.

    Filters out cancelled vouchers. One row per ledger entry — caller
    aggregates and runs FIFO.
    """
    sql = """
        SELECT
            d.PartyID,
            p.PartyName,
            ISNULL(p.CreditDays, 0)                 AS CreditDays,
            ISNULL(p.LicenseTypeID, '')             AS LicenseTypeID,
            d.TransTypeID,
            t.TransTypeName,
            d.VoucherNo,
            CAST(h.VoucherDate AS date)             AS VoucherDate,
            d.DrCrIndicator,
            CAST(d.Amount AS float)                 AS Amount
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID
                          AND h.VoucherNo   = d.VoucherNo
        JOIN MsTransType t ON t.TransTypeID = d.TransTypeID
        JOIN MsPartyMaster p ON p.PartyID   = d.PartyID
        WHERE d.PartyID LIKE 'D%'
          AND h.Cancelled = 'N'
    """
    df = run_query(sql)
    if df.empty:
        raise RuntimeError(
            "Empty party ledger — likely transient DB error. "
            "Click 🔄 Refresh in the header to retry."
        )
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
    return df


# ═══════════════════════════════════════════════════════════════════════════
# FIFO AGEING
# ═══════════════════════════════════════════════════════════════════════════

def _fifo_age_party(party_rows: pd.DataFrame,
                    today: pd.Timestamp) -> pd.DataFrame:
    """Apply FIFO knock-off for a single party's ledger.

    Returns a DataFrame of unpaid DR rows enriched with Remaining + AgeDays
    + Bucket. Empty DataFrame if the party is fully paid.
    """
    drs = party_rows[party_rows["DrCrIndicator"] == "D"].copy()
    crs = party_rows[party_rows["DrCrIndicator"] == "C"]
    if drs.empty:
        return drs.assign(Remaining=0.0, AgeDays=0, Bucket="")

    drs = drs.sort_values(["VoucherDate", "VoucherNo"]).reset_index(drop=True)
    drs["Remaining"] = drs["Amount"].astype(float)

    cr_pool = float(crs["Amount"].sum())
    for i in range(len(drs)):
        if cr_pool <= 0:
            break
        applied = min(drs.at[i, "Remaining"], cr_pool)
        drs.at[i, "Remaining"] -= applied
        cr_pool -= applied

    unpaid = drs[drs["Remaining"] > 0.5].copy()
    if unpaid.empty:
        return unpaid

    unpaid["AgeDays"] = (today - unpaid["VoucherDate"]).dt.days.astype(int)
    unpaid["Bucket"] = pd.cut(
        unpaid["AgeDays"],
        bins=[-1, 30, 60, 90, 1_000_000],
        labels=["0-30", "31-60", "61-90", "90+"],
    )
    return unpaid


def _build_party_summary(ledger: pd.DataFrame,
                         today: pd.Timestamp) -> pd.DataFrame:
    """Per-party row with Outstanding, ageing-bucket sub-totals, oldest age,
    credit-days, and an overdue flag. Sorted by Outstanding desc.
    """
    bucket_names = ["0-30", "31-60", "61-90", "90+"]
    out_rows: list[dict] = []
    for (pid, pname), party_rows in ledger.groupby(
        ["PartyID", "PartyName"], sort=False
    ):
        unpaid = _fifo_age_party(party_rows, today)
        outstanding = float(unpaid["Remaining"].sum()) if not unpaid.empty else 0.0
        if outstanding <= 0.5:
            continue  # skip parties with no outstanding

        bucket_totals: dict[str, float] = {b: 0.0 for b in bucket_names}
        if not unpaid.empty:
            grouped = unpaid.groupby("Bucket", observed=True)["Remaining"].sum()
            for b, v in grouped.items():
                bucket_totals[str(b)] = float(v)

        oldest = int(unpaid["AgeDays"].max()) if not unpaid.empty else 0
        bills_count = int(len(unpaid))
        cd = int(party_rows["CreditDays"].iloc[0])

        out_rows.append({
            "PartyID":      pid,
            "PartyName":    pname,
            "Outstanding": outstanding,
            "0-30":        bucket_totals["0-30"],
            "31-60":       bucket_totals["31-60"],
            "61-90":       bucket_totals["61-90"],
            "90+":         bucket_totals["90+"],
            "Bills":       bills_count,
            "OldestAge":   oldest,
            "CreditDays":  cd,
            "OverdueBy":   max(0, oldest - cd) if cd > 0 else oldest,
        })

    if not out_rows:
        return pd.DataFrame()
    df = pd.DataFrame(out_rows).sort_values("Outstanding", ascending=False)
    return df


# ═══════════════════════════════════════════════════════════════════════════
# UI SECTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _kpi_row(summary: pd.DataFrame) -> None:
    total_out = float(summary["Outstanding"].sum())
    total_bills = int(summary["Bills"].sum())
    parties = int(len(summary))
    over_90 = float(summary["90+"].sum())
    over_60 = float(summary["61-90"].sum()) + over_90
    overdue_pct = (over_60 / total_out * 100) if total_out else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total outstanding", format_inr(total_out))
    c2.metric("Parties with dues", f"{parties:,}",
              f"{total_bills:,} unpaid bills")
    c3.metric("Over 60 days", format_inr(over_60),
              f"{overdue_pct:.1f}% of total",
              delta_color="inverse")
    c4.metric("Over 90 days", format_inr(over_90),
              "Critical",
              delta_color="inverse")


def _bucket_chart(summary: pd.DataFrame) -> None:
    bucket_totals = {
        "0-30 days":   float(summary["0-30"].sum()),
        "31-60 days":  float(summary["31-60"].sum()),
        "61-90 days":  float(summary["61-90"].sum()),
        "90+ days":    float(summary["90+"].sum()),
    }
    bdf = pd.DataFrame({
        "Bucket": list(bucket_totals.keys()),
        "Outstanding": list(bucket_totals.values()),
    })
    st.bar_chart(bdf.set_index("Bucket"), color="#1B4F72")


def _party_table(summary: pd.DataFrame) -> None:
    df = summary.copy()
    df["Status"] = df.apply(
        lambda r: "🔴 Overdue" if r["OverdueBy"] > 0 else "✅ Within terms",
        axis=1,
    )
    cols = ["PartyID", "PartyName", "Outstanding",
            "0-30", "31-60", "61-90", "90+",
            "Bills", "OldestAge", "CreditDays", "OverdueBy", "Status"]
    df = df[cols]

    fmt_money = "{:,.0f}"

    def _bucket_style(v: float) -> str:
        try: n = float(v)
        except (TypeError, ValueError): return ""
        if n <= 0: return "color:#9ca3af"
        return ""

    def _overdue_style(v) -> str:
        try: n = int(v)
        except (TypeError, ValueError): return ""
        if n > 30:  return "color:#dc2626; font-weight:600"
        if n > 0:   return "color:#b45309; font-weight:600"
        return "color:#16a34a"

    styled = (
        df.style
        .format({
            "Outstanding": fmt_money,
            "0-30":        fmt_money,
            "31-60":       fmt_money,
            "61-90":       fmt_money,
            "90+":         fmt_money,
            "Bills":       "{:,}",
            "OldestAge":   "{:,}",
            "CreditDays":  "{:,}",
            "OverdueBy":   "{:,}",
        })
        .map(_bucket_style, subset=["0-30", "31-60", "61-90", "90+"])
        .map(_overdue_style, subset=["OverdueBy"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=520)


def _party_drilldown(ledger: pd.DataFrame, summary: pd.DataFrame,
                     today: pd.Timestamp) -> None:
    st.markdown("---")
    st.subheader("Party drill-down — bill-wise breakup")
    if summary.empty:
        st.info("No party data."); return

    options = (
        summary["PartyID"] + " — " + summary["PartyName"]
        + " (₹" + summary["Outstanding"].map(lambda v: f"{v:,.0f}") + ")"
    ).tolist()
    pick = st.selectbox("Pick a party", options, index=0, key="dbt_party_pick")
    if not pick:
        return
    pid = pick.split(" — ", 1)[0]
    party_rows = ledger[ledger["PartyID"] == pid]
    unpaid = _fifo_age_party(party_rows, today)
    if unpaid.empty:
        st.info("Party has no outstanding dues."); return

    # Bucket sub-totals as captions
    sub = (unpaid.groupby("Bucket", observed=True)["Remaining"]
           .agg(["sum", "count"])
           .rename(columns={"sum": "Outstanding", "count": "Bills"}))
    if not sub.empty:
        cap_parts = []
        for bucket in ["0-30", "31-60", "61-90", "90+"]:
            if bucket in sub.index:
                row = sub.loc[bucket]
                cap_parts.append(
                    f"**{bucket}**: ₹{row['Outstanding']:,.0f} "
                    f"({int(row['Bills'])} bills)"
                )
        if cap_parts:
            st.caption("  ·  ".join(cap_parts))

    # Bill-level table — oldest first
    disp = unpaid.sort_values(["AgeDays"], ascending=False)[
        ["VoucherDate", "TransTypeName", "VoucherNo",
         "Amount", "Remaining", "AgeDays", "Bucket"]
    ].rename(columns={
        "VoucherDate":   "Bill Date",
        "TransTypeName": "Bill Type",
        "VoucherNo":     "Bill #",
        "Amount":        "Bill Amount",
        "Remaining":     "Unpaid",
        "AgeDays":       "Age (days)",
    })
    disp["Bill Date"] = pd.to_datetime(disp["Bill Date"]).dt.strftime("%d %b %Y")

    styled = (
        disp.style
        .format({
            "Bill Amount": "{:,.0f}",
            "Unpaid":      "{:,.0f}",
            "Age (days)":  "{:,}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=420)

    # Download
    buf = disp.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download bill-wise ageing CSV",
        buf,
        file_name=f"ageing_{pid}_{today.date()}.csv",
        mime="text/csv",
    )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("📊 Debtors Ageing")
    st.caption(
        "FIFO bill-wise outstanding by age bucket. Methodology: receipts "
        "are applied to the oldest unpaid sales bill first (Tally-style "
        "on-account) since the ERP carries no per-bill linkage. Credit "
        "days are read from `MsPartyMaster.CreditDays` and drive the "
        "overdue flag."
    )

    with st.spinner("Loading party ledger…"):
        ledger = _load_party_ledger()

    today = pd.Timestamp.today().normalize()
    with st.spinner("Computing FIFO ageing…"):
        summary = _build_party_summary(ledger, today)

    if summary.empty:
        st.success("No outstanding debtor balances. 🎉"); return

    safe_section("KPI row",       _kpi_row, summary)
    st.divider()
    st.subheader("Outstanding by ageing bucket")
    safe_section("Bucket chart",  _bucket_chart, summary)
    st.divider()
    st.subheader(f"Party-wise outstanding · {len(summary):,} parties")
    safe_section("Party table",   _party_table, summary)
    safe_section("Party drill-down", _party_drilldown,
                 ledger, summary, today)
