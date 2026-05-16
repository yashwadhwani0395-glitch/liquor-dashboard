import os
import sys
import pyodbc
from dotenv import load_dotenv

load_dotenv()

SERVER   = os.getenv("DB_SERVER")
USER     = os.getenv("DB_USER")
PASSWORD = os.getenv("DB_PASSWORD")

SYSTEM_DBS = {"master", "tempdb", "model", "msdb", "ReportServer", "ReportServerTempDB"}

OUTPUT_FILE = "db_structure.txt"

lines = []

def out(text=""):
    print(text)
    lines.append(text)


def make_conn_str(database: str) -> str:
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={SERVER};"
        f"DATABASE={database};"
        f"UID={USER};"
        f"PWD={PASSWORD};"
        f"TrustServerCertificate=yes;"
    )


def list_user_databases(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sys.databases WHERE name NOT IN ({}) ORDER BY name"
                .format(",".join(f"'{d}'" for d in SYSTEM_DBS)))
    return [row[0] for row in cur.fetchall()]


def explore_database(db_name: str):
    out(f"\n{'='*60}")
    out(f"  DATABASE: {db_name}")
    out(f"{'='*60}")
    try:
        conn = pyodbc.connect(make_conn_str(db_name), timeout=10)
    except pyodbc.Error as e:
        out(f"  [ERROR] Could not connect to {db_name}: {e}")
        return

    cur = conn.cursor()

    # Tables and Views
    cur.execute("""
        SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
        FROM   INFORMATION_SCHEMA.TABLES
        ORDER  BY TABLE_TYPE, TABLE_SCHEMA, TABLE_NAME
    """)
    objects = cur.fetchall()

    if not objects:
        out("  (no tables or views found)")
        conn.close()
        return

    current_type = None
    for schema, table, ttype in objects:
        label = "TABLES" if ttype == "BASE TABLE" else "VIEWS"
        if label != current_type:
            out(f"\n  -- {label} --")
            current_type = label

        out(f"\n  [{schema}].[{table}]")

        try:
            cur.execute("""
                SELECT COLUMN_NAME, DATA_TYPE,
                       CHARACTER_MAXIMUM_LENGTH,
                       IS_NULLABLE
                FROM   INFORMATION_SCHEMA.COLUMNS
                WHERE  TABLE_SCHEMA = ? AND TABLE_NAME = ?
                ORDER  BY ORDINAL_POSITION
            """, schema, table)
            cols = cur.fetchall()
            for col_name, data_type, max_len, nullable in cols:
                type_str = data_type
                if max_len:
                    type_str += f"({max_len})" if max_len != -1 else "(MAX)"
                null_str = "NULL" if nullable == "YES" else "NOT NULL"
                out(f"      {col_name:<35} {type_str:<20} {null_str}")
        except pyodbc.Error as e:
            out(f"      [ERROR reading columns: {e}]")

    conn.close()


# ── Main ───────────────────────────────────────────────────────────────────

out("DB Explorer — LiquorBiz")
out(f"Connecting to: {SERVER}")
out(f"User: {USER}")

try:
    master_conn = pyodbc.connect(make_conn_str("master"), timeout=10)
    out("Connection to master: OK\n")
except pyodbc.Error as e:
    out(f"[FATAL] Cannot connect: {e}")
    sys.exit(1)

user_dbs = list_user_databases(master_conn)
master_conn.close()

if not user_dbs:
    out("No user databases found.")
else:
    out(f"User databases found ({len(user_dbs)}): {', '.join(user_dbs)}")
    for db in user_dbs:
        explore_database(db)

out(f"\n{'='*60}")
out("Exploration complete.")
out(f"{'='*60}\n")

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"\n[Saved to {OUTPUT_FILE}]")
