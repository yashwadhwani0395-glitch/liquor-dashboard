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

# Trader Association ban-reason decoder. AssoBan_Type is a 4-char code
# on MsPartyMaster (BannedByAssoc='Y'). See _discover_debtors_v2.out
# for the distribution: 571 OS6M / 28 LKDN / 18 CLDN / 1 Norm = 618
# total banned debtors.
BAN_REASON_MAP: dict[str, str] = {
    "OS6M": "Outstanding > 6 months",
    "LKDN": "Pre-LockDown legacy",
    "CLDN": "Closed down",
    "Norm": "Trade norms violation",
}

# Ghosted threshold (days without a sales bill).
GHOST_DAYS: int = 90

PRINCIPAL_MAP: dict[str, str] = {
    "C00025": "United Spirits",
    "C00039": "United Breweries",
    "C00040": "Diageo",
    "C00056": "Brown-Forman",
}

# License-type → channel label. Extended from src/sales_plan.py:_LT_LABEL
# with the codes that came back in _diag_channel.out (180003 / 180006 /
# 180008 / 180009 were missing previously, hence the "Other" pile-up).
CHANNEL_MAP: dict[str, str] = {
    "180001": "Wine Shop (FL-II)",
    "180002": "Permit Room (FL-III)",
    "180003": "Club / Restaurant (FL-III)",
    "180004": "Beer Shopee (FL-BR-II)",
    "180005": "Club (FL-IV)",
    "180006": "Country Liquor (FL-IV-A)",
    "180007": "One-Day Permit (FL-IV)",
    "180008": "Wholesale / Trading",
    "180009": "Importer / Bonded",
}

# AcType3ID '130007' = Cross-Supply ROUTING flag (used by sales_plan to
# split UBL's sub-targets). It is NOT a channel — same physical outlet
# can be wine shop + cross-supply simultaneously. Previously the
# by-channel section was overriding the channel label with "Cross-Supply
# (Institution)" which created a phantom channel; now retained only for
# reference in sales-side modules but not used in Debtors channel
# categorization.
CROSS_SUPPLY_AC = "130007"

# Placeholder salesman names in MsSalesmanMaster that are NOT real
# field humans (current as of team transition May 2026):
#   SURESH NAIR    (1 / 0 / 558 in SM1/SM2/SM3) — catch-all SM3 dump
#   CROSS SUPPLY   (311 / 0 / 7) — Cross-supply marker, not a person
#   CLOSED OUTLET  — status marker
#   ONE DAY LIC    — status marker
#   Z * (multiple) — ex-employees (Z RAJENDRA PRASAD, etc.)
# Note: ROHIT LAKHAN moved OUT of this set in May 2026 — he was
# reactivated as a KW Institution salesman (SM ID 000038, replacing
# Deepak Pangare who left). He's now in KNOWN_FIELD_SALESMEN.
PLACEHOLDER_NAMES: frozenset[str] = frozenset({
    "SURESH NAIR", "CROSS SUPPLY",
    "CLOSED OUTLET", "ONE DAY LIC",
    # Trailing-period variants seen in sample data
    "CLOSED OUTLET.", "ONE DAY LIC.",
})


def _is_placeholder_salesman(name: str | None) -> bool:
    """True if the salesman name is a placeholder, not a real human.
    Captures the PLACEHOLDER_NAMES set plus the Z-prefix legacy
    convention (ex-employees prefixed with 'Z ')."""
    if name is None:
        return True
    up = str(name).strip().upper()
    if not up:
        return True
    if up in PLACEHOLDER_NAMES:
        return True
    if up.startswith("Z "):
        return True
    return False


# Per-principal salesman field — STRICT principal-driven rule that
# mirrors how sales attribution works in sales_plan.py. No fallback
# chain: if the designated field is empty, the bill lands in
# 'Unmapped' (visible to the accountant for clean-up) rather than
# being absorbed by an unrelated placeholder name.
PRINCIPAL_TO_SM_FIELD: dict[str, str] = {
    "United Spirits":   "SalesManID",    # SM1
    "Diageo":           "SalesManID1",   # SM2
    "United Breweries": "SalesManID2",   # SM3
    # Brown-Forman is handled separately (split by AcType3) in
    # _attribute_salesman below.
}


# ── Per-principal salesman-field PRIORITY order ──────────────────────────
#    Each principal has a designated "primary" SM field for its handler
#    universe — but party records in the ERP are messy: some Diageo
#    institutions have SM1 populated (Miran Dmello routes them) but SM2
#    blank, etc. So we try the principal's preferred field first, then
#    fall back to SM1 / SM2 / SM3 in order. Empty / null are skipped.
# ── Known field-salesman whitelist ──────────────────────────────────────
# Real humans who physically visit outlets. Discovery on every name in
# MsSalesmanMaster (see _diag_full_sm.out) classified each:
#   - 17 names are field salesmen (this whitelist)
#   - 5 are placeholders (in PLACEHOLDER_NAMES)
#   - 8 are Z-prefix ex-employees
#   - 2 are unclassified (RAJESH, ROHIT) — kept OUT of whitelist
#     pending owner confirmation (see commit message).
#
# Names match `MsSalesmanMaster.FullName` exactly after strip().upper()
# — including DB quirks like "ABID" (not "AABID") and "RAHUL GONE" (the
# spelling used in the ERP, instead of "RAHUL GHONE"). Both variants
# are included so either matches.
#
# Used by the smart-cascade attribution in _attribute_salesman: a bill
# walks the principal's preferred SM-field order and stops at the
# FIRST field whose SalesManID resolves to a known field salesman.
# Empty / placeholder / Z-legacy IDs are skipped, so blank SM2 for a
# Diageo institution party correctly cascades to the SM1 if that SM1
# is a field salesman (e.g. MIRAN DMELLO for Permit Rooms).
KNOWN_FIELD_SALESMEN: frozenset[str] = frozenset({
    # ── Wine Shops (FL-II) ─────────────────────────────────────────────
    "SHASHANK", "SACHIN KAMBLE",           # USL Wine Shops (SM1)
    "AJAY", "DEEPAK PATIL",                # Diageo + BF Wine Shops (SM2)

    # ── Permit Rooms (FL-III) — handle both USL and Diageo ────────────
    "TULSIRAM", "SAURABH", "MIRAN DMELLO",
    "PRASHANT THORAT", "ATISH",            # 5 people post-transition
                                           # (Rohit Lakhan moved to KW Inst)

    # ── UBL KW Beer (FL-BR-II) ─────────────────────────────────────────
    "ABID", "AABID", "OMKAR PAWAR",        # both ABID spellings, just in case

    # ── KW Institution (UBL + BF, NOT Diageo) ─────────────────────────
    # Team transition May 2026: Anand Raj + Deepak Pangare LEFT;
    # ROHIT LAKHAN reactivated and RAHUL GHONE promoted from PCMC.
    "SHASHANK DESAI", "PRANAV",
    "RAHUL GHONE", "RAHUL GONE",           # ERP has the "GONE" spelling
    "ROHIT LAKHAN",                        # reactivated May 2026, KW Inst SM3

    # ── PCMC Institution ──────────────────────────────────────────────
    "GAJENDRA DAS", "AMOL SATHE",
    "PIYUSH ARORA",                        # new hire May 2026, covers
                                           # PCMC-west Permit Room belt
                                           # (Hinjewadi/Wakad/Mulshi)
})


