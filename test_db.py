"""Quick connectivity test for pymssql.

Run: python test_db.py
"""
import os, sys
import pymssql
from dotenv import load_dotenv

load_dotenv()

server_raw = os.getenv("DB_SERVER", "")
database   = os.getenv("DB_NAME",   "")
user       = os.getenv("DB_USER",   "")
password   = os.getenv("DB_PASSWORD", "")

if "," in server_raw:
    host, port = server_raw.split(",", 1)
    host, port = host.strip(), int(port.strip())
else:
    host, port = server_raw.strip(), 1433

print(f"Connecting to  : {host}:{port}")
print(f"Database       : {database}")
print(f"User           : {user}")
print()

try:
    conn = pymssql.connect(
        server=host, user=user, password=password,
        database=database, port=str(port),
        timeout=15, login_timeout=10,
    )
    print("Connection     : OK")

    cursor = conn.cursor()

    # 1 — basic ping
    cursor.execute("SELECT 1 AS ping")
    print(f"SELECT 1       : {cursor.fetchone()[0]}")

    # 2 — server version
    cursor.execute("SELECT @@VERSION")
    ver = cursor.fetchone()[0].split("\n")[0]
    print(f"Server version : {ver}")

    # 3 — row counts for key tables
    for tbl in ("TrVocHead", "TrVocItem", "MsBrandMaster", "MsPartyMaster"):
        cursor.execute(f"SELECT COUNT(*) FROM {tbl}")
        print(f"  {tbl:<20}: {cursor.fetchone()[0]:,} rows")

    # 4 — quick sales sanity
    cursor.execute("""
        SELECT COUNT(DISTINCT h.VoucherNo) AS invoices,
               CAST(SUM(vi.TotalAmount) AS BIGINT) AS revenue
        FROM   TrVocHead h
        JOIN   TrVocItem vi
            ON vi.TransTypeID = h.TransTypeID AND vi.VoucherNo = h.VoucherNo
            AND vi.ItemID LIKE 'I%%'
        WHERE  h.TransTypeID IN (18,19,23,35,37,38,39,40,41,44,47,49,51,53)
          AND  h.Cancelled   = 'N'
          AND  vi.FreeItemYN = 'N'
          AND  CAST(h.VoucherDate AS date) >= '2026-04-01'
    """)
    row = cursor.fetchone()
    print(f"\nFY 2026-27 YTD : {row[0]:,} invoices  |  Rs {row[1]:,} revenue")

    conn.close()
    print("\nAll tests PASSED.")
    sys.exit(0)

except Exception as exc:
    print(f"\nFAILED: {exc}", file=sys.stderr)
    sys.exit(1)
