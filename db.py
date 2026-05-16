"""Database connection module.

Credential resolution order:
1. st.secrets["database"]  — Streamlit Cloud (secrets entered via UI)
2. .env / environment vars — local development

Connection is cached at the app level via st.cache_resource.
run_query() translates pyodbc-style ? placeholders to pymssql-style %s
so callers never need to know which driver is active.
"""

import os
import sys
import pymssql
import pandas as pd

# ── Credential resolution ──────────────────────────────────────────────────
try:
    import streamlit as st
    _db      = st.secrets["database"]
    _SERVER  = _db["server"]
    _DBNAME  = _db["name"]
    _USER    = _db["user"]
    _PASS    = _db["password"]
except Exception:
    from dotenv import load_dotenv
    load_dotenv()
    _SERVER  = os.getenv("DB_SERVER", "")
    _DBNAME  = os.getenv("DB_NAME",   "")
    _USER    = os.getenv("DB_USER",   "")
    _PASS    = os.getenv("DB_PASSWORD", "")


def _parse_server(server_str: str) -> tuple[str, int]:
    """Split 'host,port' into (host, port). Defaults to port 1433."""
    if "," in server_str:
        host, port_str = server_str.split(",", 1)
        return host.strip(), int(port_str.strip())
    return server_str.strip(), 1433


_HOST, _PORT = _parse_server(_SERVER)


# ── Cached connection ──────────────────────────────────────────────────────
import streamlit as st   # noqa: E402  (imported again to ensure availability)


@st.cache_resource(show_spinner=False)
def get_connection() -> pymssql.Connection | None:
    """Return a cached pymssql connection. Returns None on failure."""
    try:
        return pymssql.connect(
            server=_HOST,
            user=_USER,
            password=_PASS,
            database=_DBNAME,
            port=str(_PORT),
            timeout=15,
            login_timeout=10,
        )
    except Exception as exc:
        print(f"[db] Connection failed: {exc}", file=sys.stderr)
        return None


# ── Query runner ───────────────────────────────────────────────────────────

def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute a parameterised SQL query and return a DataFrame.

    Converts pyodbc-style ? placeholders to pymssql-style %s automatically,
    so page code written for pyodbc works without changes.
    Returns an empty DataFrame on any error (logged to stderr).
    On connection errors the cached connection is cleared so the next
    call will attempt to reconnect.
    """
    conn = get_connection()
    if conn is None:
        return pd.DataFrame()

    # pymssql uses %s; callers use ? (pyodbc convention)
    if params:
        sql = sql.replace("?", "%s")

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            cols = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
        return pd.DataFrame.from_records(rows, columns=cols)
    except Exception as exc:
        print(f"[db.run_query] {exc}", file=sys.stderr)
        get_connection.clear()   # force reconnect on next call
        return pd.DataFrame()
