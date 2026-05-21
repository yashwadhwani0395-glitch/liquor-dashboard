"""Sales — Segment Analysis sub-tab.

Per-principal brand-group segments with trend metrics so the owner can spot
which segment is pulling volume down:
    L3M | L6M | LYSM | L3M-MTD | Current-MTD | MTD Δ% (vs L3M-MTD)

Case math is MIS-consistent with the rest of the dashboard:
  - keg-aware CASES_SQL_EXPR (50/30/20 LT volume conversion)
  - FY-dedup, SALES_TYPES, Cancelled='N'
  - free / scheme goods INCLUDED (stock physically sent; Rs0 revenue)

Segment classification is brand-pattern based (owner-confirmed).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from db import run_query
from utils.helpers import CASES_SQL_EXPR as _CASES

SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# Display order matters (first listed shows on top)
PRINCIPALS: list[tuple[str, str]] = [
    ("United Breweries", "C00039"),
    ("United Spirits",   "C00025"),
    ("Diageo",           "C00040"),
    ("Brown-Forman",     "C00056"),
]

# Segment display order per principal (so tables read economy→premium etc.)
SEGMENT_ORDER: dict[str, list[str]] = {
    "C00025": ["Mid Prestige", "Upper Prestige", "Other"],
    "C00040": ["Smirnoff (Vodka + Flavours)", "Black Label", "Red Label", "All Others"],
    "C00039": ["Economy", "Mainstream", "Super Premium (HUMSA)", "Other"],
    "C00056": ["Brown-Forman"],
}

# Channel display order per principal (MIS Class / Type-III split)
CHANNEL_ORDER: dict[str, list[str]] = {
    "C00039": ["KW Beer", "KW Institution", "PCMC Institution", "Cross Supply", "Other"],
    "C00025": ["MOP", "POP", "Retail", "One Day", "Other"],
    "C00040": ["MOP", "POP", "Retail", "One Day", "Other"],
    "C00056": ["Retail", "KW Institution", "Other"],
}


def _channel_for(cid: str, ac3: str, cls: str) -> str:
    """MIS channel for an outlet — UBL by AcType3, spirits/BF by Class
    (BF Institution by AcType3). Mirrors src/sales._mis_channel."""
    ac3 = (ac3 or "").strip()
    cls = (cls or "").strip()
    if cid == "C00039":                       # United Breweries — Type III
        return {
            "130001": "KW Beer", "130004": "KW Institution",
            "130006": "KW Institution", "130002": "PCMC Institution",
            "130007": "Cross Supply",
        }.get(ac3, "Other")
    if cid == "C00056" and ac3 == "130004":   # BF KW Institution
        return "KW Institution"
    return {                                  # USL / Diageo / BF retail — Class
        "060021": "MOP", "060004": "POP",
        "060020": "Retail", "060022": "One Day",
    }.get(cls, "Other")


def _segment_for(cid: str, brand: str) -> str:
    """Map a MsBrandMaster.BrandName to its owner-defined segment.
    Pattern-based so trailing spaces / SKU variants don't break it."""
    b = (brand or "").strip().upper()

    if cid == "C00025":  # United Spirits
        if "SIGNATURE" in b or "ANTIQUITY" in b:
            return "Upper Prestige"
        if "MCDOWELL" in b:                      # Original / Luxury / PET
            return "Mid Prestige"
        return "Other"                            # Black Dog Millard's etc.

    if cid == "C00040":  # Diageo
        if b.startswith("SMIRNOFF"):
            return "Smirnoff (Vodka + Flavours)"
        if "BLACK LABEL" in b and "DOUBLE" not in b:
            return "Black Label"                  # Double Black -> All Others
        if "RED LABEL" in b:
            return "Red Label"
        return "All Others"

    if cid == "C00039":  # United Breweries
        if "ULTRA" in b or "HEINEKEN" in b or "AMSTEL" in b:
            return "Super Premium (HUMSA)"
        if "LONDON PIL" in b or "CANNON" in b:
            return "Economy"
        if "KING FISHER" in b or "KINGFISHER" in b:   # Strong/Lager/Smooth/Draught
            return "Mainstream"
        return "Other"

    return "Brown-Forman"