@st.cache_data(ttl=86400, show_spinner=False)
def _field_salesman_ids() -> frozenset[str]:
    """Cached: the set of SalesManIDs whose FullName is in
    KNOWN_FIELD_SALESMEN. Built once per session (24h TTL) so
    per-row attribution stays fast."""
    df = _load_salesman_master()
    if df.empty:
        return frozenset()
    df = df.copy()
    df["NameUpper"] = df["FullName"].astype(str).str.strip().str.upper()
    keep = df["NameUpper"].isin(KNOWN_FIELD_SALESMEN)
    return frozenset(df.loc[keep, "SalesManID"].astype(str).str.strip())


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
            CAST(ISNULL(p.CDPercent, 0) AS float)              AS CDPercent,
            ISNULL(p.BannedByAssoc,       '')                  AS BannedByAssoc,
            ISNULL(p.AssoBan_Type,        '')                  AS AssoBan_Type,
            ISNULL(p.AssoBan_Description, '')                  AS AssoBan_Description,
            ISNULL(p.AssoBilling_YN,      '')                  AS AssoBilling_YN
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
def _load_cheque_returns_per_party() -> pd.DataFrame:
    """Per-party cheque-return summary (TT=12 Return Cheques).

    Returns columns:
        PartyID, ReturnCount, TotalReturnedAmt, LastReturnDate,
        DaysSinceLastReturn
    Confirmed via _discover_debtors_v2.out Q6: 317 parties with
    cumulative Rs22.73 Cr in cheque returns historically.
    """
    sql = """
        SELECT
            d.PartyID,
            COUNT(DISTINCT CONCAT(h.TransTypeID, '-', h.VoucherNo))
                                                AS ReturnCount,
            SUM(CAST(d.Amount AS float))         AS TotalReturnedAmt,
            CAST(MAX(h.VoucherDate) AS date)     AS LastReturnDate,
            DATEDIFF(DAY, MAX(h.VoucherDate),
                     CAST(GETDATE() AS date))    AS DaysSinceLastReturn
        FROM TrVocHead   h
        JOIN TrVocDetail d
            ON d.TransTypeID = h.TransTypeID
           AND d.VoucherNo   = h.VoucherNo
           AND d.DrCrIndicator = 'D'
           AND d.PartyID LIKE 'D%'
        WHERE h.TransTypeID = 12
          AND h.Cancelled   = 'N'
        GROUP BY d.PartyID
    """
    df = run_query(sql)
    if df.empty:
        return pd.DataFrame(columns=[
            "PartyID","ReturnCount","TotalReturnedAmt",
            "LastReturnDate","DaysSinceLastReturn",
        ])
    df["TotalReturnedAmt"]    = pd.to_numeric(df["TotalReturnedAmt"], errors="coerce").fillna(0.0)
    df["LastReturnDate"]      = pd.to_datetime(df["LastReturnDate"], errors="coerce")
    df["DaysSinceLastReturn"] = pd.to_numeric(df["DaysSinceLastReturn"], errors="coerce").fillna(0).astype(int)
    df["ReturnCount"]         = pd.to_numeric(df["ReturnCount"], errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_last_bill_per_party() -> pd.DataFrame:
    """Per-party LAST sales-bill date (TTs in SALES_TT only).

    Drives the "ghosted" detection (days since last bill >= 90 = ghosted).
    Cancelled vouchers excluded.
    """
    tt_csv = ",".join(str(t) for t in SALES_TT)
    sql = f"""
        SELECT
            d.PartyID,
            CAST(MAX(h.VoucherDate) AS date)  AS LastBillDate,
            DATEDIFF(DAY, MAX(h.VoucherDate),
                     CAST(GETDATE() AS date)) AS DaysSinceLastBill
        FROM TrVocHead   h
        JOIN TrVocDetail d
            ON d.TransTypeID = h.TransTypeID
           AND d.VoucherNo   = h.VoucherNo
           AND d.DrCrIndicator = 'D'
           AND d.PartyID LIKE 'D%'
        WHERE h.TransTypeID IN ({tt_csv})
          AND h.Cancelled   = 'N'
        GROUP BY d.PartyID
    """
    df = run_query(sql)
    if df.empty:
        return pd.DataFrame(columns=["PartyID","LastBillDate","DaysSinceLastBill"])
    df["LastBillDate"]     = pd.to_datetime(df["LastBillDate"], errors="coerce")
    df["DaysSinceLastBill"] = pd.to_numeric(df["DaysSinceLastBill"], errors="coerce").fillna(0).astype(int)
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

    # Channel = pure LicenseTypeID lookup. The previous code was
    # overriding to "Cross-Supply (Institution)" when AcType3='130007',
    # but Cross-Supply is a ROUTING flag (UBL sub-distribution path),
    # not a channel — the same physical outlet can be wine shop +
    # cross-supply simultaneously. Now Cross-Supply is reflected in
    # the salesman attribution (BF / UBL split) but does NOT pollute
    # the channel categorisation.
    unpaid["Channel"] = unpaid["LicenseTypeID"].map(CHANNEL_MAP).fillna("Other")

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


def _attribute_salesman(row: pd.Series,
                        field_sm_ids: frozenset[str] | None = None) -> str:
    """Per-principal **smart cascade** attribution.

    For each bill we walk the SM-field priority order designated for
    the bill's principal and return the FIRST field whose SalesManID
    resolves to a known field salesman. Placeholders, Z-legacy IDs
    and blanks are skipped — they're transparent to the cascade.

    Rationale: the strict rule (only check the designated field)
    leaves many bills 'Unmapped' because party-master records often
    have only one of the three SM fields populated. The fallback
    chain (try every field in any order) used to absorb parties into
    SURESH NAIR (the SM3 catch-all). The smart cascade combines the
    two: keep the principal-driven field order, but reject anything
    that's not a field salesman so the cascade naturally skips
    placeholders.

    Field priority by principal:
      USL bill (Principal='United Spirits')       → SM1 → SM2 → SM3
      Diageo bill                                  → SM2 → SM1 → SM3
      UBL bill                                     → SM3 → SM1 → SM2
      BF wine shop (AcType3 != 130007)             → SM2 → SM1 → SM3
      BF Cross-Supply / institution                → SM3 → SM2 → SM1
      Adjustment / Other / Non-sales row           → SM1 → SM2 → SM3

    `field_sm_ids` is the set of SalesManIDs whose FullName is in
    KNOWN_FIELD_SALESMEN. Passed in by the caller (cached via
    `_field_salesman_ids()`) so per-row apply doesn't re-build it.
    If omitted, the function falls back to looking it up — slower
    but safe for standalone calls.

    Returns the SalesManID of the first field salesman found, or ''
    (caller maps '' → 'Unmapped').
    """
    if field_sm_ids is None:
        field_sm_ids = _field_salesman_ids()

    p = row.get("Principal", "")
    if p == "Brown-Forman":
        ac3 = (row.get("AcType3ID") or "")
        order = (("SalesManID2", "SalesManID1", "SalesManID")
                 if ac3 == CROSS_SUPPLY_AC
                 else ("SalesManID1", "SalesManID", "SalesManID2"))
    elif p == "Diageo":
        order = ("SalesManID1", "SalesManID", "SalesManID2")
    elif p == "United Spirits":
        order = ("SalesManID",  "SalesManID1", "SalesManID2")
    elif p == "United Breweries":
        order = ("SalesManID2", "SalesManID",  "SalesManID1")
    else:
        order = ("SalesManID",  "SalesManID1", "SalesManID2")

    for col in order:
        sid = row.get(col) or ""
        if isinstance(sid, str):
            sid = sid.strip()
        if sid and sid in field_sm_ids:
            return sid
    return ""


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
        "Outstanding mapped via the **strict per-principal SM field** "
        "(USL: SM1; Diageo: SM2; UBL: SM3; BF: SM2 wine / SM3 Cross-"
        "Supply) — same rule as sales attribution in sales_plan.py. "
        "Placeholder names (SURESH NAIR / CROSS SUPPLY / CLOSED OUTLET "
        "/ ONE DAY LIC / Z-prefix ex-employees) are hidden by default."
    )

    sm_master    = _load_salesman_master()
    sm_lookup    = dict(zip(sm_master["SalesManID"], sm_master["FullName"]))
    field_sm_ids = _field_salesman_ids()  # pre-built once for speed

    work = df.copy()
    work["SalesmanID"] = work.apply(
        lambda r: _attribute_salesman(r, field_sm_ids), axis=1,
    )
    work["Salesman"]   = work["SalesmanID"].map(sm_lookup).fillna("")
    work.loc[work["Salesman"].str.strip() == "", "Salesman"] = "Unmapped"

    # Bucket every salesman name into one of three roles
    def _role(name: str) -> str:
        n = (name or "").strip().upper()
        if n == "UNMAPPED" or not n:
            return "Unmapped"
        if _is_placeholder_salesman(name):
            return "Placeholder"
        return "Field"
    work["SmRole"] = work["Salesman"].map(_role)

    # Role-filter radio — defaults to FIELD salesmen so placeholders
    # don't pollute the operational view.
    view = st.radio(
        "Show",
        ["Field salesmen only", "All names in ERP", "Placeholders / Unmapped only"],
        index=0,
        horizontal=True,
        key="dbt_sm_role",
        help=(
            "Field = real humans visiting outlets (Aabid, Ajay, Miran, …)\n"
            "Placeholder = ex-employees (Z-prefix), CROSS SUPPLY, "
            "SURESH NAIR-style SM3 catch-alls, status markers like "
            "CLOSED OUTLET / ONE DAY LIC. Their outstanding is "
            "REAL, but the names don't represent collectable handlers.\n"
            "Unmapped = bills whose party's designated SM field is blank."
        ),
    )
    if view == "Field salesmen only":
        view_df = work[work["SmRole"] == "Field"]
    elif view == "Placeholders / Unmapped only":
        view_df = work[work["SmRole"].isin(["Unmapped", "Placeholder"])]
    else:
        view_df = work

    if view_df.empty:
        st.info("No rows for the selected view."); return

    grouped = view_df.groupby(["Salesman", "Principal"], observed=True)
    by_sm = grouped.agg(
        Outstanding=("Remaining", "sum"),
        AvgAge=("AgeDays", "mean"),
        Parties=("PartyID", "nunique"),
    )
    od_sum = (view_df[view_df["IsOverdue"]]
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


def _section_unmatched_followup() -> None:
    """Daily worklist: parties whose receipts > bills in the last
    `window_days`, with a ratio + absolute-Rs floor to filter out
    normal AR pay-downs. Mirrors the manual matching report's
    "Uncleared / Unmatched Cheques" column (Rs6.84 Cr target).

    Tunable knobs are exposed in the UI so the operator can dial
    looser/tighter without a code change.
    """
    st.subheader("💰 Unmatched Receipts — Sales-Team Follow-up")
    st.caption(
        "Parties who have paid in the last 6 months but the receipts "
        "couldn't be cleanly matched to specific bills (lump sums, "
        "over-payments, mis-allocated cheques). **Sales team calls "
        "these parties** to confirm which bill the payment is meant "
        "for. Different from the PDC Pipeline section above, which "
        "is purely the post-dated-cheque schedule."
    )

    # ── Filter knobs ────────────────────────────────────────────────
    f1, f2, f3 = st.columns([1, 1, 1])
    with f1:
        window_days = st.selectbox(
            "Window (days)", [90, 180, 270, 365],
            index=1, key="dbt_unm_window",
        )
    with f2:
        ratio_floor = st.slider(
            "Min ratio (Receipts ÷ Bills)",
            min_value=1.00, max_value=2.00, value=1.20, step=0.05,
            key="dbt_unm_ratio",
            help="Tighten to drop parties whose receipts only "
                 "slightly exceed their bills (normal AR pay-down).",
        )
    with f3:
        min_excess_l = st.number_input(
            "Min excess (₹ Lakhs)", min_value=0.0, value=1.0, step=0.5,
            key="dbt_unm_min",
        )

    df = _load_unmatched_receipts(window_days=int(window_days))
    if df.empty:
        st.success(f"🎉 No unmatched receipts in last {window_days} days.")
        return

    # Apply tuning floors
    work = df[
        (df["Bills"] > 0)
        & (df["Receipts"] >= df["Bills"] * float(ratio_floor))
        & (df["Excess"] >= float(min_excess_l) * 1e5)
    ].copy()

    if work.empty:
        st.info("Filters too tight — relax ratio or min-excess to surface candidates.")
        return

    total_cr   = float(work["Excess"].sum()) / 1e7
    parties_n  = int(len(work))
    avg_l      = float(work["Excess"].mean()) / 1e5

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Unmatched", f"₹{total_cr:.2f} Cr",
              "Target ₹6.84 Cr (manual matching)")
    c2.metric("Parties to call", f"{parties_n:,}")
    c3.metric("Avg per party", f"₹{avg_l:.1f} L")
    c4.metric("Window", f"{window_days} d")

    # ── Salesman attribution (primary SM only) ──────────────────────
    sm_master = _load_salesman_master()
    sm_dict   = dict(zip(sm_master["SalesManID"], sm_master["FullName"]))

    def _primary_sm(row):
        for col in ("SalesManID", "SalesManID1", "SalesManID2"):
            sid = (row.get(col) or "").strip()
            if sid:
                return sid
        return ""

    work["SalesmanID"] = work.apply(_primary_sm, axis=1)
    work["Salesman"]   = work["SalesmanID"].map(sm_dict).fillna("(unmapped)")
    work.loc[work["Salesman"].str.strip() == "", "Salesman"] = "(unmapped)"

    work["Bills_L"]    = work["Bills"]    / 1e5
    work["Receipts_L"] = work["Receipts"] / 1e5
    work["Excess_L"]   = work["Excess"]   / 1e5
    work["Ratio"]      = (work["Receipts"] / work["Bills"]).round(2)

    # ── Follow-up worklist table ───────────────────────────────────
    st.markdown("**Follow-up worklist** (sorted by excess desc)")
    disp = work[[
        "PartyID", "PartyName", "Salesman",
        "Bills_L", "Receipts_L", "Excess_L", "Ratio",
    ]].rename(columns={
        "Bills_L":    f"Bills L{window_days}d (Rs L)",
        "Receipts_L": f"Receipts L{window_days}d (Rs L)",
        "Excess_L":   "Excess Receipt (Rs L)",
    })
    styled = (
        disp.style.format({
            f"Bills L{window_days}d (Rs L)":     "₹{:,.1f} L",
            f"Receipts L{window_days}d (Rs L)":  "₹{:,.1f} L",
            "Excess Receipt (Rs L)":             "₹{:,.2f} L",
            "Ratio":                             "{:.2f}x",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=460)

    # ── Salesman workload table ─────────────────────────────────────
    st.markdown("**Workload by salesman** — who has how many parties to call")
    by_sm = (
        work.groupby("Salesman", observed=True)
            .agg(PartiesToCall=("PartyID", "nunique"),
                 TotalExcess=("Excess", "sum"))
            .reset_index()
            .sort_values("TotalExcess", ascending=False)
    )
    by_sm["Total_L"] = by_sm["TotalExcess"] / 1e5
    styled_sm = (
        by_sm[["Salesman", "PartiesToCall", "Total_L"]].style.format({
            "PartiesToCall": "{:,}",
            "Total_L":       "₹{:,.1f} L",
        })
    )
    st.dataframe(styled_sm, use_container_width=True, hide_index=True)

    # ── CSV export of the worklist ──────────────────────────────────
    export = work[[
        "PartyID", "PartyName", "SalesmanID", "Salesman",
        "Bills", "Receipts", "Excess", "Ratio",
    ]]
    csv = export.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download follow-up list (CSV)", csv,
        f"unmatched_followup_{date.today()}.csv", "text/csv",
        key="dbt_unmatched_dl",
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


@st.cache_data(ttl=3600, show_spinner=False)
def _load_unmatched_receipts(window_days: int = 180) -> pd.DataFrame:
    """Parties whose receipts in the last `window_days` exceed their
    bills in the same window — the operational follow-up worklist
    that mirrors the manual matching report's "Uncleared/Unmatched
    Cheques" column.

    Returns raw per-party Bills/Receipts/Excess + party metadata
    needed for salesman attribution. The UI then applies a ratio
    floor (default 1.20) + min-excess floor (default Rs1L) to land
    near the Rs6.84 Cr target.

    The proxy used here is broader than "cheque didn't clear"
    (which is the PDC pipeline section). It catches:
      - Cheques entered but not allocated to bills yet
      - Lump-sum payments covering multiple bills (where the
        accountant hasn't broken them down)
      - Over-payments / round-offs

    All cases the sales team needs to phone the outlet about.
    """
    sql = f"""
        WITH window_activity AS (
            SELECT
                d.PartyID,
                SUM(CASE WHEN d.DrCrIndicator='D'
                         THEN CAST(d.Amount AS float) ELSE 0 END) AS Bills,
                SUM(CASE WHEN d.DrCrIndicator='C'
                          AND CAST(h.VoucherDate AS date)
                              <= CAST(GETDATE() AS date)
                         THEN CAST(d.Amount AS float) ELSE 0 END) AS Receipts
            FROM TrVocDetail d
            JOIN TrVocHead   h
                ON h.TransTypeID = d.TransTypeID
               AND h.VoucherNo   = d.VoucherNo
            WHERE d.PartyID LIKE 'D%'
              AND h.Cancelled = 'N'
              AND h.VoucherDate >= DATEADD(DAY, -{int(window_days)},
                                           CAST(GETDATE() AS date))
              AND h.VoucherDate <= CAST(GETDATE() AS date)
            GROUP BY d.PartyID
        )
        SELECT
            wa.PartyID,
            ISNULL(p.PartyName, '(unknown)')               AS PartyName,
            ISNULL(p.LicenseTypeID, '')                    AS LicenseTypeID,
            ISNULL(p.AcType3ID, '')                        AS AcType3ID,
            ISNULL(p.SalesManID,  '')                      AS SalesManID,
            ISNULL(p.SalesManID1, '')                      AS SalesManID1,
            ISNULL(p.SalesManID2, '')                      AS SalesManID2,
            wa.Bills,
            wa.Receipts,
            (wa.Receipts - wa.Bills)                       AS Excess
        FROM window_activity wa
        JOIN MsPartyMaster p ON p.PartyID = wa.PartyID
        WHERE wa.Bills > 0
          AND wa.Receipts > wa.Bills
        ORDER BY (wa.Receipts - wa.Bills) DESC
    """
    df = run_query(sql)
    if df.empty:
        return pd.DataFrame(columns=[
            "PartyID","PartyName","LicenseTypeID","AcType3ID",
            "SalesManID","SalesManID1","SalesManID2",
            "Bills","Receipts","Excess",
        ])
    for c in ("Bills", "Receipts", "Excess"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_advances() -> pd.DataFrame:
    """Parties with net Cr balance (advances received that aren't being
    netted against outstanding). Same query as Tab C in the gap section,
    cached because it's also useful as a sanity number.
    """
    sql = """
        SELECT d.PartyID,
               ISNULL(p.PartyName,'(unknown)')              AS PartyName,
               SUM(CASE WHEN d.DrCrIndicator='D'
                        THEN CAST(d.Amount AS float)
                        ELSE -CAST(d.Amount AS float) END)  AS Balance
        FROM TrVocDetail d
        JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID
                          AND h.VoucherNo   = d.VoucherNo
        WHERE d.PartyID LIKE 'D%' AND h.Cancelled = 'N'
          AND CAST(h.VoucherDate AS date) <= CAST(GETDATE() AS date)
        GROUP BY d.PartyID, p.PartyName
        HAVING SUM(CASE WHEN d.DrCrIndicator='D'
                        THEN CAST(d.Amount AS float)
                        ELSE -CAST(d.Amount AS float) END) < -1000
        ORDER BY Balance ASC
    """
    df = run_query(sql)
    if df.empty:
        return pd.DataFrame(columns=["PartyID","PartyName","Balance"])
    df["Balance"] = pd.to_numeric(df["Balance"], errors="coerce").fillna(0.0)
    return df


def _year_bucket(days: int) -> str:
    if days <= 365:   return "0-1 yr"
    if days <= 730:   return "1-2 yrs"
    if days <= 1825:  return "2-5 yrs"
    return "5+ yrs"


def _section_gap_inspection(df: pd.DataFrame) -> None:
    """Drill-down into where the Rs9 Cr gap vs BS Rs56 Cr is coming from.
    Four tabs:
      A) Non-sales TransType breakdown (TT=12 Return Cheques etc.)
      B) Ancient bills by year-bucket (writeoff candidates)
      C) Credit balances (advances that aren't netted)
      D) Top driver parties (oldest * value)
    Surfaces, doesn't hide.
    """
    st.subheader("🔍 What's in the gap?")
    st.caption(
        "Drill-down into the dashboard's excess vs BS Sundry Debtors. "
        "Owner can decide whether to write off, net, or scope each "
        "bucket individually. **Nothing here is silently dropped** — "
        "all four buckets contribute to the headline outstanding."
    )

    total_cr = float(df["Remaining"].sum()) / 1e7
    non_sales = df[~df["TransTypeID"].isin(SALES_TT)].copy()
    old_bills = df[df["AgeDays"] > 365].copy()
    advances  = _load_advances()
    advances_total_cr = float(abs(advances["Balance"].sum())) / 1e7 if not advances.empty else 0.0

    # Top-line summary bar — keeps the 4 numbers visible regardless of tab
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Non-sales DR", f"₹{non_sales['Remaining'].sum()/1e7:.2f} Cr",
              f"{len(non_sales):,} lines · {non_sales['PartyID'].nunique():,} parties")
    s2.metric("Old debt (>1 yr)",
              f"₹{old_bills['Remaining'].sum()/1e7:.2f} Cr",
              f"{len(old_bills):,} lines")
    s3.metric("Credit balances (advances)",
              f"₹{advances_total_cr:.2f} Cr",
              f"{len(advances):,} parties",
              delta_color="inverse")
    s4.metric("Net if advances applied",
              f"₹{total_cr - advances_total_cr:.2f} Cr",
              f"Today's gross: ₹{total_cr:.2f} Cr")

    tabA, tabB, tabC, tabD = st.tabs([
        "A) Non-sales TransType",
        "B) Ancient bills (>1 yr)",
        "C) Credit balances (advances)",
        "D) Top drivers",
    ])

    # ───── Tab A — by TransType (non-sales only) ─────────────────────
    with tabA:
        st.caption(
            "Outstanding originating from non-sales Dr entries — "
            "return cheques, JVs, debit notes, sales orders, etc. "
            "These are real ledger movements that count toward "
            "Sundry Debtors but don't have brand metadata."
        )
        if non_sales.empty:
            st.info("No non-sales DR outstanding. Clean.")
        else:
            by_tt = (
                non_sales.groupby(
                    ["TransTypeID", "TransTypeName"], observed=True,
                ).agg(
                    Lines=("VoucherNo", "count"),
                    Parties=("PartyID", "nunique"),
                    Outstanding=("Remaining", "sum"),
                )
                .reset_index()
                .sort_values("Outstanding", ascending=False)
            )
            by_tt["Out_Cr"] = by_tt["Outstanding"] / 1e7
            disp = by_tt[["TransTypeID", "TransTypeName", "Lines",
                          "Parties", "Out_Cr"]]
            styled = (
                disp.style.format({
                    "TransTypeID": "{:.0f}",
                    "Lines":       "{:,.0f}",
                    "Parties":     "{:,.0f}",
                    "Out_Cr":      "₹{:.2f} Cr",
                })
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

    # ───── Tab B — ancient bills by year-bucket + by-party drilldown ──
    with tabB:
        st.caption(
            "Bills older than 1 year — likely write-off candidates. "
            "5+ year bills are pre-FY20 vintage and almost certainly "
            "should be in 'Bad Debts Written Off' on the P&L rather "
            "than Sundry Debtors on the BS."
        )
        if old_bills.empty:
            st.info("No bills older than 1 year. Books are clean on this dimension.")
        else:
            old_bills["YearBucket"] = old_bills["AgeDays"].map(_year_bucket)
            yb = (
                old_bills.groupby("YearBucket", observed=True)
                         .agg(Lines=("VoucherNo", "count"),
                              Parties=("PartyID", "nunique"),
                              Outstanding=("Remaining", "sum"))
                         .reindex(["1-2 yrs", "2-5 yrs", "5+ yrs"]).fillna(0)
                         .reset_index()
            )
            yb["Out_Cr"] = yb["Outstanding"] / 1e7
            styled = (
                yb.style.format({
                    "Lines":      "{:,.0f}",
                    "Parties":    "{:,.0f}",
                    "Out_Cr":     "₹{:.2f} Cr",
                })
            )
            st.markdown("**By age bucket:**")
            st.dataframe(styled, use_container_width=True, hide_index=True)

            st.markdown("**By party (top 50 by outstanding):**")
            by_party = (
                old_bills.groupby(["PartyID", "PartyName"], observed=True)
                         .agg(Outstanding=("Remaining", "sum"),
                              OldestDays=("AgeDays", "max"),
                              Lines=("VoucherNo", "count"))
                         .reset_index()
                         .sort_values("Outstanding", ascending=False)
                         .head(50)
            )
            by_party["Outstanding_L"] = by_party["Outstanding"] / 1e5
            by_party["OldestYears"]   = (by_party["OldestDays"] / 365).round(1)
            disp = by_party[["PartyID", "PartyName", "Outstanding_L",
                             "OldestYears", "Lines"]]
            styled = (
                disp.style.format({
                    "Outstanding_L": "₹{:.1f} L",
                    "OldestYears":   "{:.1f} yrs",
                    "Lines":         "{:.0f}",
                })
            )
            st.dataframe(styled, use_container_width=True, hide_index=True, height=380)

            csv = by_party.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Download write-off review list (CSV)", csv,
                "writeoff_candidates.csv", "text/csv",
                key="dbt_writeoff_dl",
            )

    # ───── Tab C — credit balances (advances) ────────────────────────
    with tabC:
        st.caption(
            "Parties whose ledger shows a NET CREDIT balance — they've "
            "paid more than was billed. These are advance receipts / "
            "round-off credits sitting in the customer ledger. The "
            "manual matching report would typically net these against "
            "their own outstanding; the BS likely nets some but not "
            "all of them."
        )
        if advances.empty:
            st.info("No parties with net credit balance.")
        else:
            adv = advances.copy()
            adv["Balance_L"] = adv["Balance"] / 1e5
            disp = adv[["PartyID", "PartyName", "Balance_L"]].head(50)
            styled = (
                disp.style.format({
                    "Balance_L": "₹{:.1f} L",
                })
            )
            st.markdown(
                f"**{len(advances):,} parties** carry a combined "
                f"**₹{advances_total_cr:.2f} Cr** in credit balances. "
                f"If fully netted, dashboard outstanding would drop "
                f"from ₹{total_cr:.2f} Cr to "
                f"**₹{total_cr - advances_total_cr:.2f} Cr** — *below* "
                f"the BS target, so BS itself isn't fully netting these. "
                f"Use this list to decide which advances are legitimate "
                f"(apply to next bill) vs which are data-entry errors."
            )
            st.dataframe(styled, use_container_width=True, hide_index=True, height=380)
            st.caption(
                "⚠️ A credit balance of ₹50L+ on an active outlet usually "
                "means cheques have been applied to the wrong party in "
                "the ERP — worth a 5-minute accountant check."
            )

    # ───── Tab D — top driver parties ─────────────────────────────────
    with tabD:
        st.caption(
            "Top 20 parties driving the gap, ranked by `OldestBill × "
            "Outstanding` so very-old high-value lines float to the top."
        )
        work = df.copy()
        work["DriverScore"] = work["AgeDays"] * work["Remaining"] / 1e6
        big = (
            work.groupby(["PartyID", "PartyName"], observed=True)
                .agg(Outstanding=("Remaining", "sum"),
                     OldestBill=("AgeDays", "max"),
                     AvgAge=("AgeDays", "mean"),
                     Lines=("VoucherNo", "count"),
                     DriverScore=("DriverScore", "sum"))
                .reset_index()
                .sort_values("DriverScore", ascending=False)
                .head(20)
        )
        big["Outstanding_L"] = big["Outstanding"] / 1e5
        disp = big[["PartyID", "PartyName", "Outstanding_L",
                    "OldestBill", "AvgAge", "Lines"]]
        styled = (
            disp.style.format({
                "Outstanding_L": "₹{:.1f} L",
                "OldestBill":    "{:.0f} d",
                "AvgAge":        "{:.0f} d",
                "Lines":         "{:.0f}",
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True, height=480)


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
        f"Dr/Cr row per debtor, with post-dated CR rows (uncleared "
        f"PDCs) excluded — surfaced separately in the PDC pipeline section."
    )


# ═══════════════════════════════════════════════════════════════════════════
# NEW TIERED SECTIONS (Tier 1 / 2 / 3 of the redesigned layout)
# ═══════════════════════════════════════════════════════════════════════════

def _ban_status_label(banned: str, ban_type: str) -> str:
    """Return a one-line 'Ban Status' badge for table display."""
    if (banned or "").strip().upper() == "Y":
        reason = BAN_REASON_MAP.get((ban_type or "").strip(), "Banned")
        return f"🛑 {reason}"
    return "⚠️ Not banned"


def _section_hero_compact(unpaid_df: pd.DataFrame,
                          unmatched_df: pd.DataFrame) -> None:
    """4-card compact snapshot. Replaces the verbose hero card."""
    outstanding = float(unpaid_df["Remaining"].sum()) / 1e7 if not unpaid_df.empty else 0.0
    unmatched   = float(unmatched_df["Excess"].sum()) / 1e7 if not unmatched_df.empty else 0.0
    overdue     = float(unpaid_df.loc[unpaid_df["IsOverdue"], "Remaining"].sum()) / 1e7 \
                  if not unpaid_df.empty else 0.0

    if not unpaid_df.empty and unpaid_df["Remaining"].sum() > 0:
        dso = (unpaid_df["AgeDays"] * unpaid_df["Remaining"]).sum() \
              / unpaid_df["Remaining"].sum()
    else:
        dso = 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Outstanding", f"₹{outstanding:.2f} Cr")
    c2.metric("Unmatched", f"₹{unmatched:.2f} Cr",
              "Receipts > bills (180d, 1.20x, ≥₹1L)")
    pct = (overdue / outstanding * 100) if outstanding else 0
    c3.metric("Overdue", f"₹{overdue:.2f} Cr", f"{pct:.0f}% of total",
              delta_color="inverse")
    c4.metric("Avg DSO", f"{dso:.0f} days", "Weighted by Rs outstanding")


def _section_dead_money(unpaid_df: pd.DataFrame,
                        last_bill_df: pd.DataFrame) -> None:
    """Outstanding from outlets that haven't billed in 90+ days."""
    st.subheader("⚫ Dead Money — Parties Stopped Buying")
    st.caption(
        f"Outstanding from outlets that haven't billed in {GHOST_DAYS}+ "
        "days. Hardest recovery — often a candidate for write-off or "
        "Trader-Association escalation."
    )

    df = (
        unpaid_df.groupby(
            ["PartyID", "PartyName", "BannedByAssoc", "AssoBan_Type"],
            observed=True,
        )
        .agg(Outstanding=("Remaining", "sum"),
             OldestBillAge=("AgeDays", "max"))
        .reset_index()
    )
    df = df.merge(
        last_bill_df[["PartyID", "DaysSinceLastBill"]],
        on="PartyID", how="left",
    )
    df["DaysSinceLastBill"] = df["DaysSinceLastBill"].fillna(9999)

    ghosted = df[df["DaysSinceLastBill"] >= GHOST_DAYS] \
        .sort_values("Outstanding", ascending=False)

    if ghosted.empty:
        st.success("✅ No ghosted parties with outstanding."); return

    total_cr     = float(ghosted["Outstanding"].sum()) / 1e7
    count        = int(len(ghosted))
    banned_count = int((ghosted["BannedByAssoc"] == "Y").sum())
    not_banned   = ghosted[ghosted["BannedByAssoc"] != "Y"]
    not_banned_l = float(not_banned["Outstanding"].sum()) / 1e5

    c1, c2, c3 = st.columns(3)
    c1.metric("Dead money total", f"₹{total_cr:.2f} Cr")
    c2.metric("Parties", f"{count:,}",
              f"{banned_count} TA-banned · {count - banned_count} not")
    c3.metric("Not yet TA-banned",
              f"₹{not_banned_l:,.1f} L",
              "Escalate to association",
              delta_color="inverse")

    ghosted["Ban Status"]   = ghosted.apply(
        lambda r: _ban_status_label(r["BannedByAssoc"], r["AssoBan_Type"]),
        axis=1,
    )
    ghosted["Outstanding_L"] = ghosted["Outstanding"]      / 1e5
    ghosted["DaysGhosted"]   = ghosted["DaysSinceLastBill"].astype(int)

    disp = ghosted[["PartyName", "Ban Status", "Outstanding_L",
                    "DaysGhosted", "OldestBillAge"]].rename(columns={
        "PartyName": "Party",
        "Outstanding_L": "Outstanding (₹L)",
        "DaysGhosted": "Days No Bill",
        "OldestBillAge": "Oldest Bill (d)",
    })
    styled = (
        disp.style.format({
            "Outstanding (₹L)": "₹{:.1f}",
            "Days No Bill":     "{:.0f}",
            "Oldest Bill (d)":  "{:.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=400)

    csv = ghosted[[
        "PartyID", "PartyName", "BannedByAssoc", "AssoBan_Type",
        "Outstanding", "DaysGhosted", "OldestBillAge",
    ]].to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download dead money list", csv,
        f"dead_money_{date.today()}.csv", "text/csv",
        key="dbt_dl_deadmoney",
    )


def _section_returns_ghosted(unpaid_df: pd.DataFrame,
                             returns_df: pd.DataFrame,
                             last_bill_df: pd.DataFrame) -> None:
    """Worst case: bounced cheque + ghosted + still owing."""
    st.subheader("🚨 Cheque Returned + Ghosted")
    st.caption(
        "Worst case: party issued a cheque (legal commitment), it "
        "bounced, then stopped buying. Highest-priority recovery and "
        "potential legal-action queue."
    )

    df = (
        unpaid_df.groupby(
            ["PartyID", "PartyName", "BannedByAssoc", "AssoBan_Type"],
            observed=True,
        )
        .agg(Outstanding=("Remaining", "sum"))
        .reset_index()
    )
    df = df.merge(returns_df, on="PartyID", how="inner")
    df = df.merge(last_bill_df, on="PartyID", how="left")
    df["DaysSinceLastBill"] = df["DaysSinceLastBill"].fillna(9999)

    worst = df[df["DaysSinceLastBill"] >= GHOST_DAYS] \
        .sort_values("Outstanding", ascending=False)

    if worst.empty:
        st.success("✅ No worst-case parties (bounced + ghosted)."); return

    total_l = float(worst["Outstanding"].sum()) / 1e5

    c1, c2 = st.columns(2)
    c1.metric("Legal action queue", f"{len(worst):,} parties")
    c2.metric("Outstanding", f"₹{total_l:.1f} L")

    worst["Outstanding_L"] = worst["Outstanding"]       / 1e5
    worst["Returned_L"]    = worst["TotalReturnedAmt"]  / 1e5
    worst["Ban Status"]    = worst.apply(
        lambda r: _ban_status_label(r["BannedByAssoc"], r["AssoBan_Type"]),
        axis=1,
    )

    disp = worst[["PartyName", "Ban Status", "Outstanding_L",
                  "Returned_L", "ReturnCount",
                  "DaysSinceLastBill"]].rename(columns={
        "PartyName": "Party",
        "Outstanding_L": "Outstanding (₹L)",
        "Returned_L":    "Returned (₹L)",
        "ReturnCount":   "# Returns",
        "DaysSinceLastBill": "Ghosted (d)",
    })
    styled = (
        disp.style.format({
            "Outstanding (₹L)": "₹{:.1f}",
            "Returned (₹L)":    "₹{:.1f}",
            "# Returns":        "{:.0f}",
            "Ghosted (d)":      "{:.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=350)


def _section_active_bouncers(unpaid_df: pd.DataFrame,
                             returns_df: pd.DataFrame,
                             last_bill_df: pd.DataFrame) -> None:
    """Bounced but still buying — recoverable via relationship."""
    st.subheader("⚠️ Active Bouncers — Cheque Returned but Still Buying")
    st.caption(
        "Party's cheque bounced but they're still dealing with us. "
        "Recoverable through relationship — top operational priority "
        "for the sales team."
    )

    df = (
        unpaid_df.groupby(["PartyID", "PartyName", "BannedByAssoc"],
                          observed=True)
        .agg(Outstanding=("Remaining", "sum"),
             OldestBillAge=("AgeDays", "max"))
        .reset_index()
    )
    df = df.merge(returns_df, on="PartyID", how="inner")
    df = df.merge(last_bill_df, on="PartyID", how="left")
    df["DaysSinceLastBill"] = df["DaysSinceLastBill"].fillna(9999)

    active = df[df["DaysSinceLastBill"] < GHOST_DAYS] \
        .sort_values("TotalReturnedAmt", ascending=False).head(50)

    if active.empty:
        st.success("✅ No active bouncers."); return

    total_ret = float(active["TotalReturnedAmt"].sum()) / 1e7
    total_out = float(active["Outstanding"].sum())       / 1e7

    c1, c2, c3 = st.columns(3)
    c1.metric("Parties", f"{len(active):,}")
    c2.metric("Total cheques returned", f"₹{total_ret:.2f} Cr")
    c3.metric("Current outstanding",    f"₹{total_out:.2f} Cr")

    active["Returned_L"]    = active["TotalReturnedAmt"] / 1e5
    active["Outstanding_L"] = active["Outstanding"]      / 1e5

    disp = active[["PartyName", "Returned_L", "ReturnCount",
                   "DaysSinceLastReturn", "Outstanding_L",
                   "OldestBillAge"]].rename(columns={
        "PartyName": "Party",
        "Returned_L": "Returned (₹L)",
        "ReturnCount": "# Returns",
        "DaysSinceLastReturn": "Last Return",
        "Outstanding_L": "Outstanding (₹L)",
        "OldestBillAge": "Oldest Unpaid (d)",
    })
    styled = (
        disp.style.format({
            "Returned (₹L)":     "₹{:.1f}",
            "# Returns":         "{:.0f}",
            "Last Return":       "{:.0f}d ago",
            "Outstanding (₹L)":  "₹{:.1f}",
            "Oldest Unpaid (d)": "{:.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=400)


def _section_billing_risk(unpaid_df: pd.DataFrame) -> None:
    """Material outstanding but NOT TA-banned — internal hold candidate."""
    st.subheader("⚠️ Billing Risk — Outstanding Parties NOT Banned")
    st.caption(
        "Material outstanding (>₹1 L, oldest bill >60 days) but "
        "Trader-Association hasn't banned them yet. Billing department "
        "can still raise invoices — internal hold required, plus "
        "escalate to TA."
    )

    df = (
        unpaid_df.groupby(["PartyID", "PartyName", "BannedByAssoc"],
                          observed=True)
        .agg(Outstanding=("Remaining", "sum"),
             OldestBillAge=("AgeDays", "max"))
        .reset_index()
    )
    risk = df[
        (df["BannedByAssoc"] != "Y")
        & (df["OldestBillAge"] > 60)
        & (df["Outstanding"]  > 100_000)
    ].sort_values("Outstanding", ascending=False).head(50)

    if risk.empty:
        st.success("✅ All overdue parties already TA-banned."); return

    total = float(risk["Outstanding"].sum()) / 1e7
    c1, c2 = st.columns(2)
    c1.metric("Parties at risk", f"{len(risk):,}")
    c2.metric("Total exposure", f"₹{total:.2f} Cr",
              "Escalate to TA or internal hold",
              delta_color="inverse")

    risk["Outstanding_L"] = risk["Outstanding"] / 1e5
    disp = risk[["PartyName", "Outstanding_L", "OldestBillAge"]].rename(columns={
        "PartyName":     "Party",
        "Outstanding_L": "Outstanding (₹L)",
        "OldestBillAge": "Oldest Bill (d)",
    })
    styled = (
        disp.style.format({
            "Outstanding (₹L)": "₹{:.1f}",
            "Oldest Bill (d)":  "{:.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=400)


def _section_banned_register(unpaid_df: pd.DataFrame) -> None:
    """Full TA banned register grouped by reason + per-party list."""
    df = (
        unpaid_df.groupby(["PartyID", "PartyName", "BannedByAssoc",
                           "AssoBan_Type"], observed=True)
        .agg(Outstanding=("Remaining", "sum"),
             OldestBillAge=("AgeDays", "max"))
        .reset_index()
    )
    banned = df[df["BannedByAssoc"] == "Y"] \
        .sort_values("Outstanding", ascending=False)

    if banned.empty:
        st.info("No banned parties with outstanding."); return

    by_reason = (
        banned.groupby("AssoBan_Type", observed=True)
              .agg(Parties=("PartyID", "nunique"),
                   Outstanding=("Outstanding", "sum"))
              .reset_index()
    )
    by_reason["Outstanding_Cr"] = by_reason["Outstanding"] / 1e7
    by_reason["Reason"]         = by_reason["AssoBan_Type"].map(BAN_REASON_MAP) \
                                                          .fillna("—")
    by_reason = by_reason.sort_values("Outstanding", ascending=False)

    st.markdown("**Summary by ban reason:**")
    summary_disp = by_reason[["AssoBan_Type", "Reason",
                              "Parties", "Outstanding_Cr"]]
    st.dataframe(
        summary_disp.style.format({
            "Outstanding_Cr": "₹{:.2f} Cr",
            "Parties":        "{:.0f}",
        }),
        use_container_width=True, hide_index=True,
    )

    banned["Outstanding_L"] = banned["Outstanding"] / 1e5

    st.markdown("**Full banned register:**")
    disp = banned[["PartyName", "AssoBan_Type", "Outstanding_L",
                   "OldestBillAge"]].rename(columns={
        "PartyName":      "Party",
        "AssoBan_Type":   "Reason",
        "Outstanding_L":  "Outstanding (₹L)",
        "OldestBillAge":  "Oldest Bill (d)",
    })
    styled = (
        disp.style.format({
            "Outstanding (₹L)": "₹{:.1f}",
            "Oldest Bill (d)":  "{:.0f}",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True, height=500)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("📊 Debtors Ageing")
    st.caption(
        "Bill-wise FIFO ageing with Trader-Association integration. "
        "Layout is urgency-driven: **Critical** (Dead Money + Bounced "
        "+ Ghosted) is always visible; **Reference** sections (banned "
        "register, salesman board, ageing buckets, gap inspection, "
        "etc.) live in collapsible expanders at the bottom."
    )

    today = pd.Timestamp.today().normalize()
    with st.spinner("Loading debtor ledger + reference tables…"):
        ledger        = _load_ledger()
        returns_df    = _load_cheque_returns_per_party()
        last_bill_df  = _load_last_bill_per_party()
    with st.spinner("Computing party-level FIFO ageing…"):
        unpaid       = _fifo_unpaid(ledger, today)
        unmatched_df = _load_unmatched_receipts(window_days=180)
    # Apply the same default filter the unmatched section uses (ratio
    # 1.20, min Rs1L excess) so the hero KPI matches what the operator
    # sees in the dedicated section.
    if not unmatched_df.empty:
        unmatched_df = unmatched_df[
            (unmatched_df["Bills"] > 0)
            & (unmatched_df["Receipts"] >= unmatched_df["Bills"] * 1.20)
            & (unmatched_df["Excess"] >= 1e5)
        ].copy()

    # Telemetry to Streamlit Cloud logs
    print(
        f"[debtors] ledger_rows={len(ledger):,} unpaid_lines={len(unpaid):,} "
        f"parties={unpaid['PartyID'].nunique() if not unpaid.empty else 0} "
        f"total_outstanding_cr="
        f"{(float(unpaid['Remaining'].sum())/1e7) if not unpaid.empty else 0:.2f} "
        f"banned_parties={int((unpaid['BannedByAssoc']=='Y').groupby(unpaid['PartyID']).max().sum()) if not unpaid.empty else 0}",
        file=sys.stderr,
    )

    if unpaid.empty:
        st.success("🎉 No outstanding debtor balances. All bills paid.")
        return

    # ─────────── TIER 1: CRITICAL ───────────
    safe_section("Reconciliation",    _section_reconciliation, unpaid)
    st.divider()
    safe_section("Dead Money",        _section_dead_money,
                 unpaid, last_bill_df)
    st.divider()
    safe_section("Bounced + Ghosted", _section_returns_ghosted,
                 unpaid, returns_df, last_bill_df)
    st.divider()

    # ─────────── TIER 2: URGENT ───────────
    safe_section("Active Bouncers",   _section_active_bouncers,
                 unpaid, returns_df, last_bill_df)
    st.divider()
    safe_section("Billing Risk",      _section_billing_risk, unpaid)
    st.divider()

    # ─────────── TIER 3: OPERATIONAL ───────────
    safe_section("Snapshot",          _section_hero_compact,
                 unpaid, unmatched_df)
    safe_section("PDC Pipeline",      _section_pdc_pipeline, today)
    st.divider()

    # ─────────── TIER 4: REFERENCE (collapsed) ───────────
    with st.expander("📋 Trader Association Banned Register", expanded=False):
        safe_section("TA Banned Register", _section_banned_register, unpaid)

    with st.expander("🚨 Party Risk (Hold / Renegotiate / Watch / Good)",
                     expanded=False):
        safe_section("Party risk",     _section_party_risk,   unpaid)

    with st.expander("👥 Salesman Collection Efficiency", expanded=False):
        safe_section("By Salesman",    _section_by_salesman,  unpaid)

    with st.expander("🏷️ By Principal / Channel", expanded=False):
        safe_section("By Principal",   _section_by_principal, unpaid)
        safe_section("By Channel",     _section_by_channel,   unpaid)

    with st.expander("📊 Ageing Buckets Detail", expanded=False):
        safe_section("Ageing Buckets", _section_ageing_buckets, unpaid)

    with st.expander("🔍 Gap Inspection (write-offs, advances, anomalies)",
                     expanded=False):
        safe_section("Gap Inspection", _section_gap_inspection, unpaid)

    with st.expander("💰 Unmatched Receipts Follow-up", expanded=False):
        safe_section("Unmatched Follow-up", _section_unmatched_followup)

    with st.expander("🔄 Renegotiation Candidates", expanded=False):
        safe_section("Renegotiation",  _section_renegotiate,  unpaid)

    with st.expander("✅ Spot Check (5 known high-balance parties)",
                     expanded=False):
        safe_section("Spot Check",     _section_spot_check,   unpaid)

    with st.expander("🔧 Sanity Check vs Balance Sheet", expanded=False):
        safe_section("Sanity Check",   _section_sanity,       unpaid)
