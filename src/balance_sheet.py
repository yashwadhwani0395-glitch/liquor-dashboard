"""src/balance_sheet.py — Balance Sheet, built from the ERP account-head tables.

Mapping (reverse-engineered from the Teknik BS report):
  MsAccountHead.MainHeadID → XMainheadTypeMainhead.MainHeadType
      type 1 = APPLICATION OF FUNDS  → ASSETS
      type 2 = SOURCES OF FUNDS      → LIABILITIES
  MsAccountHead.SubHeadID  → MsCodeMaster.CodeDescription (the BS group:
      Secured Loans / Unsecured Loans / Sundry Creditor / Provisions /
      Share Capital / Reserves | Fixed Assets / Investments / Loans & Advances /
      Current Assets / Cash on Hand …)
  Value = MsAcHeadOpening.CloseBalTmp (live) or CloseBal (committed).

Two ERP-integrity audits the dashboard HIGHLIGHTS:
  1. Carry-forward: this-FY 1-Apr OpenBal must equal last-FY 31-Mar close
     (captured in data/bs_baseline.json from the year-end BS). Any head where
     they differ is flagged — the ERP failed to carry the balance forward.
  2. Closing stock not posted: the ledger Closing-Stock head reads ~0 intra-year
     while real inventory is ~tens of crores — flagged, and plugged from the
     live inventory value so the sheet balances.
  3. Ledger identity: CloseBal must equal OpenBal + TotalDebit − TotalCredit.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd
import streamlit as st

from db import run_query

_BS_BASELINE_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                                 "bs_baseline.json")
_GL_CLOSING_STOCK = "000007"   # CLOSING STOCK - (B/S.)


def _cr(x: float) -> str:
    return f"₹{x / 1e7:,.2f} Cr"


@st.cache_data(ttl=600, show_spinner=False)
def _load_bs_heads() -> pd.DataFrame:
    """Every Balance-Sheet account head with its balances + group/section."""
    df = run_query("""
        SELECT a.AccHeadID, a.AccName,
               xt.MainHeadType,
               ISNULL(sh.CodeDescription, '(ungrouped)') AS Subgroup,
               CAST(o.OpenBal      AS float) AS OpenBal,
               CAST(o.CloseBal     AS float) AS CloseBal,
               CAST(o.CloseBalTmp  AS float) AS CloseBalTmp,
               CAST(o.TotalDebit   AS float) AS TotDr,
               CAST(o.TotalCredit  AS float) AS TotCr
        FROM MsAccountHead a
        JOIN MsAcHeadOpening o          ON o.AccHeadID  = a.AccHeadID
        JOIN XMainheadTypeMainhead xt   ON xt.MainheadID = a.MainHeadID
        LEFT JOIN MsCodeMaster sh       ON sh.CodeValue  = a.SubHeadID
        WHERE xt.MainHeadType IN (1, 2)
    """)
    if df.empty:
        return df
    for c in ("OpenBal", "CloseBal", "CloseBalTmp", "TotDr", "TotCr"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["Section"] = df["MainHeadType"].map({1: "ASSETS", 2: "LIABILITIES"})
    df["AccName"] = df["AccName"].astype(str).str.strip()
    return df


def _load_bs_baseline() -> tuple[str, dict]:
    """Last-FY 31-Mar close per head (from the year-end BS) for the
    carry-forward audit. Returns (close_date, {AccHeadID: close})."""
    try:
        with open(_BS_BASELINE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("close_date", "")), {
            str(k): float(v) for k, v in d.get("close_by_head", {}).items()}
    except Exception as exc:
        print(f"[balance_sheet] no BS baseline: {exc}", file=sys.stderr)
        return "", {}


@st.cache_data(ttl=600, show_spinner=False)
def _live_closing_stock_value() -> float:
    """Live inventory value (₹) to plug as Closing Stock. Values EVERY batch
    item (MsItemBatchOpening) at its landed rate — the same basis the ERP
    stock-value report uses — so it covers all SKUs (the S&S baseline only
    lists the top movers, so it under-values the total)."""
    df = run_query("""
        SELECT SUM(CASE WHEN im.BottlesPerCase > 0
                        THEN ISNULL(bo.ClosingQty,0) / CAST(im.BottlesPerCase AS float)
                             * ISNULL(im.ValuationCaseRate,0) ELSE 0 END) AS Val
        FROM MsItemBatchOpening bo
        JOIN MsItemMaster im ON im.ItemID = bo.ItemID
        WHERE bo.ItemID LIKE 'I%'
    """)
    if df.empty or pd.isna(df.iloc[0]["Val"]):
        return 0.0
    return float(df.iloc[0]["Val"])


# ═══════════════════════════════════════════════════════════════════════════
# RENDER
# ═══════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("📋 Balance Sheet")
    st.caption(
        "Built live from the ERP account-head tables (MsAccountHead + "
        "MsAcHeadOpening), grouped exactly like the Teknik report. Also audits "
        "the ERP: flags any head where the 1-April opening doesn't match last "
        "year's 31-March close, and where closing stock isn't posted."
    )
    df = _load_bs_heads()
    if df.empty:
        st.error("No account-head balances available."); return

    mode = st.radio(
        "Balances", ["Live (today)", "Committed"], horizontal=True,
        key="bs_mode",
        help="Live = CloseBalTmp (includes uncommitted entries). "
             "Committed = CloseBal (posted entries only).",
    )
    bal_col = "CloseBalTmp" if mode.startswith("Live") else "CloseBal"
    df["Bal"] = df[bal_col]

    stock_val   = _live_closing_stock_value()
    close_date, base = _load_bs_baseline()

    # ── Section totals ──
    assets_heads = float(df[df["Section"] == "ASSETS"]["Bal"].sum())
    liab_heads   = float(df[df["Section"] == "LIABILITIES"]["Bal"].sum())  # negative
    assets_total = assets_heads + stock_val            # plug closing stock
    liab_total   = -liab_heads                          # show as positive magnitude
    net_result   = assets_total - liab_total            # suspense / retained result

    # ─────────── INTEGRITY AUDIT ───────────
    alerts = []
    # 1. Carry-forward: OpenBal vs last-FY close
    cf = []
    if base:
        for r in df.itertuples():
            prev = base.get(r.AccHeadID)
            if prev is None:
                continue
            if abs(prev - r.OpenBal) > 1.0:
                cf.append((r.AccName, prev, r.OpenBal, r.OpenBal - prev))
    cf_total = sum(abs(d) for _, _, _, d in cf)
    if cf:
        alerts.append(
            f"🔁 **{len(cf)} heads** where the 1-Apr opening ≠ last year's "
            f"31-Mar close (carry-forward broken), net ₹{cf_total/1e7:,.2f} Cr "
            f"of differences — see the audit below."
        )
    # 2. Closing stock not posted to the ledger
    cs_head = float(df[df["AccHeadID"] == _GL_CLOSING_STOCK]["Bal"].sum())
    if stock_val > 1e6 and abs(cs_head) < 1e6:
        alerts.append(
            f"📦 Closing stock isn't posted in the ledger (head reads "
            f"₹{cs_head/1e7:,.2f} Cr) — plugged with the live inventory value "
            f"**{_cr(stock_val)}** so the sheet balances."
        )
    # 3. Ledger identity per head: CloseBal == OpenBal + Dr − Cr
    idn = df.copy()
    idn["Implied"] = idn["OpenBal"] + idn["TotDr"] - idn["TotCr"]
    bad_idn = idn[(idn["CloseBal"] - idn["Implied"]).abs() > 1.0]

    if alerts:
        st.error("**⚠️ ERP integrity issues detected:**\n\n- " + "\n- ".join(alerts))
    else:
        st.success("✅ No carry-forward or closing-stock integrity issues detected.")

    # ── Balance status ──
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Liabilities", _cr(liab_total))
    c2.metric("Total Assets", _cr(assets_total),
              help="Includes closing stock plugged from live inventory.")
    diff = assets_total - liab_total
    c3.metric("Net result / difference", _cr(diff),
              help="Assets − Liabilities. Represents accumulated profit/loss "
                   "and any timing/suspense not yet in Reserves.")
    st.divider()

    # ── Liabilities | Assets, grouped, expandable ──
    def _side(section: str, extra_rows: list | None = None):
        sub = df[df["Section"] == section].copy()
        sub["Mag"] = sub["Bal"].abs()
        rows = []
        for grp, g in sub.groupby("Subgroup"):
            rows.append((grp, float(g["Bal"].abs().sum()), g))
        rows.sort(key=lambda x: -x[1])
        for grp, tot, g in rows:
            with st.expander(f"**{grp}** — {_cr(tot)}", expanded=False):
                show = g[["AccName", "Bal"]].copy()
                show["Bal"] = show["Bal"].abs()
                show = show.sort_values("Bal", ascending=False).rename(
                    columns={"AccName": "Account", "Bal": "₹"})
                st.dataframe(
                    show.style.format({"₹": lambda v: _cr(v)}),
                    use_container_width=True, hide_index=True)
        for label, val in (extra_rows or []):
            st.markdown(f"&nbsp;&nbsp;**{label}** — {_cr(val)}")

    lc, ac = st.columns(2)
    with lc:
        st.subheader("Liabilities")
        st.caption(f"Total **{_cr(liab_total)}**")
        _side("LIABILITIES",
              [("Net result (P&L / retained)", net_result)] if net_result > 0 else [])
    with ac:
        st.subheader("Assets")
        st.caption(f"Total **{_cr(assets_total)}**")
        _side("ASSETS", [("Closing Stock (live inventory)", stock_val)])

    st.divider()

    # ── Carry-forward audit detail ──
    with st.expander(f"🔁 Carry-forward audit — {len(cf)} mismatched heads"
                     + (f" (vs 31-Mar-{close_date[:4]} close)" if close_date else "")):
        if not base:
            st.info("Upload the year-end BS (data/bs_baseline.json) to enable "
                    "this audit.")
        elif not cf:
            st.success("Every head's 1-Apr opening matches last year's close. ✅")
        else:
            cfd = pd.DataFrame(cf, columns=["Account", "LastFY 31-Mar close",
                                            "This-FY 1-Apr open", "Difference"])
            cfd = cfd.reindex(cfd["Difference"].abs().sort_values(ascending=False).index)
            st.caption("These heads should be IDENTICAL (a closing balance must "
                       "carry into the next year's opening). Where they differ, "
                       "the ERP year-end rollover is wrong — investigate.")
            st.dataframe(
                cfd.style.format({c: (lambda v: _cr(v)) for c in
                                  ["LastFY 31-Mar close", "This-FY 1-Apr open",
                                   "Difference"]}),
                use_container_width=True, hide_index=True, height=400)
            st.download_button(
                "⬇️ Download carry-forward exceptions",
                cfd.to_csv(index=False).encode("utf-8-sig"),
                file_name="bs_carryforward_exceptions.csv", mime="text/csv",
                key="bs_cf_dl")

    # ── Ledger-identity audit ──
    with st.expander(f"🧮 Ledger-identity audit — {len(bad_idn)} heads where "
                     "CloseBal ≠ Opening + Dr − Cr"):
        if bad_idn.empty:
            st.success("Every head's CloseBal = Opening + Debit − Credit. ✅")
        else:
            bd = bad_idn[["AccName", "OpenBal", "TotDr", "TotCr",
                          "CloseBal", "Implied"]].copy()
            bd["Gap"] = bd["CloseBal"] - bd["Implied"]
            bd = bd.reindex(bd["Gap"].abs().sort_values(ascending=False).index)
            st.dataframe(
                bd.style.format({c: (lambda v: _cr(v)) for c in
                                 ["OpenBal", "TotDr", "TotCr", "CloseBal",
                                  "Implied", "Gap"]}),
                use_container_width=True, hide_index=True, height=360)