@st.cache_data(ttl=3600, show_spinner=False)
def _load_brand_monthly(company_id: str, day_cutoff: int,
                        months_back: int = 15) -> pd.DataFrame:
    """Per (BrandName, yyyy-MM): full-month cases AND MTD (day<=cutoff) cases.
    MIS-consistent: keg-aware, FY-dedup, SALES_TYPES, free goods included."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            b.BrandName                                AS BrandName,
            FORMAT(h.VoucherDate, 'yyyy-MM')           AS Mon,
            SUM({_CASES})                              AS FullCases,
            SUM(CASE WHEN DAY(h.VoucherDate) <= ?
                     THEN ({_CASES}) ELSE 0 END)       AS MtdCases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE b.CompanyID  = ?
          AND h.TransTypeID IN ({type_ph})
          AND h.Cancelled  = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY b.BrandName, FORMAT(h.VoucherDate, 'yyyy-MM')
    """
    df = run_query(sql, (day_cutoff, company_id))
    if not df.empty:
        df["FullCases"] = pd.to_numeric(df["FullCases"], errors="coerce").fillna(0.0)
        df["MtdCases"]  = pd.to_numeric(df["MtdCases"],  errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_party_monthly(company_id: str, day_cutoff: int,
                        months_back: int = 15) -> pd.DataFrame:
    """Per (Party, yyyy-MM): full-month + MTD cases. MIS-consistent:
    last-Dr-line party attribution, keg-aware, FY-dedup, free goods included."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            ISNULL(p.PartyName, d.PartyID)             AS Party,
            FORMAT(h.VoucherDate, 'yyyy-MM')           AS Mon,
            SUM({_CASES})                              AS FullCases,
            SUM(CASE WHEN DAY(h.VoucherDate) <= ?
                     THEN ({_CASES}) ELSE 0 END)       AS MtdCases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo
                                          ORDER BY id_key DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator='D' AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        LEFT JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        WHERE b.CompanyID  = ?
          AND h.TransTypeID IN ({type_ph})
          AND h.Cancelled  = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY ISNULL(p.PartyName, d.PartyID), FORMAT(h.VoucherDate, 'yyyy-MM')
    """
    df = run_query(sql, (day_cutoff, company_id))
    if not df.empty:
        df["FullCases"] = pd.to_numeric(df["FullCases"], errors="coerce").fillna(0.0)
        df["MtdCases"]  = pd.to_numeric(df["MtdCases"],  errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_channel_monthly(company_id: str, day_cutoff: int,
                          months_back: int = 15) -> pd.DataFrame:
    """Per (AcType3, Class, yyyy-MM): full-month + MTD cases. MIS-consistent:
    last-Dr-line party attribution, keg-aware, FY-dedup, free goods included."""
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT
            ISNULL(p.AcType3ID, '')                    AS AcType3ID,
            ISNULL(p.ClassID,   '')                    AS ClassID,
            FORMAT(h.VoucherDate, 'yyyy-MM')           AS Mon,
            SUM({_CASES})                              AS FullCases,
            SUM(CASE WHEN DAY(h.VoucherDate) <= ?
                     THEN ({_CASES}) ELSE 0 END)       AS MtdCases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.ItemID      LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (PARTITION BY TransTypeID, VoucherNo
                                          ORDER BY id_key DESC) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL AND DrCrIndicator='D' AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        JOIN MsPartyMaster p  ON p.PartyID = d.PartyID
        WHERE b.CompanyID  = ?
          AND h.TransTypeID IN ({type_ph})
          AND h.Cancelled  = 'N'
          AND h.VoucherDate >= DATEADD(MONTH, -{months_back}, GETDATE())
        GROUP BY p.AcType3ID, p.ClassID, FORMAT(h.VoucherDate, 'yyyy-MM')
    """
    df = run_query(sql, (day_cutoff, company_id))
    if not df.empty:
        df["FullCases"] = pd.to_numeric(df["FullCases"], errors="coerce").fillna(0.0)
        df["MtdCases"]  = pd.to_numeric(df["MtdCases"],  errors="coerce").fillna(0.0)
    return df


def _month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _prior_months(cur: str, n: int) -> list[str]:
    """n complete months immediately before `cur` (yyyy-MM), newest first."""
    yr, mo = int(cur[:4]), int(cur[5:])
    out = []
    for _ in range(n):
        mo -= 1
        if mo == 0:
            mo = 12; yr -= 1
        out.append(f"{yr:04d}-{mo:02d}")
    return out


def _trend_table(df: pd.DataFrame, group_col: str,
                 order: list[str], today: date,
                 sort_decline: bool = False) -> pd.DataFrame:
    """Build the L3M/L6M/LYSM/L3M-MTD/Current-MTD trend table for any
    grouping column. `df` must have: <group_col>, Mon, FullCases, MtdCases.
    """
    cur  = _month_str(today)
    lysm = f"{today.year-1:04d}-{today.month:02d}"
    l3   = _prior_months(cur, 3)
    l6   = _prior_months(cur, 6)

    def _sum(months, col):
        return df[df["Mon"].isin(months)].groupby(group_col)[col].sum()

    l3m    = _sum(l3, "FullCases")
    l6m    = _sum(l6, "FullCases")
    lysm_s = df[df["Mon"] == lysm].groupby(group_col)["FullCases"].sum()
    l3mtd  = _sum(l3, "MtdCases") / 3.0
    curmtd = df[df["Mon"] == cur].groupby(group_col)["MtdCases"].sum()

    groups = list(order) + [g for g in df[group_col].unique() if g not in order]
    rows = []
    for g in groups:
        full3 = float(l3m.get(g, 0.0))
        if (full3 == 0 and float(l6m.get(g, 0.0)) == 0
                and float(lysm_s.get(g, 0.0)) == 0
                and float(curmtd.get(g, 0.0)) == 0):
            continue  # skip empty groups
        rows.append({
            group_col:     g,
            "L3M":         full3,
            "L6M":         float(l6m.get(g, 0.0)),
            "LYSM":        float(lysm_s.get(g, 0.0)),
            "L3M-MTD":     float(l3mtd.get(g, 0.0)),
            "Current-MTD": float(curmtd.get(g, 0.0)),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    if sort_decline:
        # biggest absolute MTD drop first (where we're losing sales)
        out["_drop"] = out["Current-MTD"] - out["L3M-MTD"]
        out = out.sort_values("_drop").drop(columns="_drop").reset_index(drop=True)
    out["MTD Δ% vs L3M"] = out.apply(
        lambda r: ((r["Current-MTD"] - r["L3M-MTD"]) / r["L3M-MTD"] * 100.0)
                  if r["L3M-MTD"] > 0 else 0.0, axis=1)
    tot = {group_col: "TOTAL"}
    for c in ["L3M", "L6M", "LYSM", "L3M-MTD", "Current-MTD"]:
        tot[c] = float(out[c].sum())
    tot["MTD Δ% vs L3M"] = ((tot["Current-MTD"] - tot["L3M-MTD"]) / tot["L3M-MTD"] * 100.0
                            if tot["L3M-MTD"] > 0 else 0.0)
    out = pd.concat([out, pd.DataFrame([tot])], ignore_index=True)
    return out


def _segment_table(cid: str, raw: pd.DataFrame, today: date) -> pd.DataFrame:
    df = raw.copy()
    df["Segment"] = df["BrandName"].map(lambda b: _segment_for(cid, b))
    return _trend_table(df, "Segment", SEGMENT_ORDER.get(cid, []), today)


def _channel_table(cid: str, raw: pd.DataFrame, today: date) -> pd.DataFrame:
    df = raw.copy()
    df["Channel"] = df.apply(
        lambda r: _channel_for(cid, r["AcType3ID"], r["ClassID"]), axis=1)
    df = df.groupby(["Channel", "Mon"], as_index=False)[["FullCases", "MtdCases"]].sum()
    return _trend_table(df, "Channel", CHANNEL_ORDER.get(cid, []), today)


def _party_table(raw: pd.DataFrame, today: date) -> pd.DataFrame:
    # No fixed order — sort by biggest MTD drop (where we're losing sales).
    return _trend_table(raw, "Party", [], today, sort_decline=True)


def render() -> None:
    st.title("Segment Analysis")
    today = date.today()
    cutoff = today.day
    st.caption(
        f"Brand-group segments per principal. Cases are MIS-consistent "
        f"(keg-aware, free/scheme goods included). **MTD = 1-{cutoff} "
        f"{today.strftime('%b')}**; L3M-MTD = avg of the prior 3 months' "
        f"1-{cutoff} windows. Watch **MTD Δ%** to see who's pulling down."
    )

    def _delta_style(v):
        try: n = float(v)
        except (TypeError, ValueError): return ""
        if n <= -10: return "color:#dc2626;font-weight:700"
        if n < 0:    return "color:#b45309;font-weight:600"
        return "color:#16a34a;font-weight:600"

    def _show(tbl: pd.DataFrame, name: str, kind: str,
              height: int | None = None) -> None:
        """Render one styled table with its OWN download button."""
        if tbl is None or tbl.empty:
            st.caption(f"No {kind} data."); return
        sty = (tbl.style
               .format({"L3M": "{:,.0f}", "L6M": "{:,.0f}", "LYSM": "{:,.0f}",
                        "L3M-MTD": "{:,.0f}", "Current-MTD": "{:,.0f}",
                        "MTD Δ% vs L3M": "{:+.1f}%"})
               .map(_delta_style, subset=["MTD Δ% vs L3M"]))
        kwargs = {"use_container_width": True, "hide_index": True}
        if height is not None:
            kwargs["height"] = int(height)
        st.dataframe(sty, **kwargs)
        st.download_button(
            f"⬇️ Download — {name} · {kind}",
            tbl.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{kind}_{name.replace(' ', '_')}_{today.isoformat()}.csv",
            mime="text/csv",
            key=f"dl_{cid}_{kind}",
        )

    # Principal selector — one at a time, like the Sales Plan tab
    names = [n for n, _ in PRINCIPALS]
    sel = st.radio("Principal", names, horizontal=True, index=0,
                   key="seg_principal")
    cid = dict(PRINCIPALS)[sel]
    name = sel
    st.divider()

    try:
        raw_b = _load_brand_monthly(cid, cutoff)
        raw_c = _load_channel_monthly(cid, cutoff)
        raw_p = _load_party_monthly(cid, cutoff)
    except Exception as exc:
        st.error(f"Could not load {name}: {exc}")
        return
    if raw_b.empty and raw_c.empty and raw_p.empty:
        st.info("No sales data for this principal."); return

    st.markdown("**By segment**")
    _show(_segment_table(cid, raw_b, today), name, "segment")

    st.markdown("**By channel**")
    _show(_channel_table(cid, raw_c, today), name, "channel")

    st.markdown("**By party — biggest losers first** "
                "(sorted by MTD drop vs the prior 3-month MTD average)")
    _show(_party_table(raw_p, today), name, "party", height=460)
