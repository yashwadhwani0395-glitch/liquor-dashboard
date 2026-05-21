"""src/operations.py — Operations Rhythm.

Daily / weekly tool for managing the month's WOD targets.

Four phase views:
    1. PLAN     (Day 1)       — set targets, see must-bill list
    2. TRACK    (mid-month)   — pace, progress, remaining outlets
    3. ACTION   (live, daily) — today's red/amber/green focus list
    4. REVIEW   (month-end)   — scorecard, wins, misses, talking points

Reuses the active-billing universe machinery from distribution.py.
"""
from __future__ import annotations

import calendar
import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from utils.helpers import format_inr
from src.targets import get_target, set_target
from src.distribution import (
    SALESMAN_MAP,
    _load_master,
    _load_active_universe,
    _build_universes,
    _last_n_month_strs,
    _last_complete_month,
    _fmt_month,
    _prev_month_str,
)

SALES_TYPES: tuple[int, ...] = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)

# ── Channel labels ───────────────────────────────────────────────────────────
_LT_LABEL: dict[str, str] = {
    "180001": "FL-II Wine Shop",
    "180002": "FL-III Permit Room",
    "180004": "FL-BR-II Beer Shopee",
    "180005": "FL-IV Club",
    "180007": "FL-IV One Day",
}

# ── Chart theme ──────────────────────────────────────────────────────────────
_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#1A1A1A"),
    template="plotly_white",
    margin=dict(l=16, r=16, t=36, b=16),
)
_GRID = dict(gridcolor="#E8E8E8")


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def _load_daily_billing(month_start: date, month_end: date) -> pd.DataFrame:
    """Distinct (BillDate, PartyID, CompanyID) rows for the operating month.

    Used by Phase 2 (daily trend) and Phase 3 (already-billed-this-month set).
    """
    type_ph = ",".join(str(t) for t in SALES_TYPES)
    sql = f"""
        SELECT DISTINCT
            CAST(h.VoucherDate AS date) AS BillDate,
            d.PartyID,
            b.CompanyID
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON  vi.TransTypeID = h.TransTypeID
            AND vi.VoucherNo   = h.VoucherNo
            AND vi.FreeItemYN  = 'N'
            AND vi.ItemID      LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)
                     + '-' + CAST(YEAR(h.VoucherDate) AS VARCHAR)
            END
        JOIN (
            SELECT TransTypeID, VoucherNo, PartyID
            FROM (
                SELECT TransTypeID, VoucherNo, PartyID,
                       ROW_NUMBER() OVER (
                           PARTITION BY TransTypeID, VoucherNo
                           ORDER BY id_key DESC
                       ) AS rn
                FROM TrVocDetail
                WHERE PartyID IS NOT NULL
                  AND DrCrIndicator = 'D'
                  AND PartyID LIKE 'D%'
            ) x WHERE rn = 1
        ) d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({type_ph})
          AND h.Cancelled   = 'N'
          AND b.CompanyID IN ('C00025','C00040','C00056','C00039')
          AND CAST(h.VoucherDate AS date) BETWEEN ? AND ?
    """
    df = run_query(sql, (str(month_start), str(month_end)))
    if not df.empty:
        df["BillDate"] = pd.to_datetime(df["BillDate"])
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _month_bounds(any_day: date) -> tuple[date, date]:
    """Return (first, last) day of the month containing `any_day`."""
    first = any_day.replace(day=1)
    _, last_dom = calendar.monthrange(first.year, first.month)
    return first, first.replace(day=last_dom)


def _month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _per_party_month_mask(sm_master: pd.DataFrame,
                          months: list[str]) -> dict[str, frozenset]:
    """{month_str: frozenset of PartyIDs that billed in that month}."""
    return {
        m: frozenset(sm_master[sm_master["BillMonth"] == m]["PartyID"])
        for m in months
    }


