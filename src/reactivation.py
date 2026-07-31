"""src/reactivation.py — Dormant-outlet reactivation queue (S4).

The Debtors → Ghosted section surfaces parties that stopped billing AND
still owe us money. This tab is different: it also surfaces parties that
stopped billing after PAYING US CLEANLY — those are silent revenue we
could bring back if a salesman picks up the phone.

Ranked by lifetime revenue (over whatever history the ERP retains), so
the biggest historical customers surface first. Owner can annotate each
row with an assigned salesman + due-by date + note — stored in
data/reactivation_queue.json, committed with the repo.

Data sources:
    · TrVocDetail (sum of debit-side sales-bill Amount for lifetime rev)
    · _load_last_bill_per_party() from debtors.py (days silent)
    · MsPartyMaster (channel, salesman, ban flag)
    · _fifo_unpaid outstanding (so we know if they also owe us)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import format_inr
from src.debtors import (
    _load_ledger, _fifo_unpaid, _load_last_bill_per_party,
    SALES_TT, CHANNEL_MAP,
)


_ANNOTATIONS_FILE = (Path(__file__).resolve().parent.parent
                     / "data" / "reactivation_queue.json")
_ANNOTATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


CONTACT_STATUSES = ["", "Not started", "Call attempted", "Call connected",
                    "Meeting fixed", "Sample sent", "Revived", "Refused",
                    "Confirmed closed"]


# ─── Annotation storage ─────────────────────────────────────────────────────

def _load_annotations() -> dict:
    if not _ANNOTATIONS_FILE.exists():
        return {}
    try:
        return json.loads(_ANNOTATIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_annotations(data: dict) -> None:
    _ANNOTATIONS_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ─── Lifetime revenue loader ────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _load_lifetime_revenue() -> pd.DataFrame:
    """Per-party total historical revenue from all sales invoices the ERP
    still retains. Uses TrVocDetail's Dr side (the outstanding-before-
    payment amount) which reflects the invoice value even for paid bills."""
    sales_csv = ",".join(str(t) for t in SALES_TT)
    sql = f"""
        SELECT
            d.PartyID,
            SUM(CAST(d.Amount AS float))       AS LifetimeRevenue,
            COUNT(DISTINCT d.VoucherNo)        AS LifetimeBills
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID
                          AND h.VoucherNo   = d.VoucherNo
                          AND h.FinancialYear = d.FinancialYear
        WHERE d.PartyID LIKE 'D%'
          AND d.DrCrIndicator = 'D'
          AND d.TransTypeID IN ({sales_csv})
          AND h.Cancelled = 'N'
        GROUP BY d.PartyID
    """
    df = run_query(sql)
    if df.empty:
        return df
    df["LifetimeRevenue"] = pd.to_numeric(df["LifetimeRevenue"],
                                          errors="coerce").fillna(0.0)
    df["LifetimeBills"]   = pd.to_numeric(df["LifetimeBills"],
                                          errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_party_meta() -> pd.DataFrame:
    return run_query("""
        SELECT
            PartyID,
            ISNULL(PartyName, '')                     AS PartyName,
            ISNULL(LicenseTypeID, '')                 AS LicenseTypeID,
            ISNULL(SalesManID,  '')                   AS SM1,
            ISNULL(SalesManID1, '')                   AS SM2,
            ISNULL(SalesManID2, '')                   AS SM3,
            ISNULL(BannedByAssoc, '')                 AS BannedByAssoc,
            ISNULL(AssoBan_Type, '')                  AS AssoBan_Type,
            ISNULL(CreditDays, 0)                     AS CreditDays,
            ISNULL(Phone, '')                         AS Phone
        FROM MsPartyMaster
        WHERE PartyID LIKE 'D%'
    """)


# ─── Assemble the queue ─────────────────────────────────────────────────────

def _build_queue(silent_days: int = 90) -> pd.DataFrame:
    lb   = _load_last_bill_per_party()
    ltv  = _load_lifetime_revenue()
    meta = _load_party_meta()
    if lb.empty or ltv.empty or meta.empty:
        return pd.DataFrame()

    dormant = lb[lb["DaysSinceLastBill"] >= silent_days].copy()
    df = dormant.merge(ltv, on="PartyID", how="left")
    df["LifetimeRevenue"] = df["LifetimeRevenue"].fillna(0.0)
    df["LifetimeBills"]   = df["LifetimeBills"].fillna(0).astype(int)
    df = df.merge(meta, on="PartyID", how="left")
    df["PartyName"]     = df["PartyName"].fillna("(unknown)")
    df["LicenseTypeID"] = df["LicenseTypeID"].fillna("")
    df["Channel"]       = df["LicenseTypeID"].map(CHANNEL_MAP).fillna("Other")

    # Outstanding — merge from fifo_unpaid so we can flag "silent AND still owing"
    try:
        ledger = _load_ledger()
        unpaid = _fifo_unpaid(ledger, pd.Timestamp(date.today()))
    except Exception:
        unpaid = pd.DataFrame()
    if not unpaid.empty:
        owed = (unpaid.groupby("PartyID", observed=True)
                        ["Remaining"].sum().reset_index()
                        .rename(columns={"Remaining": "Outstanding"}))
        df = df.merge(owed, on="PartyID", how="left")
    else:
        df["Outstanding"] = 0.0
    df["Outstanding"] = df["Outstanding"].fillna(0.0)

    return df.sort_values("LifetimeRevenue", ascending=False).reset_index(drop=True)


# ─── UI sections ────────────────────────────────────────────────────────────

def _section_summary(df: pd.DataFrame) -> None:
    if df.empty:
        return
    total_ltv       = float(df["LifetimeRevenue"].sum())
    parties_count   = len(df)
    with_out        = df[df["Outstanding"] > 0.5]
    with_out_count  = len(with_out)
    banned          = df[df["BannedByAssoc"] == "Y"]
    biggest         = df.iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Dormant parties", f"{parties_count:,}",
              help="Have not billed for the silent-window threshold you picked below.")
    c2.metric("Lifetime revenue at risk", format_inr(total_ltv),
              help="Sum of historical invoice value across all dormant parties.")
    c3.metric("Also owe us money", f"{with_out_count:,}",
              delta=format_inr(float(with_out["Outstanding"].sum())),
              delta_color="off")
    c4.metric("Banned parties in queue", f"{len(banned):,}",
              help="Association-banned. Reactivation requires the ban to lift first — "
                   "escalate via the Bans tab.")

    st.caption(
        f"Biggest single dormant customer: **{biggest['PartyName']}** "
        f"(lifetime revenue **{format_inr(float(biggest['LifetimeRevenue']))}**, "
        f"silent **{int(biggest['DaysSinceLastBill'])} days**)."
    )


def _section_queue_editor(df: pd.DataFrame) -> None:
    st.markdown("##### Reactivation queue (editable)")
    if df.empty:
        st.info("No dormant parties in the selected window.")
        return

    # Filters
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        min_ltv_l = st.number_input(
            "Min lifetime revenue (₹ Lakh)",
            min_value=0.0, step=1.0, value=1.0,
            key="reactiv_min_ltv",
            help="Hide small-tail parties. Set to 0 to see every dormant.")
    with fc2:
        exclude_banned = st.checkbox(
            "Hide banned parties", value=True,
            key="reactiv_hide_banned",
            help="Banned parties can't be reactivated until the ban lifts — "
                 "handle those in the Bans tab instead.")
    with fc3:
        channel_opts = ["All"] + sorted(df["Channel"].unique().tolist())
        picked_ch = st.selectbox("Channel", channel_opts, key="reactiv_ch")
    with fc4:
        max_rows = st.slider(
            "Show top N", min_value=25, max_value=500, value=100, step=25,
            key="reactiv_top_n")

    view = df.copy()
    if min_ltv_l > 0:
        view = view[view["LifetimeRevenue"] >= min_ltv_l * 1e5]
    if exclude_banned:
        view = view[view["BannedByAssoc"] != "Y"]
    if picked_ch != "All":
        view = view[view["Channel"] == picked_ch]
    view = view.head(max_rows)

    if view.empty:
        st.info("No dormant parties match the current filters.")
        return

    # Merge existing annotations
    ann_all = _load_annotations()
    view = view.copy()
    view["Assigned to"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("assigned_to", ""))
    view["Due by"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("due_by", ""))
    view["Contact status"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("contact_status", ""))
    view["Notes"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("notes", ""))

    editable = view[[
        "PartyID", "PartyName", "Channel", "LifetimeRevenue",
        "LifetimeBills", "DaysSinceLastBill", "LastBillDate",
        "Outstanding", "BannedByAssoc", "Assigned to", "Due by",
        "Contact status", "Notes",
    ]].rename(columns={
        "PartyName":         "Party",
        "LifetimeRevenue":   "Lifetime rev ₹",
        "LifetimeBills":     "Lifetime bills",
        "DaysSinceLastBill": "Days silent",
        "LastBillDate":      "Last bill",
        "BannedByAssoc":     "Ban?",
    })

    edited = st.data_editor(
        editable,
        hide_index=True,
        use_container_width=True,
        height=560,
        disabled=["PartyID", "Party", "Channel", "Lifetime rev ₹",
                  "Lifetime bills", "Days silent", "Last bill",
                  "Outstanding", "Ban?"],
        column_config={
            "PartyID": st.column_config.Column("PartyID", width="small"),
            "Lifetime rev ₹": st.column_config.NumberColumn(
                "Lifetime rev ₹", format="₹%.0f"),
            "Lifetime bills": st.column_config.NumberColumn(
                "Lifetime bills", format="%d"),
            "Days silent": st.column_config.NumberColumn(
                "Days silent", format="%d"),
            "Outstanding": st.column_config.NumberColumn(
                "Still owes ₹", format="₹%.0f",
                help="If > 0, this party is dormant AND owes us money — "
                     "highest priority."),
            "Ban?": st.column_config.Column("Ban?", width="small"),
            "Assigned to": st.column_config.TextColumn(
                "Assigned to",
                help="Salesman name — free text so you can type any handler."),
            "Due by": st.column_config.TextColumn(
                "Due by",
                help="Free text — 'this week', '15-Aug', etc."),
            "Contact status": st.column_config.SelectboxColumn(
                "Contact status", options=CONTACT_STATUSES),
            "Notes": st.column_config.TextColumn("Notes"),
        },
        key="reactiv_editor",
    )

    if st.button("💾 Save annotations", type="primary", key="reactiv_save"):
        ann_all = _load_annotations()
        touched = 0
        today_iso = date.today().isoformat()
        for _, row in edited.iterrows():
            pid = row["PartyID"]
            new = {
                "assigned_to":    str(row.get("Assigned to", "") or ""),
                "due_by":         str(row.get("Due by", "") or ""),
                "contact_status": str(row.get("Contact status", "") or ""),
                "notes":          str(row.get("Notes", "") or ""),
            }
            old = ann_all.get(pid, {})
            if any(old.get(k, "") != new[k] for k in new):
                new["updated_at"] = today_iso
                ann_all[pid] = new
                touched += 1
        _save_annotations(ann_all)
        st.success(f"Saved annotations for {touched} party(ies).")
        st.rerun()

    csv = edited.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download queue CSV", csv,
        f"reactivation_queue_{date.today()}.csv", "text/csv",
        key="reactiv_dl")


def _section_priority(df: pd.DataFrame) -> None:
    """Top-10 by lifetime value — the calls to make first."""
    st.markdown("##### 🔝 Top 10 by lifetime value (start here)")
    if df.empty:
        return
    view = df[df["BannedByAssoc"] != "Y"].head(10).copy()
    if view.empty:
        st.caption("No un-banned dormant parties in the queue.")
        return
    view["Silent"] = view["DaysSinceLastBill"].astype(int).astype(str) + " d"
    disp = view[["PartyName", "Channel", "LifetimeRevenue", "Silent",
                 "Outstanding"]].rename(columns={
        "PartyName": "Party", "LifetimeRevenue": "Lifetime rev",
        "Outstanding": "Still owes",
    })
    st.dataframe(
        disp.style.format({
            "Lifetime rev": format_inr,
            "Still owes":   format_inr,
        }),
        use_container_width=True, hide_index=True)


# ─── Main render ────────────────────────────────────────────────────────────

def render() -> None:
    st.title("🔁 Dormant Outlet Reactivation Queue")
    st.caption(
        "Every party that stopped billing you 90+ days ago, ranked by "
        "lifetime revenue. The parties near the top of this list used to "
        "pay you well and quietly disappeared — a call from a salesman "
        "often brings them back. Assign each one below and track progress "
        "in the **Contact status** column."
    )
    st.divider()

    # Threshold slider (default 90d, matches GHOST_DAYS)
    silent_days = st.slider(
        "Silent threshold (days without a sales bill)",
        min_value=30, max_value=365, value=90, step=15,
        key="reactiv_silent_days",
        help="How long a party has to be silent before it counts as dormant. "
             "90d matches the ERP's ghost definition; drop lower if you want "
             "an early-warning list, raise it to focus on the long-tail.")

    try:
        df = _build_queue(silent_days=int(silent_days))
    except Exception as e:
        st.warning(f"Reactivation queue unavailable: `{e}`")
        return

    if df.empty:
        st.info("No dormant parties in the selected window. 🎉")
        return

    _section_summary(df)
    st.divider()
    _section_priority(df)
    st.divider()
    _section_queue_editor(df)
