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


def _segment_table(cid: str, raw: pd.DataFrame, today: date) -> pd.DataFrame:
    cur = _month_str(today)
    lysm = f"{today.year-1:04d}-{today.month:02d}"
    l3 = _prior_months(cur, 3)
    l6 = _prior_months(cur, 6)

    df = raw.copy()
    df["Segment"] = df["BrandName"].map(lambda b: _segment_for(cid, b))

    def _sum(months, col):
        return (df[df["Mon"].isin(months)]
                .groupby("Segment")[col].sum())

    l3m   = _sum(l3, "FullCases")
    l6m   = _sum(l6, "FullCases")
    lysm_s = df[df["Mon"] == lysm].groupby("Segment")["FullCases"].sum()
    l3mtd = _sum(l3, "MtdCases") / 3.0
    curmtd = df[df["Mon"] == cur].groupby("Segment")["MtdCases"].sum()

    segs = SEGMENT_ORDER.get(cid, sorted(df["Segment"].unique()))
    rows = []
    for s in segs:
        rows.append({
            "Segment":      s,
            "L3M":          float(l3m.get(s, 0.0)),
            "L6M":          float(l6m.get(s, 0.0)),
            "LYSM":         float(lysm_s.get(s, 0.0)),
            "L3M-MTD":      float(l3mtd.get(s, 0.0)),
            "Current-MTD":  float(curmtd.get(s, 0.0)),
        })
    out = pd.DataFrame(rows)
    # also append any unexpected segment not in the order list
    extra = [s for s in df["Segment"].unique() if s not in segs]
    for s in extra:
        out = pd.concat([out, pd.DataFrame([{
            "Segment": s,
            "L3M": float(l3m.get(s, 0.0)), "L6M": float(l6m.get(s, 0.0)),
            "LYSM": float(lysm_s.get(s, 0.0)), "L3M-MTD": float(l3mtd.get(s, 0.0)),
            "Current-MTD": float(curmtd.get(s, 0.0)),
        }])], ignore_index=True)

    out["MTD Δ% vs L3M"] = out.apply(
        lambda r: ((r["Current-MTD"] - r["L3M-MTD"]) / r["L3M-MTD"] * 100.0)
                  if r["L3M-MTD"] > 0 else 0.0, axis=1)
    # TOTAL row
    tot = {"Segment": "TOTAL"}
    for c in ["L3M", "L6M", "LYSM", "L3M-MTD", "Current-MTD"]:
        tot[c] = float(out[c].sum())
    tot["MTD Δ% vs L3M"] = ((tot["Current-MTD"] - tot["L3M-MTD"]) / tot["L3M-MTD"] * 100.0
                            if tot["L3M-MTD"] > 0 else 0.0)
    out = pd.concat([out, pd.DataFrame([tot])], ignore_index=True)
    return out


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

    all_frames = []
    for name, cid in PRINCIPALS:
        st.markdown(f"### {name}")
        try:
            raw = _load_brand_monthly(cid, cutoff)
        except Exception as exc:
            st.error(f"Could not load {name}: {exc}")
            continue
        if raw.empty:
            st.info("No sales data."); continue
        tbl = _segment_table(cid, raw, today)

        def _delta_style(v):
            try: n = float(v)
            except (TypeError, ValueError): return ""
            if n <= -10: return "color:#dc2626;font-weight:700"
            if n < 0:    return "color:#b45309;font-weight:600"
            return "color:#16a34a;font-weight:600"

        sty = (tbl.style
               .format({"L3M": "{:,.0f}", "L6M": "{:,.0f}", "LYSM": "{:,.0f}",
                        "L3M-MTD": "{:,.0f}", "Current-MTD": "{:,.0f}",
                        "MTD Δ% vs L3M": "{:+.1f}%"})
               .map(_delta_style, subset=["MTD Δ% vs L3M"]))
        st.dataframe(sty, use_container_width=True, hide_index=True)

        f = tbl.copy(); f.insert(0, "Principal", name)
        all_frames.append(f)

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        st.download_button(
            "⬇️ Download segment analysis (CSV)",
            combined.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"segment_analysis_{today.isoformat()}.csv",
            mime="text/csv",
        )