def _categorise_outlets(sm_master: pd.DataFrame,
                        universe: frozenset,
                        current_month: str) -> dict[str, pd.DataFrame]:
    """Bucket outlets into Guaranteed / Stretch / Recover.

    Guaranteed: billed in 6 of last 6 months
    Stretch:    billed in 3–5 of last 6 months
    Recover:    billed in 0–2 of last 6 months, but billed in 3+ of last 12

    All buckets are restricted to the salesman's universe.
    """
    # last 6 months ending at the month BEFORE current (so current is the
    # month being planned and isn't double-counted in "billed last 6")
    last_6  = _last_n_month_strs(7)[:-1] if False else _last_n_month_strs(6)
    last_12 = _last_n_month_strs(12)

    # Exclude the current planning month from history so the categorisation
    # reflects what we know going into the new month.
    last_6  = [m for m in last_6  if m != current_month]
    last_12 = [m for m in last_12 if m != current_month]

    by_party_6 = (
        sm_master[sm_master["BillMonth"].isin(last_6) & sm_master["PartyID"].isin(universe)]
        .groupby("PartyID")["BillMonth"].nunique()
    )
    by_party_12 = (
        sm_master[sm_master["BillMonth"].isin(last_12) & sm_master["PartyID"].isin(universe)]
        .groupby("PartyID")["BillMonth"].nunique()
    )

    party_info = (
        sm_master[sm_master["PartyID"].isin(universe)]
        .groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
        .agg(
            Cases6=("Cases",   "sum"),
            Rev6=  ("Revenue", "sum"),
            LastBill=("LastBillDate", "max"),
        )
    )
    party_info["Channel"]    = party_info["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
    party_info["MonthsIn6"]  = party_info["PartyID"].map(by_party_6).fillna(0).astype(int)
    party_info["MonthsIn12"] = party_info["PartyID"].map(by_party_12).fillna(0).astype(int)
    party_info["AvgCases"]   = (party_info["Cases6"] / 6).round(1)
    party_info["AvgRev"]     = (party_info["Rev6"] / 6).round(0)

    guaranteed = party_info[party_info["MonthsIn6"] >= 6].copy()
    stretch    = party_info[(party_info["MonthsIn6"] >= 3) & (party_info["MonthsIn6"] <= 5)].copy()
    recover    = party_info[(party_info["MonthsIn6"] <= 2) & (party_info["MonthsIn12"] >= 3)].copy()

    # also include universe parties with zero recent billing in "Recover"
    seen = set(party_info["PartyID"])
    missing = universe - seen
    if missing:
        miss_df = pd.DataFrame([
            {"PartyID": pid, "PartyName": "—", "LicenseTypeID": "", "Channel": "—",
             "Cases6": 0, "Rev6": 0.0, "LastBill": pd.NaT,
             "MonthsIn6": 0, "MonthsIn12": 0, "AvgCases": 0.0, "AvgRev": 0.0}
            for pid in missing
        ])
        # Don't add these to "recover" automatically — too noisy.

    return {"guaranteed": guaranteed, "stretch": stretch, "recover": recover}


def _pace_metrics(billed_now: int, universe: int,
                  target_pct: float, op_month: date) -> dict:
    """Compute pace indicators for Phase 2."""
    today = date.today()
    first, last = _month_bounds(op_month)
    # Cap days_passed so a future-month selection doesn't break math
    days_passed = max(1, (min(today, last) - first).days + 1)
    days_in     = (last - first).days + 1
    days_left   = max(0, days_in - days_passed)

    current_wod = billed_now / universe * 100 if universe else 0.0
    projected   = current_wod * (days_in / max(days_passed, 1))
    target_billed = int(round(target_pct / 100 * universe))
    needed_more   = max(0, target_billed - billed_now)
    pace_needed   = needed_more / max(days_left, 1)

    return {
        "days_passed":    days_passed,
        "days_in":        days_in,
        "days_left":      days_left,
        "current_wod":    current_wod,
        "projected_wod":  projected,
        "target_billed":  target_billed,
        "needed_more":    needed_more,
        "pace_needed":    pace_needed,
    }


def _wod_color(pct: float) -> tuple[str, str]:
    if pct >= 70: return "#dcfce7", "#166534"
    if pct >= 50: return "#fef3c7", "#854d0e"
    return "#fee2e2", "#991b1b"


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — PLAN
# ═══════════════════════════════════════════════════════════════════════════════

def _phase_plan(selected_sm: list[str], op_month: date,
                master: pd.DataFrame, universes: dict[str, frozenset]) -> None:
    st.subheader(f"Plan · {_fmt_month(_month_str(op_month))}")
    st.caption("Set targets for the month and review who you must bill.")

    op_month_str   = _month_str(op_month)
    prev_month_str = _prev_month_str(op_month_str)

    excel_per_sm: dict[str, pd.DataFrame] = {}

    for sm in selected_sm:
        if sm not in SALESMAN_MAP:
            continue
        cfg = SALESMAN_MAP[sm]
        universe = universes[sm]
        uni_size = len(universe)
        if uni_size == 0:
            with st.expander(f"⚠️ {sm} — no active universe"):
                st.info("This salesman has no outlets in their active universe.")
            continue

        sm_master = master[
            (master["CompanyID"].isin(cfg["principals"])) &
            (master["PartyID"].isin(universe))
        ]

        # ── Last month results ────────────────────────────────────────────
        prev_billed_set = frozenset(
            sm_master[sm_master["BillMonth"] == prev_month_str]["PartyID"]
        )
        prev_billed = len(prev_billed_set)
        prev_wod    = prev_billed / uni_size * 100 if uni_size else 0.0
        prev_cases  = int(sm_master[sm_master["BillMonth"] == prev_month_str]["Cases"].sum())
        prev_rev    = float(sm_master[sm_master["BillMonth"] == prev_month_str]["Revenue"].sum())

        prev_target = get_target(sm, prev_month_str)

        # Movement: outlets that billed two months ago but lapsed last month / recovered
        two_back = _last_n_month_strs(2)[0]  # the month BEFORE prev_month
        two_back_set = frozenset(
            sm_master[sm_master["BillMonth"] == two_back]["PartyID"]
        )
        recovered = (prev_billed_set - two_back_set) & universe
        lost      = (two_back_set - prev_billed_set) & universe

        # ── Default target = last actual + 5 ──
        existing_target = get_target(sm, op_month_str,
                                     default_wod=min(95.0, round(prev_wod + 5, 0)),
                                     default_outlets=int(round((prev_wod + 5) / 100 * uni_size)))

        with st.expander(f"**{sm}** · universe {uni_size} · last WOD {prev_wod:.1f}%",
                         expanded=(len(selected_sm) == 1)):
            col_l, col_r = st.columns([1, 1])

            # ── Left: last month results ──
            with col_l:
                st.markdown(f"##### {_fmt_month(prev_month_str)} Results")
                m1, m2, m3 = st.columns(3)
                with m1:
                    delta = f"{prev_wod - (prev_target.get('wod_pct') or prev_wod):+.1f} vs target" \
                        if prev_target.get("wod_pct") else None
                    st.metric("WOD %", f"{prev_wod:.1f}%", delta=delta)
                with m2:
                    target_outlets = prev_target.get("target_outlets") or "-"
                    st.metric("Outlets billed",
                              f"{prev_billed}",
                              delta=f"of {target_outlets} target" if target_outlets != "-" else None,
                              delta_color="off")
                with m3:
                    st.metric("Revenue", format_inr(prev_rev))

                wins  = f"✅ Recovered {len(recovered)} outlets · cases {prev_cases:,}"
                misses = f"⚠️ Newly lapsed {len(lost)} outlets"
                st.success(wins)
                st.warning(misses)

            # ── Right: this month plan ──
            with col_r:
                st.markdown(f"##### {_fmt_month(op_month_str)} Plan")
                with st.form(key=f"plan_{sm}_{op_month_str}"):
                    wod_default = int(existing_target.get("wod_pct") or 70)
                    wod_pct = st.slider(
                        "WOD % Target", 30, 100, value=wod_default, step=5,
                        key=f"slider_{sm}_{op_month_str}",
                    )
                    target_outlets = int(round(wod_pct / 100 * uni_size))
                    st.caption(f"= **{target_outlets:,} outlets** of {uni_size:,} universe")
                    submitted = st.form_submit_button("💾 Save target")
                if submitted:
                    set_target(sm, op_month_str, wod_pct, target_outlets)
                    st.success(f"Target saved: {wod_pct}% ({target_outlets} outlets)")

                last_set = existing_target.get("updated_at")
                if last_set:
                    st.caption(f"Last saved: {last_set}")

            st.markdown("---")

            # ── Must-bill outlet lists ──
            buckets = _categorise_outlets(sm_master, universe, op_month_str)
            g, s, r = buckets["guaranteed"], buckets["stretch"], buckets["recover"]

            tabs = st.tabs([
                f"🟢 Guaranteed ({len(g)})",
                f"🟡 Stretch ({len(s)})",
                f"🔴 Recover ({len(r)})",
            ])

            with tabs[0]:
                st.caption("Billed in **all** of last 6 months — confirm visits early.")
                if g.empty:
                    st.info("No guaranteed outlets.")
                else:
                    gt = g.sort_values("AvgRev", ascending=False).head(50)[
                        ["PartyName", "Channel", "AvgCases", "AvgRev"]
                    ].rename(columns={
                        "PartyName": "Party",
                        "AvgCases":  "Avg Cases/mo",
                        "AvgRev":    "Avg Revenue/mo",
                    })
                    st.dataframe(
                        gt.style.format({"Avg Revenue/mo": format_inr,
                                         "Avg Cases/mo":   "{:.1f}"}),
                        use_container_width=True, hide_index=True,
                    )
                    g_out = gt.copy(); g_out["Bucket"] = "Guaranteed"
                    excel_per_sm.setdefault(sm, pd.DataFrame())
                    excel_per_sm[sm] = pd.concat([excel_per_sm[sm], g_out], ignore_index=True)

            with tabs[1]:
                st.caption("Billed 3–5 of last 6 — inconsistent buyers. Focus to convert.")
                if s.empty:
                    st.info("No stretch outlets.")
                else:
                    st_df = s.sort_values("MonthsIn6", ascending=False).head(50).copy()
                    st_df["LastBill"] = pd.to_datetime(st_df["LastBill"], errors="coerce") \
                        .dt.strftime("%d %b %Y").fillna("—")
                    tbl = st_df[["PartyName", "Channel", "MonthsIn6", "LastBill", "AvgRev"]].rename(
                        columns={"PartyName": "Party", "MonthsIn6": "Months/6",
                                 "LastBill": "Last Bill", "AvgRev": "Avg Revenue/mo"},
                    )
                    st.dataframe(
                        tbl.style.format({"Avg Revenue/mo": format_inr}),
                        use_container_width=True, hide_index=True,
                    )
                    s_out = tbl.copy(); s_out["Bucket"] = "Stretch"
                    excel_per_sm.setdefault(sm, pd.DataFrame())
                    excel_per_sm[sm] = pd.concat([excel_per_sm[sm], s_out], ignore_index=True)

            with tabs[2]:
                st.caption("Lapsed recently but were active in last 12. **Top priority**.")
                if r.empty:
                    st.success("No recovery candidates — clean slate.")
                else:
                    rc = r.sort_values("Rev6", ascending=False).head(50).copy()
                    rc["LastBill"] = pd.to_datetime(rc["LastBill"], errors="coerce") \
                        .dt.strftime("%d %b %Y").fillna("Never")
                    tbl = rc[["PartyName", "Channel", "LastBill", "MonthsIn12", "Rev6"]].rename(
                        columns={"PartyName": "Party", "MonthsIn12": "Active/12",
                                 "LastBill": "Last Bill", "Rev6": "Last 6mo Rev"},
                    )
                    st.dataframe(
                        tbl.style.format({"Last 6mo Rev": format_inr}),
                        use_container_width=True, hide_index=True,
                    )
                    r_out = tbl.copy(); r_out["Bucket"] = "Recover"
                    excel_per_sm.setdefault(sm, pd.DataFrame())
                    excel_per_sm[sm] = pd.concat([excel_per_sm[sm], r_out], ignore_index=True)

    # ── Export ──
    if excel_per_sm:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for sm, df in excel_per_sm.items():
                if df is not None and not df.empty:
                    df.to_excel(writer, sheet_name=sm[:31], index=False)
        buf.seek(0)
        st.download_button(
            "📥 Download Salesman Action Plan (Excel)",
            buf,
            file_name=f"action_plan_{op_month_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — TRACK
# ═══════════════════════════════════════════════════════════════════════════════

def _phase_track(selected_sm: list[str], op_month: date,
                 master: pd.DataFrame, universes: dict[str, frozenset]) -> None:
    st.subheader(f"Track · {_fmt_month(_month_str(op_month))}")
    st.caption("Progress vs target so far this month.")

    op_month_str = _month_str(op_month)
    first, last  = _month_bounds(op_month)
    daily_df     = _load_daily_billing(first, last)

    for sm in selected_sm:
        if sm not in SALESMAN_MAP:
            continue
        cfg = SALESMAN_MAP[sm]
        universe = universes[sm]
        uni_size = len(universe)
        if uni_size == 0:
            continue

        target_entry  = get_target(sm, op_month_str)
        target_wod    = target_entry.get("wod_pct") or 70
        target_outlets = target_entry.get("target_outlets") or int(round(target_wod / 100 * uni_size))

        # Outlets billed so far this month
        if daily_df.empty:
            billed_set: frozenset = frozenset()
        else:
            sm_daily = daily_df[
                daily_df["CompanyID"].isin(cfg["principals"]) &
                daily_df["PartyID"].isin(universe)
            ]
            billed_set = frozenset(sm_daily["PartyID"])
        billed_now = len(billed_set)
        pace = _pace_metrics(billed_now, uni_size, target_wod, op_month)

        # ── Header ──
        with st.expander(
            f"**{sm}** — {billed_now} of {uni_size} billed "
            f"({pace['current_wod']:.1f}% so far · target {target_wod:.0f}%)",
            expanded=(len(selected_sm) == 1),
        ):
            # ── Big progress bar ──
            pct_of_target = min(1.0, billed_now / target_outlets) if target_outlets else 0
            st.progress(pct_of_target,
                        text=f"{billed_now:,} / {target_outlets:,} target outlets "
                             f"({pct_of_target*100:.0f}% of plan)")

            # ── Pace indicator (3 columns) ──
            p1, p2, p3, p4 = st.columns(4)
            with p1:
                pbg, pfg = _wod_color(pace["projected_wod"])
                st.markdown(
                    f"<div style='background:{pbg}; padding:0.6rem; border-radius:6px;"
                    f"text-align:center'><div style='font-size:0.7rem;color:#374151'>"
                    f"At current pace</div><div style='font-size:1.4rem;font-weight:700;"
                    f"color:{pfg}'>{pace['projected_wod']:.0f}%</div></div>",
                    unsafe_allow_html=True,
                )
            with p2:
                st.metric("🎯 Need more", f"{pace['needed_more']:,}",
                          delta=f"to hit {target_wod:.0f}% target",
                          delta_color="off")
            with p3:
                st.metric("📅 Days remaining", f"{pace['days_left']}")
            with p4:
                st.metric("📊 Pace needed", f"{pace['pace_needed']:.1f}/day")

            # ── Daily billing trend ──
            if not daily_df.empty and not sm_daily.empty:
                daily = (
                    sm_daily.groupby(sm_daily["BillDate"].dt.date)["PartyID"]
                    .nunique().reset_index()
                    .rename(columns={"BillDate": "Day", "PartyID": "Outlets"})
                )
                daily["Day"] = pd.to_datetime(daily["Day"])
                daily["WeekNum"] = ((daily["Day"].dt.day - 1) // 7 + 1).astype(str)
                fig = px.bar(
                    daily, x="Day", y="Outlets", color="WeekNum",
                    color_discrete_sequence=px.colors.qualitative.Bold,
                    labels={"Day": "", "Outlets": "Outlets billed", "WeekNum": "Week"},
                )
                fig.update_layout(**_LAYOUT, height=240,
                                  xaxis=_GRID, yaxis=_GRID)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No daily billing yet this month.")

            # ── Status split ──
            sm_master = master[
                (master["CompanyID"].isin(cfg["principals"])) &
                (master["PartyID"].isin(universe))
            ]
            buckets = _categorise_outlets(sm_master, universe, op_month_str)
            in_plan_ids = (
                frozenset(buckets["guaranteed"]["PartyID"]) |
                frozenset(buckets["stretch"]["PartyID"])    |
                frozenset(buckets["recover"]["PartyID"])
            )
            yet_in_plan = (in_plan_ids - billed_set) & universe
            outside_plan = (universe - billed_set) - in_plan_ids

            s1, s2, s3 = st.columns(3)
            s1.metric("✅ Billed this month",  f"{billed_now}")
            s2.metric("⏳ Yet to bill (plan)", f"{len(yet_in_plan)}")
            s3.metric("⚠️ Outside plan",      f"{len(outside_plan)}")

            # ── Remaining outlets table (priority sorted) ──
            st.markdown("**Remaining outlets to push:**")
            party_info = (
                sm_master.groupby(["PartyID", "PartyName", "LicenseTypeID"], as_index=False)
                .agg(LastBill=("LastBillDate", "max"),
                     LastRev=("Revenue", "max"))
            )
            party_info["Channel"] = party_info["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
            recover_set    = frozenset(buckets["recover"]["PartyID"])
            stretch_set    = frozenset(buckets["stretch"]["PartyID"])
            guaranteed_set = frozenset(buckets["guaranteed"]["PartyID"])

            def _priority(pid: str) -> tuple[int, str]:
                if pid in recover_set:    return (0, "🔴 Recover")
                if pid in stretch_set:    return (1, "🟡 Stretch")
                if pid in guaranteed_set: return (2, "🟢 Guaranteed")
                return (3, "·  Other")

            rem_df = party_info[party_info["PartyID"].isin(universe - billed_set)].copy()
            if rem_df.empty:
                st.success("🎉 All universe outlets billed.")
            else:
                rem_df[["_ord", "Priority"]] = rem_df["PartyID"].apply(
                    lambda pid: pd.Series(_priority(pid))
                )
                rem_df["Last Bill"] = pd.to_datetime(rem_df["LastBill"], errors="coerce") \
                    .dt.strftime("%d %b %Y").fillna("Never")
                rem_df = rem_df.sort_values(["_ord", "LastRev"], ascending=[True, False]).head(40)
                tbl = rem_df[["Priority", "PartyName", "Channel", "Last Bill", "LastRev"]].rename(
                    columns={"PartyName": "Party", "LastRev": "Last Bill Value"},
                )
                st.dataframe(
                    tbl.style.format({"Last Bill Value": format_inr}),
                    use_container_width=True, hide_index=True,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — ACTION LIST (red/amber/green)
# ═══════════════════════════════════════════════════════════════════════════════

def _phase_action(selected_sm: list[str], op_month: date,
                  master: pd.DataFrame, universes: dict[str, frozenset]) -> None:
    st.subheader("Today's Action List")
    st.caption("Filter to one salesman to see today's red / amber / green list.")

    if not selected_sm:
        st.info("Select at least one salesman above.")
        return
    sm = selected_sm[0] if len(selected_sm) == 1 else st.selectbox(
        "Salesman", selected_sm, key="action_sm",
    )
    cfg = SALESMAN_MAP[sm]
    universe = universes[sm]
    uni_size = len(universe)
    if uni_size == 0:
        st.warning(f"No universe for {sm}.")
        return

    op_month_str = _month_str(op_month)
    first, last  = _month_bounds(op_month)
    daily_df     = _load_daily_billing(first, last)
    if daily_df.empty:
        billed_set: frozenset = frozenset()
    else:
        billed_set = frozenset(
            daily_df[
                daily_df["CompanyID"].isin(cfg["principals"]) &
                daily_df["PartyID"].isin(universe)
            ]["PartyID"]
        )

    sm_master = master[
        (master["CompanyID"].isin(cfg["principals"])) &
        (master["PartyID"].isin(universe))
    ]
    buckets = _categorise_outlets(sm_master, universe, op_month_str)
    recover    = buckets["recover"]
    stretch    = buckets["stretch"]
    guaranteed = buckets["guaranteed"]

    recover_pending    = recover[~recover["PartyID"].isin(billed_set)]
    stretch_pending    = stretch[~stretch["PartyID"].isin(billed_set)]
    guaranteed_pending = guaranteed[~guaranteed["PartyID"].isin(billed_set)]
    guaranteed_done    = guaranteed[guaranteed["PartyID"].isin(billed_set)]

    # ── Layout ──
    col_r, col_a, col_g = st.columns(3)

    with col_r:
        st.markdown(f"#### 🔴 Visit Today/Tomorrow  ·  {len(recover_pending)}")
        st.caption("High-priority recoveries — biggest revenue at risk first.")
        if recover_pending.empty:
            st.success("No urgent recoveries pending.")
        else:
            tbl = recover_pending.sort_values("Rev6", ascending=False).head(20).copy()
            tbl["Last Bill"] = pd.to_datetime(tbl["LastBill"], errors="coerce") \
                .dt.strftime("%d %b %Y").fillna("Never")
            disp = tbl[["PartyName", "Channel", "Last Bill", "Rev6"]].rename(
                columns={"PartyName": "Party", "Rev6": "Last 6mo Rev"},
            )
            st.dataframe(
                disp.style.format({"Last 6mo Rev": format_inr}),
                use_container_width=True, hide_index=True,
            )
            st.download_button(
                "📥 Red list (CSV)",
                disp.to_csv(index=False).encode(),
                file_name=f"red_{sm}_{op_month_str}.csv",
                mime="text/csv",
                key=f"dl_red_{sm}",
            )

    with col_a:
        st.markdown(f"#### 🟡 This Week  ·  {len(stretch_pending)}")
        st.caption("Inconsistent buyers — schedule a call this week.")
        if stretch_pending.empty:
            st.success("No stretch outlets pending.")
        else:
            tbl = stretch_pending.sort_values("AvgRev", ascending=False).head(20).copy()
            tbl["Last Bill"] = pd.to_datetime(tbl["LastBill"], errors="coerce") \
                .dt.strftime("%d %b %Y").fillna("—")
            disp = tbl[["PartyName", "Channel", "Last Bill", "MonthsIn6", "AvgRev"]].rename(
                columns={"PartyName": "Party", "MonthsIn6": "Months/6",
                         "AvgRev": "Avg Revenue/mo"},
            )
            st.dataframe(
                disp.style.format({"Avg Revenue/mo": format_inr}),
                use_container_width=True, hide_index=True,
            )
            st.download_button(
                "📥 Amber list (CSV)",
                disp.to_csv(index=False).encode(),
                file_name=f"amber_{sm}_{op_month_str}.csv",
                mime="text/csv",
                key=f"dl_amber_{sm}",
            )

    with col_g:
        st.markdown(f"#### 🟢 Completed  ·  {len(billed_set)}")
        st.caption("Already billed this month — no action needed.")
        if not billed_set:
            st.info("Nothing billed yet this month.")
        else:
            with st.expander(f"Show {len(billed_set)} completed outlets"):
                completed = sm_master[sm_master["PartyID"].isin(billed_set)].drop_duplicates("PartyID")
                completed["Channel"] = completed["LicenseTypeID"].map(_LT_LABEL).fillna("Other")
                st.dataframe(
                    completed[["PartyName", "Channel"]].rename(
                        columns={"PartyName": "Party"}
                    ),
                    use_container_width=True, hide_index=True,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — REVIEW (month-end)
# ═══════════════════════════════════════════════════════════════════════════════

def _phase_review(selected_sm: list[str], op_month: date,
                  master: pd.DataFrame, universes: dict[str, frozenset]) -> None:
    st.subheader(f"Review · {_fmt_month(_month_str(op_month))}")
    st.caption("Month-end scorecard. Auto-generated talking points for next month.")

    op_month_str = _month_str(op_month)
    months_6     = _last_n_month_strs(6)

    for sm in selected_sm:
        if sm not in SALESMAN_MAP:
            continue
        cfg = SALESMAN_MAP[sm]
        universe = universes[sm]
        uni_size = len(universe)
        if uni_size == 0:
            continue

        sm_master = master[
            (master["CompanyID"].isin(cfg["principals"])) &
            (master["PartyID"].isin(universe))
        ]

        # ── Final WOD ──
        mo_set = frozenset(sm_master[sm_master["BillMonth"] == op_month_str]["PartyID"])
        final_billed = len(mo_set)
        final_wod    = final_billed / uni_size * 100 if uni_size else 0.0
        target_entry = get_target(sm, op_month_str)
        target_wod   = target_entry.get("wod_pct")
        hit = (target_wod is not None) and (final_wod >= float(target_wod))
        icon = "✅" if hit else ("❌" if target_wod is not None else "·")
        target_txt = f"Target: {target_wod:.0f}%" if target_wod else "No target set"

        with st.expander(
            f"**{sm}** — Final WOD {final_wod:.1f}%  ·  {target_txt}  {icon}",
            expanded=(len(selected_sm) == 1),
        ):
            # ── Wins ──
            prev_mo  = _prev_month_str(op_month_str)
            prev_set = frozenset(sm_master[sm_master["BillMonth"] == prev_mo]["PartyID"])
            recovered = mo_set - prev_set
            newly_lapsed = prev_set - mo_set

            # Brand-new = appeared in op_month but never in last 6 prior
            prior_6_set = frozenset(
                sm_master[
                    sm_master["BillMonth"].isin([m for m in months_6 if m != op_month_str])
                ]["PartyID"]
            )
            new_outlets = mo_set - prior_6_set

            w_col, m_col = st.columns(2)
            with w_col:
                st.markdown("#### ✅ Wins")
                st.markdown(f"- Recovered **{len(recovered)}** lapsed outlets")
                st.markdown(f"- Activated **{len(new_outlets)}** new outlets")
                rev_mo = float(sm_master[sm_master["BillMonth"] == op_month_str]["Revenue"].sum())
                cases_mo = int(sm_master[sm_master["BillMonth"] == op_month_str]["Cases"].sum())
                st.markdown(f"- Revenue **{format_inr(rev_mo)}** · Cases **{cases_mo:,}**")

            with m_col:
                st.markdown("#### ⚠️ Misses")
                # Newly-lapsed with last-month revenue at risk
                lapsed_rows = (
                    sm_master[
                        (sm_master["BillMonth"] == prev_mo) &
                        sm_master["PartyID"].isin(newly_lapsed)
                    ]
                    .groupby(["PartyID", "PartyName"], as_index=False)["Revenue"].sum()
                    .sort_values("Revenue", ascending=False)
                )
                rev_lost = float(lapsed_rows["Revenue"].sum()) if not lapsed_rows.empty else 0.0
                st.markdown(f"- Newly lapsed **{len(newly_lapsed)}** outlets")
                st.markdown(f"- Revenue at risk **{format_inr(rev_lost)}**")
                if target_wod is not None and not hit:
                    gap_outlets = max(0, int(round(target_wod / 100 * uni_size)) - final_billed)
                    st.markdown(f"- Missed target by **{gap_outlets}** outlets")

            # ── 6-month WOD trend ──
            st.markdown("**Last 6 months WOD trend**")
            trend_rows = []
            for m in months_6:
                set_m = frozenset(sm_master[sm_master["BillMonth"] == m]["PartyID"])
                wod_m = len(set_m) / uni_size * 100 if uni_size else 0.0
                trend_rows.append({"Month": _fmt_month(m), "WOD %": round(wod_m, 1)})
            tr = pd.DataFrame(trend_rows)
            fig = px.line(
                tr, x="Month", y="WOD %", markers=True,
                color_discrete_sequence=["#1B4F72"],
            )
            fig.update_layout(**_LAYOUT, height=240,
                              xaxis=_GRID, yaxis=dict(ticksuffix="%", **_GRID))
            fig.update_traces(line_width=2.5, marker_size=6)
            st.plotly_chart(fig, use_container_width=True)

            # ── Talking points (auto-generated) ──
            st.markdown("#### 🎯 Talking Points for Next Month Meeting")
            tps: list[str] = []

            top_lapsed = lapsed_rows.head(3) if not lapsed_rows.empty else pd.DataFrame()
            for _, row in top_lapsed.iterrows():
                tps.append(
                    f"Focus on recovering **{row['PartyName']}** — "
                    f"last billed {format_inr(row['Revenue'])} before lapsing."
                )

            if len(recovered) >= 3:
                tps.append(f"Replicate this month's playbook — recovered **{len(recovered)}** outlets.")

            if target_wod is not None:
                if hit:
                    tps.append(f"Exceeded target by **{final_wod - target_wod:+.1f}** pp; consider raising next month's target.")
                else:
                    tps.append(f"Missed target by **{target_wod - final_wod:.1f}** pp; revisit must-bill list each Friday.")

            # Trend talking point
            if len(tr) >= 3:
                last3 = tr["WOD %"].tail(3).tolist()
                if last3[-1] - last3[0] > 5:
                    tps.append(f"WOD trending **up** ({last3[0]:.0f}% → {last3[-1]:.0f}%) over last 3 months.")
                elif last3[-1] - last3[0] < -5:
                    tps.append(f"WOD trending **down** ({last3[0]:.0f}% → {last3[-1]:.0f}%) — needs attention.")

            if not tps:
                tps.append("Stable month with no standout wins or losses.")
            for tp in tps:
                st.markdown(f"- {tp}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════════════

def render() -> None:
    st.title("Operations Rhythm")
    st.caption("Plan the month · track daily · recover lapsed outlets")
    st.divider()

    # ── Phase selector ──
    phase = st.radio(
        "View mode",
        ["📅 Plan (Day 1)", "📊 Track (mid-month)", "📋 Action List", "🔄 Review (Month-end)"],
        horizontal=True,
        key="ops_phase",
    )

    # ── Month + salesman filter ──
    today = date.today()
    default_month = today.replace(day=1) if phase != "🔄 Review (Month-end)" \
        else (today.replace(day=1) - timedelta(days=1)).replace(day=1)

    c1, c2 = st.columns([1, 3])
    with c1:
        op_month = st.date_input(
            "Operating month",
            value=default_month,
            key="ops_month",
        )
    if isinstance(op_month, (tuple, list)):
        op_month = op_month[0] if op_month else default_month

    all_sm = list(SALESMAN_MAP.keys())
    with c2:
        selected_sm = st.multiselect(
            "Salesman",
            options=all_sm,
            default=all_sm if len(all_sm) <= 6 else all_sm[:4],
            key="ops_sm",
        )
    if not selected_sm:
        st.warning("Select at least one salesman above.")
        return

    # ── Load shared data ──
    with st.spinner("Loading operations data…"):
        master = _load_master(13)
        uni_df = _load_active_universe()

    if master.empty or uni_df.empty:
        st.error("No data available. Check DB connection.")
        return

    universes, _ = _build_universes(uni_df)
    st.divider()

    # ── Dispatch ──
    if phase.startswith("📅"):
        _phase_plan(selected_sm, op_month, master, universes)
    elif phase.startswith("📊"):
        _phase_track(selected_sm, op_month, master, universes)
    elif phase.startswith("📋"):
        _phase_action(selected_sm, op_month, master, universes)
    elif phase.startswith("🔄"):
        _phase_review(selected_sm, op_month, master, universes)
