"""KWPL morning digest — 9am owner brief via email.

Queries the ERP for yesterday's activity + MTD vs target + red flags,
then emails a formatted brief to the recipients in DIGEST_TO.

Designed to run from GitHub Actions on a cron schedule (see
.github/workflows/morning_digest.yml). Also runs locally: set the env
vars, run `python scripts/send_digest.py --dry-run` to print the brief
without sending.

Env vars (all required unless noted):
    DB_SERVER        host,port    e.g. 182.156.137.121,5235
    DB_NAME          database     e.g. KW2526
    DB_USER          username
    DB_PASSWORD      password
    SMTP_HOST        SMTP server  default: smtp.gmail.com
    SMTP_PORT        SMTP port    default: 587
    SMTP_USER        email account used to send
    SMTP_PASSWORD    app password (NOT the account password — Gmail
                     requires an "App Password" from account security)
    DIGEST_FROM      From: email  default: SMTP_USER
    DIGEST_TO        comma-separated recipient list
    DIGEST_APP_URL   optional URL to the deployed Streamlit app,
                     included as a link in the brief

CLI flags:
    --dry-run        print the brief to stdout, do not send
    --to <email>     override DIGEST_TO for this one run
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
import pymssql

_IST = timezone(timedelta(hours=5, minutes=30))


# ─── Config ─────────────────────────────────────────────────────────────────

def _cfg(key: str, default: str | None = None, required: bool = False) -> str | None:
    v = os.environ.get(key, default)
    if required and not v:
        print(f"[digest] FATAL: env var {key} is required", file=sys.stderr)
        sys.exit(2)
    return v


DB_SERVER   = _cfg("DB_SERVER",   required=True)
DB_NAME     = _cfg("DB_NAME",     required=True)
DB_USER     = _cfg("DB_USER",     required=True)
DB_PASSWORD = _cfg("DB_PASSWORD", required=True)

SMTP_HOST     = _cfg("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(_cfg("SMTP_PORT", "587") or "587")
SMTP_USER     = _cfg("SMTP_USER")
SMTP_PASSWORD = _cfg("SMTP_PASSWORD")
DIGEST_FROM   = _cfg("DIGEST_FROM", SMTP_USER)
DIGEST_TO     = _cfg("DIGEST_TO", "")
APP_URL       = _cfg("DIGEST_APP_URL", "")

_HOST, _PORT = (DB_SERVER.split(",", 1) + ["1433"])[:2]
_PORT = int(_PORT)


SALES_TYPES = (18, 19, 23, 35, 37, 38, 39, 40, 41, 44, 47, 49, 51, 53)
_TYPE_PH    = ",".join(str(t) for t in SALES_TYPES)

_CASES_EXPR = """(CASE
    WHEN im.ItemDescription LIKE '%50 LT%' OR im.ItemDescription LIKE '%50LT%' THEN vi.TotalBottleQty * (50.0/7.8)
    WHEN im.ItemDescription LIKE '%30 LT%' OR im.ItemDescription LIKE '%30LT%' THEN vi.TotalBottleQty * (30.0/7.8)
    WHEN im.ItemDescription LIKE '%20 LT%' OR im.ItemDescription LIKE '%20LT%' THEN vi.TotalBottleQty * (20.0/7.8)
    ELSE CAST(vi.TotalBottleQty AS decimal(18,4)) / NULLIF(im.BottlesPerCase, 0)
END)"""

PRINCIPAL_NAMES = {
    "C00039": "UBL",
    "C00025": "USL",
    "C00040": "Diageo",
    "C00056": "BF",
}


# ─── DB helper ──────────────────────────────────────────────────────────────

def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = pymssql.connect(server=_HOST, port=str(_PORT),
                           user=DB_USER, password=DB_PASSWORD,
                           database=DB_NAME, timeout=60, login_timeout=15)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
        return pd.DataFrame.from_records(rows, columns=cols)
    finally:
        conn.close()


def _inr(v: float) -> str:
    if v >= 1_00_00_000:
        return f"Rs {v/1_00_00_000:.2f} Cr"
    if v >= 1_00_000:
        return f"Rs {v/1_00_000:.2f} L"
    return f"Rs {v:,.0f}"


# ─── Data blocks ────────────────────────────────────────────────────────────

def yesterday_by_principal(yday: date) -> pd.DataFrame:
    sql = f"""
        SELECT
            b.CompanyID,
            SUM({_CASES_EXPR})              AS Cases,
            SUM(vi.TotalAmount)             AS Revenue
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON vi.TransTypeID = h.TransTypeID AND vi.VoucherNo = h.VoucherNo
            AND vi.ItemID LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({_TYPE_PH})
          AND h.Cancelled = 'N'
          AND CAST(h.VoucherDate AS date) = %s
          AND b.CompanyID IN ('C00025','C00040','C00039','C00056')
        GROUP BY b.CompanyID
    """
    return _query(sql, (yday.isoformat(),))


def mtd_by_principal(month_start: date, today: date) -> pd.DataFrame:
    sql = f"""
        SELECT
            b.CompanyID,
            SUM({_CASES_EXPR})              AS Cases
        FROM TrVocHead h
        JOIN TrVocItem vi
            ON vi.TransTypeID = h.TransTypeID AND vi.VoucherNo = h.VoucherNo
            AND vi.ItemID LIKE 'I%'
            AND vi.FinancialYear = CASE
                WHEN MONTH(h.VoucherDate) >= 4
                THEN CAST(YEAR(h.VoucherDate) AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate)+1 AS VARCHAR)
                ELSE CAST(YEAR(h.VoucherDate)-1 AS VARCHAR)+'-'+CAST(YEAR(h.VoucherDate) AS VARCHAR)
              END
        JOIN MsItemMaster  im ON im.ItemID = vi.ItemID
        JOIN MsBrandMaster b  ON b.BrandID = im.BrandID
        WHERE h.TransTypeID IN ({_TYPE_PH})
          AND h.Cancelled = 'N'
          AND h.VoucherDate BETWEEN %s AND %s
          AND b.CompanyID IN ('C00025','C00040','C00039','C00056')
        GROUP BY b.CompanyID
    """
    return _query(sql, (month_start.isoformat(), today.isoformat()))


def collections_yesterday(yday: date) -> float:
    # Receipts: TransType 2 (bank receipts) + 6 (cash receipts) — party Cr side
    sql = """
        SELECT ISNULL(SUM(CAST(d.Amount AS float)), 0) AS Total
        FROM TrVocHead h
        JOIN TrVocDetail d
          ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        WHERE h.TransTypeID IN (2, 6)
          AND h.Cancelled = 'N'
          AND CAST(h.VoucherDate AS date) = %s
          AND d.DrCrIndicator = 'C'
          AND d.PartyID LIKE 'D%'
    """
    df = _query(sql, (yday.isoformat(),))
    return float(df.iloc[0]["Total"]) if not df.empty else 0.0


def biggest_overdue() -> dict | None:
    sql = f"""
        SELECT TOP 1
            d.PartyID,
            ISNULL(p.PartyName, '')                          AS PartyName,
            CAST(d.BalanceAmount AS float)                   AS Owed,
            DATEDIFF(DAY,
                     COALESCE(CAST(h.TPDate AS date), CAST(h.VoucherDate AS date)),
                     CAST(GETDATE() AS date))                AS AgeDays
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID
                          AND h.VoucherNo   = d.VoucherNo
                          AND h.FinancialYear = d.FinancialYear
        LEFT JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        WHERE d.DrCrIndicator = 'D'
          AND d.PartyID LIKE 'D%'
          AND d.BalanceAmount > 0.5
          AND d.TransTypeID IN ({_TYPE_PH})
          AND h.Cancelled = 'N'
        ORDER BY d.BalanceAmount DESC
    """
    df = _query(sql)
    if df.empty:
        return None
    r = df.iloc[0]
    return {"party": r["PartyName"] or r["PartyID"],
            "owed": float(r["Owed"]),
            "age":  int(r["AgeDays"] or 0)}


def red_flag_counts() -> dict:
    # Banned parties with outstanding
    banned = _query(f"""
        SELECT COUNT(DISTINCT d.PartyID) AS N,
               ISNULL(SUM(CAST(d.BalanceAmount AS float)), 0) AS Amt
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID AND h.VoucherNo = d.VoucherNo AND h.FinancialYear = d.FinancialYear
        JOIN MsPartyMaster p ON p.PartyID = d.PartyID
        WHERE p.BannedByAssoc = 'Y'
          AND d.DrCrIndicator = 'D' AND d.BalanceAmount > 0.5
          AND d.PartyID LIKE 'D%'
          AND d.TransTypeID IN ({_TYPE_PH})
          AND h.Cancelled = 'N'
    """).iloc[0]

    # 90+ overdue (bill-driven, mirrors _fifo_unpaid)
    overdue = _query(f"""
        SELECT COUNT(DISTINCT d.PartyID) AS N,
               ISNULL(SUM(CAST(d.BalanceAmount AS float)), 0) AS Amt
        FROM TrVocDetail d
        JOIN TrVocHead   h ON h.TransTypeID = d.TransTypeID AND h.VoucherNo = d.VoucherNo AND h.FinancialYear = d.FinancialYear
        WHERE d.DrCrIndicator = 'D' AND d.BalanceAmount > 0.5
          AND d.PartyID LIKE 'D%'
          AND d.TransTypeID IN ({_TYPE_PH})
          AND h.Cancelled = 'N'
          AND DATEDIFF(DAY,
                       COALESCE(CAST(h.TPDate AS date), CAST(h.VoucherDate AS date)),
                       CAST(GETDATE() AS date)) >= 90
    """).iloc[0]

    # Cheque bounces in last 30 days
    bounces = _query("""
        SELECT COUNT(DISTINCT CONCAT(h.TransTypeID, '-', h.VoucherNo)) AS N,
               ISNULL(SUM(CAST(d.Amount AS float)), 0) AS Amt
        FROM TrVocHead h
        JOIN TrVocDetail d ON d.TransTypeID = h.TransTypeID AND d.VoucherNo = h.VoucherNo
        WHERE h.TransTypeID = 12 AND h.Cancelled = 'N'
          AND h.VoucherDate >= DATEADD(DAY, -30, GETDATE())
          AND d.DrCrIndicator = 'D' AND d.PartyID LIKE 'D%'
    """).iloc[0]

    return {
        "banned":   {"n": int(banned["N"]),  "amt": float(banned["Amt"])},
        "overdue":  {"n": int(overdue["N"]), "amt": float(overdue["Amt"])},
        "bounces":  {"n": int(bounces["N"]), "amt": float(bounces["Amt"])},
    }


def load_month_targets(month_str: str) -> dict[str, int]:
    """Read data/principal_targets.json for the month; return name -> total_cases."""
    p = Path(__file__).resolve().parent.parent / "data" / "principal_targets.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for key, entry in data.items():
        if key.endswith(f"__{month_str}"):
            principal = key.rsplit("__", 1)[0]
            out[principal] = int(entry.get("total_cases", 0))
    return out


# ─── Formatters ─────────────────────────────────────────────────────────────

# Long-name → short-name for target lookup vs SQL principal names
_LONG_TO_SHORT = {
    "United Breweries": "UBL",
    "United Spirits":   "USL",
    "Diageo":           "Diageo",
    "Brown-Forman":     "BF",
}


def build_brief(today: date) -> dict:
    yday        = today - timedelta(days=1)
    month_start = today.replace(day=1)
    month_str   = today.strftime("%Y-%m")

    y_by_p       = yesterday_by_principal(yday)
    mtd_by_p     = mtd_by_principal(month_start, today)
    y_total      = float(y_by_p["Cases"].sum()) if not y_by_p.empty else 0.0
    y_rev        = float(y_by_p["Revenue"].sum()) if not y_by_p.empty else 0.0
    coll_yday    = collections_yesterday(yday)
    overdue_top  = biggest_overdue()
    flags        = red_flag_counts()
    targets_raw  = load_month_targets(month_str)  # keyed by long names
    targets      = {_LONG_TO_SHORT.get(k, k): v for k, v in targets_raw.items()}

    # Per-principal snapshot
    per_principal = []
    y_map   = {r["CompanyID"]: float(r["Cases"]) for _, r in y_by_p.iterrows()} if not y_by_p.empty else {}
    mtd_map = {r["CompanyID"]: float(r["Cases"]) for _, r in mtd_by_p.iterrows()} if not mtd_by_p.empty else {}
    for cid, short in PRINCIPAL_NAMES.items():
        y_cs   = y_map.get(cid, 0.0)
        mtd_cs = mtd_map.get(cid, 0.0)
        tgt    = targets.get(short, 0)
        pct    = (mtd_cs / tgt * 100) if tgt else 0.0
        per_principal.append({"short": short, "y_cs": y_cs, "mtd_cs": mtd_cs,
                              "tgt": tgt, "pct": pct})

    return {
        "date":          today,
        "yesterday":     yday,
        "y_total":       y_total,
        "y_rev":         y_rev,
        "per_principal": per_principal,
        "collections":   coll_yday,
        "biggest_overdue": overdue_top,
        "flags":         flags,
    }


def format_text(b: dict) -> str:
    lines = []
    lines.append(f"KWPL — {b['date']:%d %b %Y} briefing")
    lines.append("=" * 45)
    lines.append("")
    lines.append(f"YESTERDAY ({b['yesterday']:%a %d %b})")
    lines.append(f"  Total: {b['y_total']:,.0f} cases · {_inr(b['y_rev'])}")
    line = "  "
    for p in b["per_principal"]:
        line += f"{p['short']}: {p['y_cs']:,.0f}  "
    lines.append(line.rstrip())
    lines.append("")
    lines.append("MONTH SO FAR")
    for p in b["per_principal"]:
        tgt_txt = f" of {p['tgt']:,}" if p["tgt"] else " (no target set)"
        pct_txt = f" — {p['pct']:.1f}%" if p["tgt"] else ""
        lines.append(f"  {p['short']:8} {p['mtd_cs']:>7,.0f} cs{tgt_txt}{pct_txt}")
    lines.append("")
    lines.append("CASH")
    lines.append(f"  Collections yesterday: {_inr(b['collections'])}")
    if b["biggest_overdue"]:
        o = b["biggest_overdue"]
        lines.append(f"  Biggest overdue: {o['party']} — {_inr(o['owed'])} ({o['age']} days)")
    lines.append("")
    lines.append("RED FLAGS")
    f = b["flags"]
    lines.append(f"  Banned & owe:    {f['banned']['n']} parties · {_inr(f['banned']['amt'])}")
    lines.append(f"  90+ overdue:     {f['overdue']['n']} parties · {_inr(f['overdue']['amt'])}")
    lines.append(f"  Cheque bounces:  {f['bounces']['n']} in last 30d · {_inr(f['bounces']['amt'])}")
    if APP_URL:
        lines.append("")
        lines.append(f"Open the dashboard: {APP_URL}")
    return "\n".join(lines)


def format_html(b: dict) -> str:
    def _row(cells, header=False, weight=None):
        tag = "th" if header else "td"
        w   = f"font-weight:{weight};" if weight else ""
        cs  = "".join(f"<{tag} style='padding:4px 10px; text-align:left; {w}'>{c}</{tag}>"
                      for c in cells)
        return f"<tr>{cs}</tr>"

    per_principal_rows = "".join(
        _row([p["short"],
              f"{p['y_cs']:,.0f}",
              f"{p['mtd_cs']:,.0f}",
              f"{p['tgt']:,}" if p["tgt"] else "—",
              f"{p['pct']:.1f}%" if p["tgt"] else "—"])
        for p in b["per_principal"])

    f = b["flags"]
    o = b["biggest_overdue"] or {}
    over_line = (f"{o['party']} — {_inr(o['owed'])} ({o['age']} days)"
                 if o else "None")

    app_link = (f"<p style='margin-top:20px'>"
                f"<a href='{APP_URL}' style='color:#1B4F72; font-weight:600'>"
                f"Open the dashboard →</a></p>" if APP_URL else "")

    return f"""<!doctype html>
