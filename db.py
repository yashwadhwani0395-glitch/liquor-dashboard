"""Database connection module.

Credential resolution order:
1. st.secrets["database"]  — used on Streamlit Cloud (secrets entered via UI)
2. .env / environment vars — used locally
"""

import os
import sys
import pyodbc
import pandas as pd

# ── Credential resolution ──────────────────────────────────────────────────
try:
    import streamlit as st
    _db   = st.secrets["database"]
    SERVER   = _db["server"]
    DATABASE = _db["name"]
    USERNAME = _db["user"]
    PASSWORD = _db["password"]
except Exception:
    from dotenv import load_dotenv
    load_dotenv()
    SERVER   = os.getenv("DB_SERVER")
    DATABASE = os.getenv("DB_NAME")
    USERNAME = os.getenv("DB_USER")
    PASSWORD = os.getenv("DB_PASSWORD")


def _conn_str() -> str:
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={SERVER};"
        f"DATABASE={DATABASE};"
        f"UID={USERNAME};"
        f"PWD={PASSWORD};"
        f"TrustServerCertificate=yes;"
    )


def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a parameterised SQL query and return a DataFrame.

    Creates a fresh connection per call; callers cache results via
    @st.cache_data so connections only open on cache misses.
    Returns an empty DataFrame on any error (logged to stderr).
    """
    try:
        with pyodbc.connect(_conn_str(), timeout=15) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            cols = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
        return pd.DataFrame.from_records(rows, columns=cols)
    except Exception as exc:
        print(f"[db.run_query] {exc}", file=sys.stderr)
        return pd.DataFrame()
