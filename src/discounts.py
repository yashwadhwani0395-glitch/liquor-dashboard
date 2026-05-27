"""src/discounts.py — Discounts & Schemes page.

Two things the business cares about, both stored as SERVICE-ITEM lines
(ItemID like 'S%') on the same vouchers as the product lines, in
TrVocItem.TotalAmount (negative = discount given, positive = charge added).

Authoritative classification (verified from the ERP, NOT name-guessing):
  • Every discount / scheme service item posts to GL head 000004 (SALES) —
    PostAccSaleID on MsServiceItemMaster. The non-discount charges each have
    their own heads (TCS→000218, VAT→000409, GST→000528, Transport→000040,
    Service&Maint→000529) and are therefore excluded automatically.
  • CASH DISCOUNT = the service items brands point to via
    MsBrandMaster.CashDiscountID (currently S00002 = CD 2%, S00008 = CD 1%).
    Given to customers; comes straight off our margin.
  • TRADE / SCHEME DISCOUNT = every OTHER value-reducing (negative) service
    line on head 000004. These are the per-brand paper-scheme discounts we
    CLAIM BACK from the principal. Each month's scheme is its own service
    item (e.g. "AUG TRADE DISC CANNON"); the brand's TradeDiscountID pointer
    is re-pointed to that month's item.

Section 1 — Cash Discounts: per party, CD given vs how fast they actually pay
  (snapshot DSO = outstanding ÷ daily sales). Flags parties who TAKE the cash
  discount but still pay slowly — i.e. the CD isn't buying us faster money.

Section 2 — Trade / Scheme Discounts: per principal × month (claim recoverable
  from the company), drill-down to brand.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import safe_section, CASES_SQL_EXPR as _CASES
from src.segments import _segment_for, SEGMENT_ORDER, PRINCIPALS

# GL head that all sales-contra (discount/scheme) service items post to.
_DISCOUNT_GL_HEAD = "000004"

# MIS sales transaction types (same set used across the dashboard).
_SALES_TYPES: tuple[int, ...] = (
    18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53,
)

_PRINCIPAL_NAMES = {
    "C00025": "United Spirits", "C00039": "United Breweries",
    "C00040": "Diageo", "C00056": "Brown-Forman",
}


def _cr(x: float) -> str:
    return f"₹{x / 1e7:,.2f} Cr"


def _lk(x: float) -> str:
    if abs(x) >= 1e7:
        return f"₹{x/1e7:,.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x/1e5:,.2f} L"
    return f"₹{x:,.0f}"


# ─────────────────────────────────────────────────────────────────────────
# Authoritative cash-discount item set (from brand pointers)
# ─────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def _load_cd_item_ids() -> tuple[str, ...]:
    """Service items used as a brand's CashDiscountID = the cash-discount set."""
    df = run_query("""
        SELECT DISTINCT LTRIM(RTRIM(CashDiscountID)) AS SItemID
        FROM MsBrandMaster
        WHERE LTRIM(RTRIM(ISNULL(CashDiscountID,''))) <> ''
          AND LTRIM(RTRIM(CashDiscountID)) <> '0'
    """)
    if df.empty:
        return ("S00002", "S00008")   # known fallback
    ids = tuple(sorted({str(s).strip() for s in df["SItemID"] if str(s).strip()}))
    return ids or ("S00002", "S00008")


