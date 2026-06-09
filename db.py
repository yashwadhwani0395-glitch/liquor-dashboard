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
import time
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
    """Return a cached pymssql connection. Retries a few times on transient
    connect failures (common on Streamlit Cloud, where the DB momentarily
    refuses a fresh connect under load) before giving up. Returns None only
    if every attempt fails."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return pymssql.connect(
                server=_HOST,
                user=_USER,
                password=_PASS,
                database=_DBNAME,
                port=str(_PORT),
                timeout=30,
                login_timeout=15,
            )
        except Exception as exc:
            last_exc = exc
            print(f"[db] Connection attempt {attempt+1}/3 failed: {exc}",
                  file=sys.stderr)
            time.sleep(0.5 * (2 ** attempt))
    print(f"[db] Connection failed after 3 attempts: {last_exc}", file=sys.stderr)
    return None


# ── Connection status helper ───────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)   # avoid re-probing on every rerun
def get_connection_status() -> bool:
    """Return True if the DB is reachable, False otherwise.
    Cached for 60s — call get_connection.clear() to force a fresh probe."""
    try:
        conn = get_connection()
        return conn is not None
    except Exception:
        return False


# ── Query runner ───────────────────────────────────────────────────────────

# Transient SQL Server errors that are safe (and expected) to retry.
# 1205 = deadlock victim. The server has already rolled the txn back; the
#        canonical fix is to rerun. Retrying is the documented Microsoft
#        guidance — see "Handling Deadlocks" in SQL Server docs.
# 1222 = lock request timeout.
_RETRYABLE_SQL_ERRORS = (1205, 1222)

# DB-Lib (pymssql) CONNECTION-level error codes. These mean the cached
# connection has gone dead — extremely common on Streamlit Cloud, where the
# st.cache_resource connection sits idle between reruns and the SQL Server /
# network silently drops it. The cure is to throw the dead connection away,
# reconnect, and retry — which run_query already does on every retry path.
#   20047 = DBPROCESS is dead or not enabled
#   20003 = connection to the database failed / closed
#   20002 = Adaptive Server connection failed
#   20017 = unexpected EOF from the server
#   20006 = write to the server failed
#   20009 = read/write operation failed
#   20018 = general SQL Server error (often follows a dropped connection)
_RETRYABLE_CONN_ERRORS = (20047, 20003, 20002, 20017, 20006, 20009, 20018)

# Substrings that indicate a dead/dropped connection regardless of code.
_CONN_ERROR_HINTS = (
    "dbprocess is dead", "connection", "unexpected eof",
    "write to the server failed", "not connected", "closed",
    "adaptive server", "broken pipe", "timed out",
)

# Retries: 5 attempts with backoff 1.5s, 3s, 6s, 12s (~22s total). The earlier
# 3-attempt / 0.5s base was too short — Streamlit Cloud logs showed the
# generic "(0, b'Unknown error')" drops needing more time for the SQL server
# to recover before the next attempt would succeed.
_MAX_RETRIES   = 5
_RETRY_BACKOFF = 1.5  # seconds; doubles each retry


def _is_retryable(exc: Exception) -> bool:
    """True if exc is a transient error we should retry (deadlock/lock
    timeout OR a dead-connection error that a reconnect will fix)."""
    # pymssql wraps the error number in args[0]
    try:
        code = exc.args[0] if exc.args else None
    except Exception:
        code = None
    if code in _RETRYABLE_SQL_ERRORS or code in _RETRYABLE_CONN_ERRORS:
        return True
    # OperationalError / InterfaceError are pymssql's connection-failure
    # classes — almost always cured by reconnecting.
    if isinstance(exc, (pymssql.OperationalError, pymssql.InterfaceError)):
        return True
    # Message-based safety net for deadlocks and dropped connections.
    msg = str(exc).lower()
    if "deadlocked" in msg or "deadlock victim" in msg:
        return True
    return any(hint in msg for hint in _CONN_ERROR_HINTS)


def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute a parameterised SQL query and return a DataFrame.

    Converts pyodbc-style ? placeholders to pymssql-style %s automatically,
    so page code written for pyodbc works without changes.

    On transient SQL Server errors (deadlock victim 1205, lock timeout 1222)
    the query is retried up to _MAX_RETRIES times with exponential backoff.

    On persistent failure RAISES — never returns an empty DataFrame as
    a silent stand-in for an error. This is critical because callers wrap
    us in @st.cache_data; returning empty would poison the cache for the
    full TTL (up to an hour) and the dashboard would silently show "0".
    Streamlit does not cache results when the function raises, so re-raising
    forces the next call to try the DB again.
    """
    # Acquire a connection, retrying transient None (cached dead connection /
    # momentary connect refusal) by clearing the cache and reconnecting.
    conn = get_connection()
    if conn is None:
        for attempt in range(_MAX_RETRIES):
            wait = _RETRY_BACKOFF * (2 ** attempt)
            print(
                f"[db.run_query] no connection (attempt "
                f"{attempt+1}/{_MAX_RETRIES}), reconnecting in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            get_connection.clear()
            conn = get_connection()
            if conn is not None:
                break
        if conn is None:
            raise RuntimeError(
                "Database connection unavailable. Check DB credentials / network."
            )

    # pymssql uses %s; callers use ? (pyodbc convention)
    if params:
        sql = sql.replace("?", "%s")

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params or ())
                cols = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
            return pd.DataFrame.from_records(rows, columns=cols)
        except Exception as exc:
            last_exc = exc
            if _is_retryable(exc) and attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                print(
                    f"[db.run_query] transient error (attempt "
                    f"{attempt+1}/{_MAX_RETRIES}), retrying in {wait}s: {exc}",
                    file=sys.stderr,
                )
                time.sleep(wait)
                # Explicitly close the dead connection BEFORE clearing the
                # cache and reconnecting — otherwise the underlying TCP
                # socket can sit half-open and the next attempt inherits the
                # broken state. .close() is safe to call on already-dead
                # connections (it just no-ops).
                try:
                    conn.close()
                except Exception:
                    pass
                get_connection.clear()
                conn = get_connection()
                if conn is None:
                    raise RuntimeError(
                        "Reconnect failed after transient DB error."
                    ) from exc
                continue
            # Non-retryable, or out of retries — clear connection and raise
            # so @st.cache_data does NOT cache an empty result.
            print(f"[db.run_query] {exc}", file=sys.stderr)
            get_connection.clear()
            raise
    # Unreachable, but keep mypy happy
    raise RuntimeError("run_query exhausted retries") from last_exc
