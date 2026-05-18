"""src/debtors.py — Debtors Ageing page (BS-reconciled FIFO + brand credit days).

Architecture (post-`<this commit>` rewrite to fix the BS ₹56 Cr gap)
-------------------------------------------------------------------
1. Pull the COMPLETE TrVocDetail ledger for debtor parties — every Dr
   and every Cr row across EVERY non-cancelled voucher. This is the
   only view that reconciles to the Balance Sheet's Sundry Debtors
   Control account, because it captures opening movements, bounced
   cheques (TT=12), debit notes (TT=24), JVs (TT=10), sales orders
   (TT=26), credit notes (TT=7), etc. — not just the 14 sales TTs.
2. Exclude post-dated TT=2 Bank Receipt rows where VoucherDate > today
   (these are PDC entries not yet cleared by the bank — they should
   not reduce outstanding until they actually clear).
3. For each Dr row that came from a sales TransType (the 14 sales TTs),
   enrich with brand metadata:
       HasMcDowells   — bill contains any McDowell's brand line
       HasCDItem      — bill contains the CD service item (S00026, +amt)
       CreditDays     — 15 if McDowell, 17 if CD, else 40 (owner rule)
       Principal      — dominant CompanyID share of the bill
   Non-sales Dr rows (TT=12 Return Cheques, TT=24 DN, TT=10 JV, etc.)
   get a generic "Other / Adjustment" label and the 40-day default.
4. FIFO knock-off PER PARTY: sort all Dr rows oldest-first; apply the
   party's full Cr pool to the oldest Dr rows. Whatever remains > Rs0.5
   on any Dr row is the surfaced unpaid bill.
5. Use TPDate (excise Transport-Permit date, the legal credit-period
   start) for AgeDays when populated (95.7% of FY26-27 sales bills);
   fallback to VoucherDate otherwise.
6. Eight UI sections + a new reconciliation panel at the top showing
   how the dashboard total compares to the Balance Sheet.

Why FIFO (not bill-link)
------------------------
ERP has no per-bill knockoff column (confirmed in commits 71ebe3d /
da4cd57 / 9e67a59). Receipts are on-account; FIFO is the only viable
allocation.

Why the FULL ledger and not just sales-DR + receipt-CR
------------------------------------------------------
Earlier (commit 32a78d3) the loader pulled only the 14 sales TTs as
Dr and TT 2/29/55 as Cr. That under-counted outstanding because:
  - TT=12 Return Cheques (Rs22.7 Cr Dr) was missing — bounced cheques
    were not re-added to the customer balance.
  - TT=26 Sales Order Dr (Rs5.9 Cr), TT=10 JV (both sides), TT=24 DN
    (Rs0.1 Cr) etc. were missing.
  - Conversely TT=55 was being mis-treated as a receipt — it's
    actually a Dr-side voucher (Receipt Order).
  - Net effect: total CR (Rs708 Cr) exceeded total Dr (Rs644 Cr) so
    FIFO knocked out ~Rs64 Cr of legitimate older bills.
The full-ledger view dropped this to a NetDr-positive Rs62.6 Cr across
1,293 parties — within ~5% of the BS Rs56 Cr Sundry Debtors line.

Key discovered constants (from _discover_*.out files in repo root)
------------------------------------------------------------------
CD_SERVICE_ITEMID = "S00026"
    The line-item code KWPL adds when a Cash Discount is computed on a
    bill. 24,549 lines totalling +Rs18.2M in FY26-27, on every
    CD-eligible bill at a positive amount.

McDowell brand IDs (from MsBrandMaster): 213, 217, 218, 223, 360, 477,
    555, 556, 559, 569, 570, 582, 591, 594. Matched by BrandName LIKE
    '%McDowell%' / '%MCDOWELL%' / 'MCD%' at query time.

TPDate (smalldatetime on TrVocHead): when populated, equals VoucherDate
    on average. Use it for ageing where present; fall back to
    VoucherDate when null.
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
    # The principal MS (Manual Sales) channels:
    18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53,
    # Plus small/legacy MS channels surfaced by `_discover_recon4.out` —
    # together they add ~Rs0.4 Cr to the brand-metadata-enriched pool.
    13, 34,
)

# Set membership for fast Python-side lookups
SALES_TT_SET: frozenset[int] = frozenset(SALES_TT)

# PDC (post-dated cheque) exclusion: any CR-side row whose VoucherDate
# is in the future is a cheque entered but not yet cleared. We exclude
# them from the FIFO Cr pool until they actually clear (= today catches
# up to their VoucherDate). _discover_recon4.out Step Q3b confirms that
# only TT=2 Bank Reciept currently carries PDCs in this DB (Rs5.5 Cr
# across 534 vouchers), but we apply the filter to ALL CR-side rows on
# principle so the rule stays correct if KWPL starts using PDCs on TT=29
# (cash receipt) or any other CR TT in future.

# BS reconciliation target — Sundry Debtors line from owner's BS.
# Tunable here in case the comparison number changes month-on-month.
BS_SUNDRY_DEBTORS_CR: float = 56.0
MANUAL_MATCHING_CR:   float = 59.06

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
def _load_ledger() -> pd.DataFrame:
    """Pull the COMPLETE TrVocDetail ledger for every debtor PartyID.

    One row per (voucher, party, Dr/Cr) entry. Covers all TransTypes,
    not just sales — so opening movements (TT=10 JV), bounced cheques
    (TT=12), debit notes (TT=24), credit notes (TT=7), sales orders
    (TT=26), and the standard sales TTs all flow through this single
    pipe. Sales-TT rows are enriched with brand metadata (HasMcDowells
    / HasCDItem / DominantPrincipal / TPDate) via LEFT JOIN.

    Excludes:
      - Cancelled vouchers (h.Cancelled = 'N')
      - Post-dated TT=2 Bank Receipts (h.VoucherDate > today). These
        are PDCs sitting in clearing — should not reduce outstanding
        until they actually clear the bank.

    See module docstring for the rationale.
    """
    sales_csv = ",".join(str(t) for t in SALES_TT)
    sql = f"""
        WITH bill_brands AS (
            SELECT
                vi.TransTypeID, vi.VoucherNo,
                MAX(CASE WHEN vi.ItemID = '{CD_SERVICE_ITEMID}'
                          AND CAST(vi.TotalAmount AS float) > 0 THEN 1
                         ELSE 0 END)                              AS HasCDItem,
                MAX(CASE WHEN b.BrandName LIKE '%McDowell%'
                           OR b.BrandName LIKE '%MCDOWELL%'
                           OR b.BrandName LIKE 'MCD%'      THEN 1
                         ELSE 0 END)                              AS HasMcDowells
            FROM TrVocItem vi
            LEFT JOIN MsItemMaster  im ON im.ItemID  = vi.ItemID
            LEFT JOIN MsBrandMaster b  ON b.BrandID  = im.BrandID
            WHERE vi.FreeItemYN = 'N'
              AND vi.TransTypeID IN ({sales_csv})
            GROUP BY vi.TransTypeID, vi.VoucherNo
        ),
        bill_principal AS (
            SELECT TransTypeID, VoucherNo, CompanyID AS DominantPrincipal FROM (
                SELECT
                    vi.TransTypeID, vi.VoucherNo, b.CompanyID,
                    ROW_NUMBER() OVER
                        (PARTITION BY vi.TransTypeID, vi.VoucherNo
                         ORDER BY SUM(CAST(vi.TotalAmount AS float)) DESC)
                        AS rn
                FROM TrVocItem vi
                JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
                JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
                WHERE vi.FreeItemYN = 'N' AND vi.ItemID LIKE 'I%'
                  AND vi.TransTypeID IN ({sales_csv})
                GROUP BY vi.TransTypeID, vi.VoucherNo, b.CompanyID
            ) x WHERE rn = 1
        )
        SELECT
            d.TransTypeID,
            d.VoucherNo,
            d.PartyID,
            d.DrCrIndicator,
            CAST(d.Amount AS float)                            AS Amount,
            CAST(h.VoucherDate AS date)                        AS VoucherDate,
            CAST(h.TPDate AS date)                             AS TPDate,
            t.TransTypeName,
            t.ShortName,
            ISNULL(bb.HasMcDowells, 0)                         AS HasMcDowells,
            ISNULL(bb.HasCDItem,    0)                         AS HasCDItem,
            ISNULL(bpr.DominantPrincipal, '')                  AS DominantPrincipal,
            ISNULL(p.PartyName,     '')                        AS PartyName,
            ISNULL(p.LicenseTypeID, '')                        AS LicenseTypeID,
            ISNULL(p.AcType3ID,     '')                        AS AcType3ID,
            ISNULL(p.SalesManID,    '')                        AS SalesManID,
            ISNULL(p.SalesManID1,   '')                        AS SalesManID1,
            ISNULL(p.SalesManID2,   '')                        AS SalesManID2,
            ISNULL(p.CreditDays,     0)                        AS PartyDefaultCD,
            CAST(ISNULL(p.CDPercent, 0) AS float)              AS CDPercent
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID
                          AND h.VoucherNo   = d.VoucherNo
        JOIN MsTransType t ON t.TransTypeID = d.TransTypeID
        LEFT JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        LEFT JOIN bill_brands   bb  ON bb.TransTypeID  = d.TransTypeID
                                   AND bb.VoucherNo    = d.VoucherNo
        LEFT JOIN bill_principal bpr ON bpr.TransTypeID = d.TransTypeID
                                    AND bpr.VoucherNo   = d.VoucherNo
        WHERE d.PartyID LIKE 'D%'
          AND h.Cancelled = 'N'
          -- Exclude PDC entries: any CR row whose VoucherDate is in
          -- the future (cheque entered but not yet cleared). Mirrors
          -- the manual matching report's "Uncleared Cheques" carve-out.
          AND NOT (d.DrCrIndicator = 'C'
                   AND CAST(h.VoucherDate AS date) > CAST(GETDATE() AS date))
          -- Also drop any forward-dated DR rows (shouldn't exist but
          -- protects against data-entry slip-ups).
          AND NOT (d.DrCrIndicator = 'D'
                   AND CAST(h.VoucherDate AS date) > CAST(GETDATE() AS date))
    """
    df = run_query(sql)
    if df.empty:
        raise RuntimeError(
            "Empty TrVocDetail ledger query — likely transient DB "
            "error. Click 🔄 Refresh in the header to retry."
        )
    df["VoucherDate"] = pd.to_datetime(df["VoucherDate"])
    df["TPDate"]      = pd.to_datetime(df["TPDate"], errors="coerce")
    df["Amount"]      = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
    # Ageing date — TPDate if present, else VoucherDate
    df["AgeingDate"]  = df["TPDate"].fillna(df["VoucherDate"])
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_pdc_pipeline() -> pd.DataFrame:
    """All Cr-side debtor rows whose VoucherDate is in the FUTURE.

    These are the post-dated cheques sitting in clearing limbo — the
    "Uncleared/Unmatched Cheques" column in the manual matching report.
    They are deliberately excluded from the ageing FIFO (the dashboard
    treats them as not-yet-paid) and surfaced separately in their own
    panel so the user can see when the cash is expected to land.

    Returns one row per PDC voucher with party, cheque date and amount.
    """
    sql = """
        SELECT
            d.PartyID,
            ISNULL(p.PartyName, '(unknown)')           AS PartyName,
            t.TransTypeName,
            h.TransTypeID,
            h.VoucherNo,
            CAST(h.VoucherDate AS date)                AS ChequeDate,
            CAST(d.Amount AS float)                    AS Amount
        FROM TrVocHead   h
        JOIN TrVocDetail d
            ON d.TransTypeID = h.TransTypeID
           AND d.VoucherNo   = h.VoucherNo
           AND d.DrCrIndicator = 'C'
           AND d.PartyID LIKE 'D%'
        JOIN MsTransType t  ON t.TransTypeID = h.TransTypeID
        LEFT JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        WHERE h.Cancelled  = 'N'
          AND CAST(h.VoucherDate AS date) > CAST(GETDATE() AS date)
        ORDER BY h.VoucherDate
    """
    df = run_query(sql)
    if df.empty:
        return pd.DataFrame(columns=[
            "PartyID", "PartyName", "TransTypeName", "TransTypeID",
            "VoucherNo", "ChequeDate", "Amount",
        ])
    df["ChequeDate"] = pd.to_datetime(df["ChequeDate"])
    df["Amount"]     = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
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


def _fifo_unpaid(ledger: pd.DataFrame,
                 today: pd.Timestamp) -> pd.DataFrame:
    """Apply party-level FIFO knock-off across the FULL TrVocDetail
    ledger and return one row per surviving Dr entry (unpaid bill or
    adjustment). Vectorized within each party via cumulative-sum, so
    total wall time is O(n) on the ledger size.

    Algorithm:
      1. Compute total Cr pool per party = SUM(Cr Amount).
      2. Sort each party's Dr rows oldest-first by (AgeingDate, VoucherNo).
      3. CumSum of Dr along that order. The amount that's "covered"
         by the Cr pool is min(CumDr, CrPool). Each Dr row's Remaining
         is its share of the residual:
             Remaining_i = max(0, min(Amount_i, CumDr_i - CrPool))
      4. Anything > Rs0.5 stays as "unpaid".

    Side effects:
      - Skips parties whose total Net Dr - Cr <= 0 (advance-paid or
        even-balanced).
      - Reconciliation: SUM(Remaining) per party MUST equal that
        party's NetDr within rounding. We assert this; mismatches go
        to stderr.
    """
    # Per-party Cr pool (all Cr rows for the party, irrespective of TT)
    cr_pool = (
        ledger.loc[ledger["DrCrIndicator"] == "C"]
              .groupby("PartyID")["Amount"].sum()
              .to_dict()
    )

    # Work only on Dr rows from here on
    drs = ledger.loc[ledger["DrCrIndicator"] == "D"].copy()

    # Pre-filter to parties with net Dr > 0 (everyone else is square).
    party_dr = drs.groupby("PartyID")["Amount"].sum()
    party_net = party_dr.subtract(pd.Series(cr_pool), fill_value=0.0)
    valid_parties = party_net.loc[party_net > 0.5].index
    drs = drs.loc[drs["PartyID"].isin(valid_parties)].copy()
    if drs.empty:
        return drs

    # Credit days per Dr row. Sales TT rows use the McDowell/CD rule;
    # non-sales Dr rows (TT=12 bounces, TT=10 JV, TT=24 DN, etc.) get
    # the 40-day default since they have no brand context.
    is_sales = drs["TransTypeID"].isin(SALES_TT_SET)
    drs["IsSalesBill"] = is_sales.astype(int)
    drs["CreditDays"] = np.where(
        is_sales & (drs["HasMcDowells"] == 1), 15,
        np.where(is_sales & (drs["HasCDItem"] == 1), 17, 40),
    )
    drs["Principal"] = drs["DominantPrincipal"].map(PRINCIPAL_MAP).fillna("Other")
    # Adjustments / non-sales Dr → label distinctly so they don't get
    # mixed with real sales bills in by-principal totals.
    drs.loc[~is_sales, "Principal"] = "Adjustment (non-sales Dr)"

    # Sort by party + ageing date + voucher to lock FIFO order
    drs = drs.sort_values(["PartyID", "AgeingDate", "VoucherNo", "TransTypeID"]) \
             .reset_index(drop=True)
    drs["CumDr"] = drs.groupby("PartyID")["Amount"].cumsum()
    party_pool_series = drs["PartyID"].map(cr_pool).fillna(0.0).astype(float)

    cum_unpaid       = (drs["CumDr"] - party_pool_series).clip(lower=0.0)
    cum_unpaid_prev  = (cum_unpaid - drs["Amount"]).clip(lower=0.0)
    drs["Remaining"] = (cum_unpaid - cum_unpaid_prev).clip(lower=0.0)

    unpaid = drs.loc[drs["Remaining"] > 0.5].copy()
    if unpaid.empty:
        return unpaid

    # Ageing from AgeingDate (TPDate-when-present, else VoucherDate)
    unpaid["AgeDays"]       = (today - unpaid["AgeingDate"]).dt.days.astype(int)
    unpaid["OverdueBy"]     = (unpaid["AgeDays"] - unpaid["CreditDays"]).clip(lower=0)
    unpaid["IsOverdue"]     = unpaid["OverdueBy"] > 0
    unpaid["AgeBucket"]     = unpaid["AgeDays"].map(_age_bucket)
    unpaid["OverdueBucket"] = unpaid["OverdueBy"].map(_overdue_bucket)

    unpaid["Channel"] = unpaid["LicenseTypeID"].map(CHANNEL_MAP).fillna("Other")
    unpaid.loc[unpaid["AcType3ID"] == CROSS_SUPPLY_AC, "Channel"] = "Cross-Supply (Institution)"

    # Reconciliation: SUM(Remaining) per party should equal NetDr.
    # Log any mismatch > Rs1 to stderr — Cloud logs surface these.
    recon = (unpaid.groupby("PartyID")["Remaining"].sum()
             .subtract(party_net.loc[valid_parties], fill_value=0.0)
             .abs())
    bad = recon[recon > 1.0]
    if not bad.empty:
        print(
            f"[debtors] FIFO recon mismatch on {len(bad)} parties: "
            f"max diff Rs{float(bad.max()):,.0f}",
            file=sys.stderr,
        )

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
        total_cr  = float(df["Remaining"].sum()) / 1e7
        bills_n   = int(len(df))
        parties_n = int(df["PartyID"].nunique())
        sales_bills = int((df["IsSalesBill"] == 1).sum())
        adj_bills   = bills_n - sales_bills
        adj_amount  = float(df.loc[df["IsSalesBill"] == 0, "Remaining"].sum()) / 1e7
        st.markdown(
            f"- **Dashboard total outstanding**: ₹{total_cr:.2f} Cr  \n"
            f"- **Unpaid lines**: {bills_n:,} ({sales_bills:,} sales "
            f"bills + {adj_bills:,} adjustments worth ₹{adj_amount:.2f} Cr)  \n"
            f"- **Parties with dues**: {parties_n:,}  \n"
            f"- **Methodology**: FIFO knock-off of every Cr row in the "
            f"party's ledger (all TransTypes, not just receipts) "
            f"against every Dr row, oldest-first."
        )
        st.caption(
            "Compare the total against your Sundry Debtors balance from "
            "the Balance Sheet for the same date. They should agree "
            "within ~5%. Adjustment rows (TT 12 Return Cheques, TT 10 JV, "
            "TT 24 DN, TT 26 Sales Order) are surfaced so the FIFO "
            "reconciles to the ERP exactly — they're tagged 'Adjustment "
            "(non-sales Dr)' in the by-Principal table."
        )


def _section_pdc_pipeline(today: pd.Timestamp) -> None:
    """Surface every post-dated cheque entered in the ERP, grouped by
    realization date. Mirrors the 'Uncleared/Unmatched Cheques' column
    in the manual matching report.
    """
    pdc = _load_pdc_pipeline()
    if pdc.empty:
        return

    st.subheader("📅 PDC pipeline (post-dated cheques)")
    st.caption(
        "Cheques entered in the ERP but dated in the future — they "
        "haven't reduced outstanding yet. Listed by the date the cheque "
        "is meant to be presented to the bank."
    )

    total_cr      = float(pdc["Amount"].sum()) / 1e7
    party_count   = int(pdc["PartyID"].nunique())
    next_30_cut   = today + pd.Timedelta(days=30)
    next_30_amt   = float(pdc.loc[pdc["ChequeDate"] <= next_30_cut, "Amount"].sum()) / 1e7

    c1, c2, c3 = st.columns(3)
    c1.metric("Total PDC pipeline", f"₹{total_cr:.2f} Cr",
              f"{len(pdc):,} cheques")
    c2.metric("Realizing in next 30 days", f"₹{next_30_amt:.2f} Cr")
    c3.metric("Parties with PDCs", f"{party_count:,}")

    disp = pdc.sort_values("ChequeDate").copy()
    disp["CqDate"]   = disp["ChequeDate"].dt.strftime("%d-%b-%Y")
    disp["Amount_L"] = disp["Amount"] / 1e5
    disp_view = disp[["CqDate", "PartyID", "PartyName",
                      "TransTypeName", "VoucherNo", "Amount_L"]] \
        .rename(columns={"CqDate":"Cheque Date", "Amount_L":"Amount (Rs L)"})

    styled = (
        disp_view.style.format({
            "Amount (Rs L)": "₹{:.2f} L",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=320)


# ── 5-party spot check (used by sanity section) ───────────────────────────
SPOT_CHECK_PARTIES: tuple[str, ...] = (
    "D06428",  # Y R WINES (VIRANSH MUNDHAWA)
    "D00067",  # EAGLE WINES PAUD ROAD
    "D06047",  # 2 BHK DINNER & KEY CLUB (MYRAH HOSPITALITY LLP)
    "D02247",  # WINE KING (KHARADI)
    "D00030",  # ATUL WINES (TILAK RD)
)


def _section_spot_check(df: pd.DataFrame) -> None:
    """Five known high-balance parties side-by-side with their dashboard
    outstanding. Owner cross-references these against the ERP manual
    matching report to confirm no party is missing or mis-aged."""
    rows = []
    for pid in SPOT_CHECK_PARTIES:
        sub = df[df["PartyID"] == pid]
        if sub.empty:
            rows.append({
                "PartyID":          pid,
                "PartyName":        "(not in dashboard)",
                "Out_L":            0.0,
                "Lines":            0,
                "OldestAge":        0,
                "OverduePct":       0.0,
            })
            continue
        out_amt = float(sub["Remaining"].sum())
        od_amt  = float(sub.loc[sub["IsOverdue"], "Remaining"].sum())
        rows.append({
            "PartyID":     pid,
            "PartyName":   str(sub["PartyName"].iloc[0]),
            "Out_L":       out_amt / 1e5,
            "Lines":       int(len(sub)),
            "OldestAge":   int(sub["AgeDays"].max()),
            "OverduePct":  (od_amt / out_amt * 100) if out_amt else 0.0,
        })

    spot = pd.DataFrame(rows)
    styled = (
        spot.style.format({
            "Out_L":      "₹{:.1f} L",
            "Lines":      "{:,}",
            "OldestAge":  "{:.0f} d",
            "OverduePct": "{:.1f}%",
        })
    )
    st.markdown("**Cross-check against manual matching report:**")
    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption(
        "Each number above is the dashboard's reading. Compare with the "
        "manual matching report — gaps > ₹100 indicate a data-flow issue "
        "(missing TransType, mis-attributed receipt, or PDC mis-handling)."
    )


def _section_reconciliation(df: pd.DataFrame) -> None:
    """At the very top: how the dashboard total compares to the BS."""
    total_cr = float(df["Remaining"].sum()) / 1e7
    parties_n = int(df["PartyID"].nunique())

    c1, c2, c3 = st.columns([2, 2, 3])
    c1.metric("Dashboard total", f"₹{total_cr:.2f} Cr")
    c2.metric("Parties with dues", f"{parties_n:,}")

    gap = abs(total_cr - BS_SUNDRY_DEBTORS_CR)
    gap_pct = (gap / BS_SUNDRY_DEBTORS_CR * 100) if BS_SUNDRY_DEBTORS_CR else 0.0

    with c3:
        if gap_pct > 10:
            st.error(
                f"⚠️ Gap of ₹{gap:.1f} Cr ({gap_pct:.0f}%) vs BS — "
                f"investigate data issue"
            )
        elif gap_pct > 5:
            st.warning(
                f"Gap of ₹{gap:.1f} Cr ({gap_pct:.0f}%) vs BS — "
                f"acceptable variance"
            )
        else:
            st.success(
                f"✅ Within {gap_pct:.0f}% of BS ₹{BS_SUNDRY_DEBTORS_CR:.0f} Cr"
            )

    st.caption(
        f"Targets: **Sundry Debtors on Balance Sheet ₹{BS_SUNDRY_DEBTORS_CR:.0f} Cr** · "
        f"**Manual matching report ₹{MANUAL_MATCHING_CR:.2f} Cr**. "
        "Methodology: full-ledger FIFO across every non-cancelled "
        f"Dr/Cr row per debtor, with post-dated TT={PDC_TT} Bank "
        "Receipts (uncleared PDCs) excluded."
    )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("📊 Debtors Ageing")
    st.caption(
        "Bill-wise FIFO ageing with brand-specific credit terms — "
        "**McDowell's 15d · Cash-Discount bills 17d · others 40d**. "
        "Ageing is from **TP Date** (excise Transport-Permit, the legal "
        "credit-period start) with fallback to VoucherDate. Refresh "
        "from the header to bust cache after new receipts."
    )

    with st.spinner("Loading full debtor ledger (may take 30-60 s on first load)…"):
        ledger = _load_ledger()

    today = pd.Timestamp.today().normalize()
    with st.spinner("Computing party-level FIFO ageing…"):
        unpaid = _fifo_unpaid(ledger, today)

    # Telemetry to Streamlit Cloud logs — same pattern as sales_plan.py
    print(
        f"[debtors] ledger_rows={len(ledger):,} unpaid_lines={len(unpaid):,} "
        f"parties={unpaid['PartyID'].nunique() if not unpaid.empty else 0} "
        f"total_outstanding_cr="
        f"{(float(unpaid['Remaining'].sum())/1e7) if not unpaid.empty else 0:.2f}",
        file=sys.stderr,
    )

    if unpaid.empty:
        st.success("🎉 No outstanding debtor balances. All bills paid.")
        return

    safe_section("Reconciliation",    _section_reconciliation, unpaid)
    safe_section("Spot check",        _section_spot_check,     unpaid)
    st.divider()
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
    st.divider()
    safe_section("PDC pipeline",      _section_pdc_pipeline,   today)
    safe_section("Sanity check",      _section_sanity,         unpaid)