# ─────────────────────────────────────────────────────────────────────────
# Section 1 — Cash discount by party + payment speed
# ─────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def _load_cd_by_party(months: int, cd_ids: tuple[str, ...]) -> pd.DataFrame:
    cd_ph = ",".join("'%s'" % i for i in cd_ids)
    sales_ph = ",".join(str(t) for t in _SALES_TYPES)

    last_debtor = """
        JOIN (
            SELECT TransTypeID, VoucherNo, FinancialYear, PartyID FROM (
                SELECT TransTypeID, VoucherNo, FinancialYear, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo, FinancialYear
                                          ORDER BY id_key DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID LIKE 'D%' AND DrCrIndicator='D'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
           AND d.FinancialYear = h.FinancialYear
    """

    # CD given per party (CD service lines on sales bills; negative → discount)
    cd = run_query(f"""
        SELECT d.PartyID,
               SUM(CAST(vi.TotalAmount AS float))  AS CDRaw,
               COUNT(DISTINCT h.VoucherNo)         AS CDBills
        FROM TrVocItem vi
        JOIN TrVocHead h
            ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
           AND h.FinancialYear = vi.FinancialYear
        {last_debtor}
        WHERE vi.ItemID IN ({cd_ph})
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          AND h.Cancelled = 'N'
        GROUP BY d.PartyID
    """)
    if cd.empty:
        return cd
    cd["CDGiven"] = -pd.to_numeric(cd["CDRaw"], errors="coerce").fillna(0.0)
    cd = cd[cd["CDGiven"] > 0.0].copy()
    if cd.empty:
        return cd

    # Period credit sales per party (product lines)
    sales = run_query(f"""
        SELECT d.PartyID, SUM(CAST(vi.TotalAmount AS float)) AS Sales
        FROM TrVocItem vi
        JOIN TrVocHead h
            ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
           AND h.FinancialYear = vi.FinancialYear
        {last_debtor}
        WHERE vi.ItemID LIKE 'I%' AND h.TransTypeID IN ({sales_ph})
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          AND h.Cancelled = 'N'
        GROUP BY d.PartyID
    """)

    # Current outstanding per party (open sales-bill balances)
    os_df = run_query(f"""
        SELECT PartyID, SUM(CAST(BalanceAmount AS float)) AS OS
        FROM TrVocDetail
        WHERE PartyID LIKE 'D%' AND DrCrIndicator='D'
          AND CAST(BalanceAmount AS float) > 0.5
          AND TransTypeID IN ({sales_ph})
        GROUP BY PartyID
    """)

    pm = run_query("""
        SELECT PartyID, PartyName,
               CAST(ISNULL(CreditDays,0) AS int)   AS CreditDays,
               CAST(ISNULL(CDPercent,0) AS float)  AS CDPercent
        FROM MsPartyMaster WHERE PartyID LIKE 'D%'
    """)

    m = cd.merge(sales, on="PartyID", how="left") \
          .merge(os_df, on="PartyID", how="left") \
          .merge(pm, on="PartyID", how="left")
    for c in ("Sales", "OS", "CreditDays", "CDPercent"):
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0)
    m["PartyName"] = m["PartyName"].fillna(m["PartyID"]).astype(str).str.strip()

    days = months * 30.0
    # Snapshot DSO: outstanding ÷ average daily sales over the window.
    m["DSO"] = m.apply(
        lambda r: (r["OS"] / (r["Sales"] / days)) if r["Sales"] > 0 else float("nan"),
        axis=1)
    # CD as % of sales (how much margin we're giving this party)
    m["CDpctSales"] = m.apply(
        lambda r: (r["CDGiven"] / r["Sales"] * 100.0) if r["Sales"] > 0 else 0.0,
        axis=1)
    return m


def _section_cash_discount(months: int) -> None:
    st.subheader("💸 Cash Discounts — given to customers, off our margin")
    cd_ids = _load_cd_item_ids()
    st.caption(
        f"Cash-discount service items (from each brand's CashDiscountID): "
        f"**{', '.join(cd_ids)}**. Last {months} months. "
        f"**DSO** = current outstanding ÷ average daily sales — a snapshot of "
        f"how many days their money actually takes. A party that takes the cash "
        f"discount but has DSO well above their credit-day terms isn't paying "
        f"fast enough to justify it."
    )
    df = _load_cd_by_party(months, cd_ids)
    if df.empty:
        st.info("No cash-discount activity in the window."); return

    tot_cd = float(df["CDGiven"].sum())
    n_parties = int(len(df))
    # "Not justified": takes CD but DSO materially exceeds their terms.
    slow = df[(df["CreditDays"] > 0) & (df["DSO"].notna())
              & (df["DSO"] > df["CreditDays"] + 7)].copy()
    slow_cd = float(slow["CDGiven"].sum())

    c1, c2, c3 = st.columns(3)
    c1.metric("Total cash discount given", _lk(tot_cd))
    c2.metric("Parties receiving CD", f"{n_parties:,}")
    c3.metric("CD to slow payers", _lk(slow_cd),
              help="Cash discount given to parties whose DSO exceeds their "
                   "credit-day terms by >7 days — discount not buying fast money.")

    show = df.sort_values("CDGiven", ascending=False).head(200).copy()
    show = show[["PartyName", "CDGiven", "CDpctSales", "Sales", "OS",
                 "DSO", "CreditDays", "CDBills"]].rename(columns={
        "PartyName": "Party", "CDGiven": "CD given", "CDpctSales": "CD % of sales",
        "Sales": "Sales (window)", "OS": "Outstanding", "DSO": "DSO (days)",
        "CreditDays": "Terms (days)", "CDBills": "Bills"})

    def _flag_dso(row):
        try:
            if row["Terms (days)"] > 0 and pd.notna(row["DSO (days)"]) \
               and row["DSO (days)"] > row["Terms (days)"] + 7:
                return ["color:#dc2626;font-weight:700" if c == "DSO (days)" else ""
                        for c in show.columns]
        except Exception:
            pass
        return ["" for _ in show.columns]

    sty = (show.style
           .format({"CD given": _lk, "CD % of sales": "{:.2f}%",
                    "Sales (window)": _lk, "Outstanding": _lk,
                    "DSO (days)": "{:,.0f}", "Terms (days)": "{:,.0f}",
                    "Bills": "{:,.0f}"}, na_rep="—")
           .apply(_flag_dso, axis=1))
    st.dataframe(sty, use_container_width=True, hide_index=True, height=460)
    st.download_button(
        "⬇️ Download cash-discount-by-party",
        show.to_csv(index=False).encode("utf-8-sig"),
        file_name="cash_discount_by_party.csv", mime="text/csv", key="cd_dl")


