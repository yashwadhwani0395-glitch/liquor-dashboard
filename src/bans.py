"""src/bans.py — Trader Association ban dashboard (S1).

Standalone tab for tracking parties banned by the Trader Association.
The Debtors tab already has a summary section; this page adds:

  · Full ban register (owing + not owing) — you're often blocked from
    billing a party even if they've cleared, so 'not owing' still
    matters operationally.
  · Editable annotation layer — expected release date, appeal status,
    owner notes — stored in data/ban_annotations.json.
  · Ban-reason breakdown with exposure & party count.
  · Cross-reference: which banned parties are also ghosted / bounced
    cheques recently → prioritise the truly dead accounts.

Data source: MsPartyMaster.BannedByAssoc / AssoBan_Type /
AssoBan_Description columns, enriched with FIFO outstanding from the
same _load_ledger + _fifo_unpaid used by the Debtors tab.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from utils.helpers import format_inr
from src.debtors import (
    _load_ledger, _fifo_unpaid, _load_last_bill_per_party,
    _load_cheque_returns_per_party, BAN_REASON_MAP,
)


_ANNOTATIONS_FILE = (Path(__file__).resolve().parent.parent
                     / "data" / "ban_annotations.json")
_ANNOTATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


APPEAL_STATUSES = ["", "Not started", "Filed", "Under review",
                   "Payment plan agreed", "Ready to release", "Rejected"]


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


def _annotation_for(party_id: str) -> dict:
    ann = _load_annotations().get(party_id, {})
    return {
        "expected_release": ann.get("expected_release", ""),
        "appeal_status":    ann.get("appeal_status", ""),
        "notes":            ann.get("notes", ""),
        "updated_at":       ann.get("updated_at", ""),
    }


# ─── Data assembly ──────────────────────────────────────────────────────────

@st.cache_data(ttl=900, show_spinner=False)
def _build_ban_register() -> pd.DataFrame:
    """One row per banned party — outstanding, ageing, last bill,
    cheque bounces, plus the raw ban fields. Non-cached slice of the
    already-cached ledger + last-bill + cheques so this call is nearly
    free on a warm session."""
    ledger = _load_ledger()
    if ledger.empty:
        return pd.DataFrame()
    banned_rows = ledger[ledger["BannedByAssoc"] == "Y"]
    if banned_rows.empty:
        return pd.DataFrame()

    # Owning-ness comes from the unpaid FIFO frame
    unpaid = _fifo_unpaid(ledger, pd.Timestamp(date.today()))
    unpaid_agg = (unpaid.groupby("PartyID", observed=True)
                        .agg(Outstanding=("Remaining", "sum"),
                             OldestBillAge=("AgeDays", "max"))
                        .reset_index()) if not unpaid.empty else pd.DataFrame(
                            columns=["PartyID", "Outstanding", "OldestBillAge"])

    # First row per banned party for the descriptive fields
    party_meta = (banned_rows.sort_values("VoucherDate", ascending=False)
                             .drop_duplicates("PartyID")[
                    ["PartyID", "PartyName", "AssoBan_Type",
                     "AssoBan_Description", "AssoBilling_YN",
                     "PartyDefaultCD", "LicenseTypeID"]])

    df = party_meta.merge(unpaid_agg, on="PartyID", how="left")
    df["Outstanding"]   = df["Outstanding"].fillna(0.0)
    df["OldestBillAge"] = df["OldestBillAge"].fillna(0)

    # Enrich with last-bill and cheque-bounce info
    lb = _load_last_bill_per_party()
    if not lb.empty:
        df = df.merge(
            lb[["PartyID", "LastBillDate", "DaysSinceLastBill"]],
            on="PartyID", how="left")
    else:
        df["LastBillDate"] = pd.NaT
        df["DaysSinceLastBill"] = 0

    cheques = _load_cheque_returns_per_party()
    if not cheques.empty:
        df = df.merge(
            cheques[["PartyID", "ReturnCount", "TotalReturnedAmt",
                     "DaysSinceLastReturn"]],
            on="PartyID", how="left")
    else:
        df["ReturnCount"] = 0
        df["TotalReturnedAmt"] = 0.0
        df["DaysSinceLastReturn"] = -1

    df["ReturnCount"] = df["ReturnCount"].fillna(0).astype(int)
    df["TotalReturnedAmt"] = df["TotalReturnedAmt"].fillna(0.0)
    df["DaysSinceLastReturn"] = df["DaysSinceLastReturn"].fillna(-1).astype(int)
    df["DaysSinceLastBill"] = df["DaysSinceLastBill"].fillna(-1).astype(int)

    df["ReasonLabel"] = (df["AssoBan_Type"].fillna("").map(BAN_REASON_MAP)
                                                     .fillna("Other"))

    return df.sort_values("Outstanding", ascending=False).reset_index(drop=True)


# ─── UI sections ────────────────────────────────────────────────────────────

def _section_summary(df: pd.DataFrame) -> None:
    total    = len(df)
    owing    = df[df["Outstanding"] > 0.5]
    owing_n  = len(owing)
    exposure = float(owing["Outstanding"].sum())
    biggest  = owing.iloc[0] if not owing.empty else None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total banned parties", f"{total:,}")
    c2.metric("Banned & still owing", f"{owing_n:,}")
    c3.metric("Exposure", format_inr(exposure))
    if biggest is not None:
        c4.metric("Biggest single exposure",
                  format_inr(float(biggest["Outstanding"])),
                  delta=biggest["PartyName"][:22],
                  delta_color="off")
    else:
        c4.metric("Biggest single exposure", "—")


def _section_by_reason(df: pd.DataFrame) -> None:
    st.markdown("##### Breakdown by ban reason")
    if df.empty:
        st.info("No banned parties on record."); return
    by_r = (df.groupby(["AssoBan_Type", "ReasonLabel"], observed=True)
              .agg(Parties=("PartyID", "nunique"),
                   Exposure=("Outstanding", "sum"))
              .reset_index()
              .sort_values("Exposure", ascending=False))
    by_r["Exposure_Cr"] = by_r["Exposure"] / 1e7
    disp = by_r[["AssoBan_Type", "ReasonLabel", "Parties",
                 "Exposure_Cr"]].rename(columns={
        "AssoBan_Type": "Code", "ReasonLabel": "Reason",
        "Exposure_Cr":  "Exposure (₹ Cr)"})
    st.dataframe(
        disp.style.format({"Exposure (₹ Cr)": "{:.2f}"}),
        use_container_width=True, hide_index=True)


def _section_register_with_annotations(df: pd.DataFrame) -> None:
    st.markdown("##### Full register (editable annotations)")
    if df.empty:
        st.info("No banned parties on record."); return

    # Filter controls
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        show_owing_only = st.checkbox(
            "Only parties with outstanding balance",
            value=True, key="ban_only_owing")
    with fc2:
        min_expo_l = st.number_input(
            "Min exposure (₹ Lakh)",
            min_value=0.0, step=1.0, value=0.0,
            key="ban_min_expo",
            help="Hide parties with less than this exposure. "
                 "Set to 0 to see everyone.")
    with fc3:
        reason_options = ["All"] + sorted(df["ReasonLabel"].unique().tolist())
        picked_reason = st.selectbox(
            "Reason filter", reason_options, key="ban_reason_pick")

    view = df.copy()
    if show_owing_only:
        view = view[view["Outstanding"] > 0.5]
    if min_expo_l > 0:
        view = view[view["Outstanding"] >= min_expo_l * 1e5]
    if picked_reason != "All":
        view = view[view["ReasonLabel"] == picked_reason]

    if view.empty:
        st.info("No parties match the current filters.")
        return

    # Merge current annotations for display
    ann_all = _load_annotations()
    view = view.copy()
    view["Expected release"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("expected_release", ""))
    view["Appeal status"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("appeal_status", ""))
    view["Notes"] = view["PartyID"].map(
        lambda pid: ann_all.get(pid, {}).get("notes", ""))

    # Compact editable table via st.data_editor.
    # Only the three annotation columns are editable — everything else
    # is read-only (comes from the ERP).
    editable = view[[
        "PartyID", "PartyName", "ReasonLabel", "Outstanding",
        "OldestBillAge", "DaysSinceLastBill", "ReturnCount",
        "AssoBan_Description", "Expected release", "Appeal status", "Notes",
    ]].rename(columns={
        "PartyName":            "Party",
        "ReasonLabel":          "Reason",
        "Outstanding":          "Outstanding ₹",
        "OldestBillAge":        "Oldest bill (d)",
        "DaysSinceLastBill":    "Days silent",
        "ReturnCount":          "Cheque returns",
        "AssoBan_Description":  "ERP note",
    })

    edited = st.data_editor(
        editable,
        hide_index=True,
        use_container_width=True,
        height=520,
        disabled=["PartyID", "Party", "Reason", "Outstanding ₹",
                  "Oldest bill (d)", "Days silent", "Cheque returns",
                  "ERP note"],
        column_config={
            "PartyID": st.column_config.Column("PartyID", width="small"),
            "Outstanding ₹": st.column_config.NumberColumn(
                "Outstanding ₹", format="₹%.0f"),
            "Oldest bill (d)": st.column_config.NumberColumn(
                "Oldest bill (d)", format="%d"),
            "Days silent": st.column_config.NumberColumn(
                "Days silent", format="%d",
                help="Days since the last sales bill."),
            "Cheque returns": st.column_config.NumberColumn(
                "Cheque returns", format="%d"),
            "Expected release": st.column_config.TextColumn(
                "Expected release", help="Free-text, e.g. '15-Aug' "
                "or 'awaiting cheque'."),
            "Appeal status": st.column_config.SelectboxColumn(
                "Appeal status", options=APPEAL_STATUSES),
            "Notes": st.column_config.TextColumn(
                "Notes", help="Owner notes — visible only to whoever "
                "opens this dashboard."),
        },
        key="ban_editor",
    )

    # Save changes back to disk
    if st.button("💾 Save annotations", type="primary", key="ban_save"):
        ann_all = _load_annotations()
        touched = 0
        today_iso = date.today().isoformat()
        for _, row in edited.iterrows():
            pid = row["PartyID"]
            new = {
                "expected_release": str(row.get("Expected release", "") or ""),
                "appeal_status":    str(row.get("Appeal status", "") or ""),
                "notes":            str(row.get("Notes", "") or ""),
            }
            old = ann_all.get(pid, {})
            if any(old.get(k, "") != new[k] for k in new):
                new["updated_at"] = today_iso
                ann_all[pid] = new
                touched += 1
        _save_annotations(ann_all)
        st.success(f"Saved annotations for {touched} party(ies).")
        st.rerun()


def _section_priority_targets(df: pd.DataFrame) -> None:
    """Cross-list: banned parties that are ALSO ghosted or bouncing —
    these are the truly-dead accounts the owner should write off or
    escalate first."""
    st.markdown("##### Priority action: banned + ghosted / bouncing")
    if df.empty:
        return
    ghosted_or_bouncing = df[
        (df["Outstanding"] > 0.5)
        & ((df["DaysSinceLastBill"] >= 90) | (df["ReturnCount"] > 0))
    ].copy()
    if ghosted_or_bouncing.empty:
        st.caption("Every banned-and-owing party is either still billing "
                   "or has no cheque bounces on record. Good sign.")
        return
    ghosted_or_bouncing["Signal"] = ghosted_or_bouncing.apply(
        lambda r: " · ".join(filter(None, [
            f"👻 silent {int(r['DaysSinceLastBill'])}d"
                if r["DaysSinceLastBill"] >= 90 else "",
            f"📉 {int(r['ReturnCount'])} bounces"
                if r["ReturnCount"] > 0 else "",
        ])), axis=1)
    disp = ghosted_or_bouncing[[
        "PartyName", "ReasonLabel", "Outstanding", "Signal",
    ]].rename(columns={
        "PartyName": "Party", "ReasonLabel": "Reason",
        "Outstanding": "Owed ₹",
    }).sort_values("Owed ₹", ascending=False)
    st.dataframe(
        disp.style.format({"Owed ₹": format_inr}),
        use_container_width=True, hide_index=True, height=360)
    st.caption(f"**{len(ghosted_or_bouncing):,} parties** are banned "
               f"AND silent 90d+ OR have cheque bounces. Total exposure: "
               f"**{format_inr(float(ghosted_or_bouncing['Outstanding'].sum()))}**. "
               f"These are your write-off / legal-action candidates.")


# ─── Main render ────────────────────────────────────────────────────────────

def render() -> None:
    st.title("🚫 Trader Association Bans")
    st.caption(
        "Every party with `BannedByAssoc = Y` on MsPartyMaster, enriched "
        "with FIFO outstanding, days-silent, and cheque-bounce history. "
        "The annotation columns (expected release, appeal status, notes) "
        "are yours to edit — stored in `data/ban_annotations.json` and "
        "committed with the repo."
    )
    st.divider()

    try:
        df = _build_ban_register()
    except Exception as e:
        st.warning(f"Ban register unavailable: `{e}`")
        return

    if df.empty:
        st.info("No banned parties on record. 🎉")
        return

    _section_summary(df)
    st.divider()
    _section_by_reason(df)
    st.divider()
    _section_priority_targets(df)
    st.divider()
    _section_register_with_annotations(df)
