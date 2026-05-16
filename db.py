import os
import pyodbc
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()


def _get_connection_string() -> str:
    server = os.getenv("DB_SERVER", "localhost")
    database = os.getenv("DB_NAME", "")
    username = os.getenv("DB_USER", "")
    password = os.getenv("DB_PASSWORD", "")
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        f"TrustServerCertificate=yes;"
    )


@st.cache_resource(show_spinner=False)
def get_connection() -> pyodbc.Connection | None:
    try:
        conn = pyodbc.connect(_get_connection_string(), timeout=10)
        return conn
    except pyodbc.Error as e:
        st.error(
            f"**Database connection failed.** Check your .env credentials.\n\n"
            f"`{e}`"
        )
        return None


def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute a SQL query and return results as a DataFrame.

    Falls back to an empty DataFrame on error so pages can degrade gracefully.
    """
    conn = get_connection()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(sql, conn, params=params)
    except Exception as e:
        st.error(f"**Query failed:** `{e}`")
        return pd.DataFrame()