# ─────────────────────────────────────────────────────────────────────────
# Section 2 — Trade / scheme discount by principal × month (claim from company)
# ─────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def _load_trade_disc(months: int, cd_ids: tuple[str, ...]) -> pd.DataFrame:
    """One row per (month, principal, dominant-brand, party) with the trade /
    scheme discount on that voucher group. Segment is derived in pandas from
    (CompanyID, BrandName) using the Segment-Analysis classifier so the segment
    names match that page exactly."""
    cd_ph = ",".join("'%s'" % i for i in cd_ids)
    df = run_query(f"""
        WITH bill_brand AS (
            SELECT TransTypeID, VoucherNo, FinancialYear, CompanyID, BrandName FROM (
                SELECT vi.TransTypeID, vi.VoucherNo, vi.FinancialYear,
                       b.CompanyID, b.BrandName,
                       ROW_NUMBER() OVER (
                           PARTITION BY vi.TransTypeID, vi.VoucherNo, vi.FinancialYear
                           ORDER BY SUM(CAST(vi.TotalAmount AS float)) DESC) AS rn
                FROM TrVocItem vi
                JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
                JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
                WHERE vi.ItemID LIKE 'I%'
                GROUP BY vi.TransTypeID, vi.VoucherNo, vi.FinancialYear,
                         b.CompanyID, b.BrandName
            ) z WHERE rn = 1
        ),
        last_debtor AS (
            SELECT TransTypeID, VoucherNo, FinancialYear, PartyID FROM (
                SELECT TransTypeID, VoucherNo, FinancialYear, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo, FinancialYear
                                          ORDER BY id_key DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID LIKE 'D%' AND DrCrIndicator='D'
            ) x WHERE rn = 1
        )
        SELECT FORMAT(h.VoucherDate, 'yyyy-MM')          AS Mon,
               ISNULL(bb.CompanyID, '?')                 AS CompanyID,
               ISNULL(bb.BrandName, '(unattributed)')    AS BrandName,
               ISNULL(ld.PartyID, '?')                   AS PartyID,
               ISNULL(pm.PartyName, ld.PartyID)          AS PartyName,
               SUM(CAST(vi.TotalAmount AS float))        AS TradeRaw
        FROM TrVocItem vi
        JOIN MsServiceItemMaster s
            ON s.SItemID = vi.ItemID AND s.PostAccSaleID = '{_DISCOUNT_GL_HEAD}'
        JOIN TrVocHead h
            ON h.TransTypeID = vi.TransTypeID AND h.VoucherNo = vi.VoucherNo
           AND h.FinancialYear = vi.FinancialYear
        LEFT JOIN bill_brand bb
            ON bb.TransTypeID = h.TransTypeID AND bb.VoucherNo = h.VoucherNo
           AND bb.FinancialYear = h.FinancialYear
        LEFT JOIN last_debtor ld
            ON ld.TransTypeID = h.TransTypeID AND ld.VoucherNo = h.VoucherNo
           AND ld.FinancialYear = h.FinancialYear
        LEFT JOIN MsPartyMaster pm ON pm.PartyID = ld.PartyID
        WHERE vi.ItemID NOT IN ({cd_ph})
          AND CAST(vi.TotalAmount AS float) < 0    -- value-reducing = discount
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          AND h.Cancelled = 'N'
        GROUP BY FORMAT(h.VoucherDate, 'yyyy-MM'),
                 ISNULL(bb.CompanyID, '?'), ISNULL(bb.BrandName, '(unattributed)'),
                 ISNULL(ld.PartyID, '?'), ISNULL(pm.PartyName, ld.PartyID)
    """)
    if df.empty:
        return df
    df["TradeDisc"] = -pd.to_numeric(df["TradeRaw"], errors="coerce").fillna(0.0)
    df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    df["PartyName"] = df["PartyName"].astype(str).str.strip()
    df["Segment"]   = df.apply(
        lambda r: _segment_for(r["CompanyID"], r["BrandName"])
                  if r["CompanyID"] in _PRINCIPAL_NAMES else "Other",
        axis=1)
    return df