<html><body style='font-family:system-ui,-apple-system,sans-serif; color:#111827; max-width:640px'>
<h2 style='color:#1B4F72; border-bottom:3px solid #E8A838; padding-bottom:8px'>
  KWPL — {b['date']:%d %b %Y} briefing
</h2>

<h3 style='color:#374151; margin-top:24px'>Yesterday ({b['yesterday']:%a %d %b})</h3>
<p style='font-size:1.3rem; margin:6px 0'>
  <b>{b['y_total']:,.0f}</b> cases · <b>{_inr(b['y_rev'])}</b>
</p>

<h3 style='color:#374151; margin-top:24px'>Month so far</h3>
<table style='border-collapse:collapse; font-size:0.9rem'>
  {_row(['Principal', 'Yday cs', 'MTD cs', 'Target', '% of tgt'], header=True, weight=600)}
  {per_principal_rows}
</table>

<h3 style='color:#374151; margin-top:24px'>Cash</h3>
<p style='margin:4px 0'>Collections yesterday: <b>{_inr(b['collections'])}</b></p>
<p style='margin:4px 0'>Biggest overdue: <b>{over_line}</b></p>

<h3 style='color:#374151; margin-top:24px'>Red flags</h3>
<ul style='margin:6px 0'>
  <li>Banned & owe: <b>{f['banned']['n']}</b> parties · <b>{_inr(f['banned']['amt'])}</b></li>
  <li>90+ overdue: <b>{f['overdue']['n']}</b> parties · <b>{_inr(f['overdue']['amt'])}</b></li>
  <li>Cheque bounces: <b>{f['bounces']['n']}</b> in last 30d · <b>{_inr(f['bounces']['amt'])}</b></li>
