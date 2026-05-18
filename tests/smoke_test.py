"""Page-level smoke test — verify every render() function completes.

Stubs Streamlit so we can invoke render() without a running server, then
walks through each tab/sub-tab. Reports which pages pass / fail and why.

Usage:
    python tests/smoke_test.py
"""
from __future__ import annotations

import sys, traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ──────────────────────────────────────────────────────────────────────────
# Streamlit stub — minimal subset for render-path coverage
# ──────────────────────────────────────────────────────────────────────────
class _Stub:
    def __getattr__(self, k):
        if k in {"sidebar", "container"}: return self
        def _noop(*a, **kw):
            # Force Styler eval so renamed-column bugs surface
            if k == "dataframe" and a and hasattr(a[0], "to_html"):
                try: a[0].to_html()
                except Exception as e:
                    raise RuntimeError(f"styler crash in dataframe: {e}") from e
            return self
        return _noop
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter([self]*7)
    def __getitem__(self, k): return self
    def columns(self, n, *a, **k):
        if isinstance(n, int): return [self]*n
        return [self]*len(n)
    def tabs(self, names): return [self for _ in names]
    def set_page_config(self, *a, **k): pass
    def date_input(self, *a, **k):
        v = k.get("value", date.today())
        return v
    def selectbox(self, l, options, **k): return options[0] if options else None
    def multiselect(self, l, options, **k): return k.get("default") or []
    def radio(self, l, options, **k): return options[0]
    def slider(self, l, *a, **k):
        return k.get("value", k.get("min_value", a[0] if a else 0))
    def number_input(self, l, *a, **k):
        return k.get("value", k.get("min_value", a[0] if a else 0))
    def text_input(self, l, value="", **k): return value
    def button(self, *a, **k): return False
    def form_submit_button(self, *a, **k): return False
    def form(self, *a, **k): return self
    def expander(self, *a, **k): return self
    def spinner(self, *a, **k): return self
    def progress(self, *a, **k): return self
    def download_button(self, *a, **k): return False
    def metric(self, *a, **k): return self
    def plotly_chart(self, *a, **k): return self
    def markdown(self, *a, **k): return self
    def info(self, *a, **k): return self
    def success(self, *a, **k): return self
    def warning(self, *a, **k): return self
    def error(self, *a, **k): return self
    def caption(self, *a, **k): return self
    def title(self, *a, **k): return self
    def subheader(self, *a, **k): return self
    def header(self, *a, **k): return self
    def divider(self): return self
    def cache_data(self, *a, **k):
        def deco(f):
            f.clear = lambda: None
            return f
        return deco
    def cache_resource(self, *a, **k):
        def deco(f):
            f.clear = lambda: None
            return f
        return deco
    @property
    def session_state(self): return {}
    @property
    def column_config(self):
        class _CC:
            @staticmethod
            def Column(label, **k): return label
        return _CC()
    def rerun(self): pass
    def stop(self): pass

stub = _Stub()
import streamlit
for attr in dir(stub):
    if attr.startswith("_"): continue
    setattr(streamlit, attr, getattr(stub, attr))
streamlit.session_state = {}

# Stub openpyxl-backed ExcelWriter so Excel exports don't crash locally
import pandas as _pd
class _NoOpExcelWriter:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
_pd.ExcelWriter = _NoOpExcelWriter
_pd.DataFrame.to_excel = lambda self, *a, **k: None


# ──────────────────────────────────────────────────────────────────────────
# Test runner
# ──────────────────────────────────────────────────────────────────────────
RESULTS = []

def test_page(name: str, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        RESULTS.append((name, "PASS", None))
        print(f"  PASS  {name}")
    except Exception as e:
        tb = traceback.format_exc()
        RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        # Show short traceback
        for line in tb.splitlines()[-5:]:
            print(f"        {line}")


print("=" * 70)
print("SMOKE TEST — all page renders")
print("=" * 70)

# ── Module imports ───────────────────────────────────────────────────────
print("\n[1/3] Module imports")
modules = ["sales","salesman","distribution","principal","operations",
           "sales_plan","purchase","inventory","debtors"]
for mod in modules:
    try:
        m = __import__(f"src.{mod}", fromlist=["render"])
        assert hasattr(m, "render")
        print(f"  PASS  src.{mod}.render")
        RESULTS.append((f"import:{mod}", "PASS", None))
    except Exception as e:
        print(f"  FAIL  src.{mod}: {type(e).__name__}: {e}")
        RESULTS.append((f"import:{mod}", "FAIL", str(e)))

# ── Page renders ─────────────────────────────────────────────────────────
print("\n[2/3] Page render() calls")
from src.sales       import render as r_sales
from src.salesman    import render as r_salesman
from src.distribution import render as r_distribution
from src.principal   import render as r_principal
from src.operations  import render as r_operations
from src.sales_plan  import render as r_sales_plan
from src.purchase    import render as r_purchase
from src.inventory   import render as r_inventory
from src.debtors     import render as r_debtors

test_page("Sales Overview",   r_sales)
test_page("Team Performance", r_salesman)
test_page("Distribution",     r_distribution)
test_page("Meeting Pack",     r_principal)
test_page("Operations Rhythm", r_operations)
test_page("Sales Plan",       r_sales_plan)
test_page("Purchase",         r_purchase)
test_page("Inventory",        r_inventory)
test_page("Debtors Ageing",   r_debtors)

# ── Summary ──────────────────────────────────────────────────────────────
print("\n[3/3] Summary")
passes = sum(1 for _, s, _ in RESULTS if s == "PASS")
fails  = sum(1 for _, s, _ in RESULTS if s == "FAIL")
print(f"  PASS: {passes:>3}")
print(f"  FAIL: {fails:>3}")

if fails:
    print("\n  Failures:")
    for name, status, err in RESULTS:
        if status == "FAIL":
            print(f"    - {name}: {err}")
    sys.exit(1)
print("\nALL PAGES RENDER CLEANLY")