def _segment_month_matrix(d: pd.DataFrame, months_order: list[str]) -> pd.DataFrame:
    """Pivot: rows = Principal · Segment, cols = months, + TOTAL."""
    g = (d.groupby(["Principal", "Segment", "Mon"])["TradeDisc"].sum()
           .unstack("Mon").reindex(columns=months_order).fillna(0.0))
    g["TOTAL"] = g.sum(axis=1)
    # Order by principal display order, then segment display order.
    prin_order = [name for name, _ in PRINCIPALS] + ["Other"]
    cid_by_name = {name: cid for name, cid in PRINCIPALS}

    def _seg_rank(principal, segment):
        cid = cid_by_name.get(principal, "")
        order = SEGMENT_ORDER.get(cid, [])
        return order.index(segment) if segment in order else 99
    g = g.reset_index()
    g["_p"] = g["Principal"].map(lambda p: prin_order.index(p)
                                 if p in prin_order else 99)
    g["_s"] = g.apply(lambda r: _seg_rank(r["Principal"], r["Segment"]), axis=1)
    g = g.sort_values(["_p", "_s"]).drop(columns=["_p", "_s"])
    return g


def _section_trade_discount(months: int) -> None:
    st.subheader("🏷️ Trade / Scheme Discounts — claimable from the company")
    st.caption(
        "Per-brand paper-scheme discounts (every value-reducing service line on "
        f"GL head {_DISCOUNT_GL_HEAD}/SALES, excluding cash discount). Each line "
        "is tagged to a **segment** via the dominant brand on its voucher (same "
        "segments as the Segment Analysis page) and to the **party** on the bill. "
        "These are the amounts we claim back from the principal."
    )
    cd_ids = _load_cd_item_ids()
    df = _load_trade_disc(months, cd_ids)
    if df.empty:
        st.info("No trade/scheme-discount activity in the window."); return

    months_order = sorted(df["Mon"].unique())
    tot = float(df["TradeDisc"].sum())
    st.metric(f"Total trade/scheme discount (last {months} mo)", _cr(tot),
              help="Total claimable from the principals over the window.")

    # ── Spend per segment × month ──
    st.markdown("##### Spend per segment × month")
    seg_mat = _segment_month_matrix(df, months_order)
    st.dataframe(
        seg_mat.style.format({c: (lambda v: _lk(v)) for c in
                              list(months_order) + ["TOTAL"]}),
        use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download segment × month",
        seg_mat.to_csv(index=False).encode("utf-8-sig"),
        file_name="trade_discount_segment_month.csv", mime="text/csv",
        key="td_seg_dl")

    # ── Party drill-down: pick a party → its segment × month matrix ──
    st.markdown("##### Drill down by party")
    party_tot = (df.groupby(["PartyName"])["TradeDisc"].sum()
                   .sort_values(ascending=False))
    party_tot = party_tot[party_tot > 0]
    if party_tot.empty:
        st.caption("No party-attributable trade discount."); return
    opts = [f"{name}  ·  {_lk(v)}" for name, v in party_tot.items()]
    names = list(party_tot.index)
    pick = st.selectbox(
        "Party (sorted by total trade discount received)", opts,
        key="td_party_pick")
    chosen = names[opts.index(pick)]
    pdf = df[df["PartyName"] == chosen]
    pm = (pdf.groupby(["Principal", "Segment", "Mon"])["TradeDisc"].sum()
            .unstack("Mon").reindex(columns=months_order).fillna(0.0))
    pm["TOTAL"] = pm.sum(axis=1)
    pm = pm[pm["TOTAL"] > 0].reset_index()
    st.caption(f"**{chosen}** — trade/scheme discount by segment × month")
    st.dataframe(
        pm.style.format({c: (lambda v: _lk(v)) for c in
                         list(months_order) + ["TOTAL"]}),
        use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────
# Section 3 — Lifting vs discount, per principal × month
# ─────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def _load_lifting(months: int) -> pd.DataFrame:
    """Keg-aware cases sold (lifting) per principal × month — MIS basis
    (FY-dedup, SALES_TYPES, free goods included), same as Segment Analysis."""
    type_ph = ",".join(str(t) for t in _SALES_TYPES)
    cids = ",".join("'%s'" % c for c in _PRINCIPAL_NAMES)
    df = run_query(f"""
        SELECT b.CompanyID,
               FORMAT(h.VoucherDate, 'yyyy-MM')  AS Mon,
               SUM({_CASES})                     AS Cases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID AND vi.VoucherNo = h.VoucherNo
            AND vi.ItemID LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months}, GETDATE())
          AND b.CompanyID IN ({cids})
        GROUP BY b.CompanyID, FORMAT(h.VoucherDate, 'yyyy-MM')
    """)
    if df.empty:
        return df
    df["Cases"]     = pd.to_numeric(df["Cases"], errors="coerce").fillna(0.0)
    df["Principal"] = df["CompanyID"].map(_PRINCIPAL_NAMES).fillna("Other")
    return df


def _section_lifting_vs_discount(months: int) -> None:
    st.subheader("📦 Lifting vs trade discount — per principal × month")
    st.caption(
        "Lifting = keg-aware cases sold (MIS basis). Discount = trade/scheme "
        "discount on those principals' brands. ₹/case = discount ÷ lifting — "
        "how much scheme support each case carried that month."
    )
    cd_ids = _load_cd_item_ids()
    lift = _load_lifting(months)
    disc = _load_trade_disc(months, cd_ids)
    if lift.empty:
        st.info("No lifting data in the window."); return

    months_order = sorted(set(lift["Mon"]).union(
        set(disc["Mon"]) if not disc.empty else set()))
    prin_order = [n for n, _ in PRINCIPALS]

    lift_pv = (lift.groupby(["Principal", "Mon"])["Cases"].sum()
                   .unstack("Mon").reindex(index=prin_order, columns=months_order)
                   .fillna(0.0))
    if disc.empty:
        disc_pv = lift_pv * 0.0
    else:
        disc_pv = (disc[disc["Principal"].isin(prin_order)]
                   .groupby(["Principal", "Mon"])["TradeDisc"].sum()
                   .unstack("Mon").reindex(index=prin_order, columns=months_order)
                   .fillna(0.0))
    rate_pv = (disc_pv / lift_pv.replace(0.0, float("nan")))

    def _with_total(pv, total_fn):
        out = pv.copy()
        out["TOTAL"] = total_fn(out)
        return out.reset_index().rename(columns={"index": "Principal"})

    st.markdown("**Lifting (cases)**")
    lc = _with_total(lift_pv, lambda o: o.sum(axis=1))
    st.dataframe(lc.style.format({c: "{:,.0f}" for c in
                 list(months_order) + ["TOTAL"]}),
                 use_container_width=True, hide_index=True)

    st.markdown("**Trade / scheme discount (₹)**")
    dc = _with_total(disc_pv, lambda o: o.sum(axis=1))
    st.dataframe(dc.style.format({c: (lambda v: _lk(v)) for c in
                 list(months_order) + ["TOTAL"]}),
                 use_container_width=True, hide_index=True)

    st.markdown("**Discount per case (₹/cs)**")
    # Total ₹/cs = total discount ÷ total lifting (not the row-mean of rates)
    rate_tot = (disc_pv.sum(axis=1) / lift_pv.sum(axis=1).replace(0.0, float("nan")))
    rc = rate_pv.copy()
    rc["TOTAL"] = rate_tot
    rc = rc.reset_index().rename(columns={"index": "Principal"})
    st.dataframe(rc.style.format({c: "₹{:,.1f}" for c in
                 list(months_order) + ["TOTAL"]}, na_rep="—"),
                 use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download lifting vs discount",
        dc.merge(lc, on="Principal", suffixes=("_disc", "_cases"))
          .to_csv(index=False).encode("utf-8-sig"),
        file_name="lifting_vs_discount.csv", mime="text/csv", key="lvd_dl")


# ═══════════════════════════════════════════════════════════════════════════
def render() -> None:
    st.title("🏷️ Discounts & Schemes")
    st.caption(
        "Cash discounts we give customers (off our margin) and trade/scheme "
        "discounts we claim back from the principals — both read live from the "
        "ERP service-item lines (GL head 000004 / SALES)."
    )
    months = st.radio("Look-back window", [6, 12, 18], index=1, horizontal=True,
                      key="disc_months", format_func=lambda m: f"Last {m} months")
    st.divider()
    safe_section("Cash Discounts", _section_cash_discount, months)
    st.divider()
    safe_section("Lifting vs Discount", _section_lifting_vs_discount, months)
    st.divider()
    safe_section("Trade / Scheme Discounts", _section_trade_discount, months)