</ul>
{app_link}

<p style='color:#9ca3af; font-size:0.75rem; margin-top:24px; border-top:1px solid #e5e7eb; padding-top:8px'>
  Generated at {datetime.now(_IST):%d %b %Y, %H:%M IST}.
</p>
</body></html>"""


# ─── Sender ────────────────────────────────────────────────────────────────

def send_email(subject: str, text_body: str, html_body: str, to: list[str]) -> None:
    if not (SMTP_USER and SMTP_PASSWORD):
        print("[digest] FATAL: SMTP_USER / SMTP_PASSWORD not set", file=sys.stderr)
        sys.exit(2)
    if not to:
        print("[digest] FATAL: no recipients (set DIGEST_TO)", file=sys.stderr)
        sys.exit(2)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = DIGEST_FROM or SMTP_USER
    msg["To"]      = ", ".join(to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)
    print(f"[digest] sent to {len(to)} recipient(s): {', '.join(to)}")


# ─── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the brief to stdout instead of sending.")
    ap.add_argument("--to", default=None,
                    help="Override DIGEST_TO for this one run "
                         "(comma-separated).")
    args = ap.parse_args()

    today = datetime.now(_IST).date()
    b     = build_brief(today)
    text  = format_text(b)
    html  = format_html(b)

    if args.dry_run:
        print(text)
        return

    to_str = args.to or DIGEST_TO or ""
    to = [t.strip() for t in to_str.split(",") if t.strip()]
    subject = f"KWPL brief · {today:%d %b} · {b['y_total']:,.0f} cs yesterday"
    send_email(subject, text, html, to)


if __name__ == "__main__":
    main()
