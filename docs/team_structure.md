# KWPL Sales Team — Current Structure

_Effective May 2026. This document is the canonical roster for code that
hardcodes team membership (`src/debtors.py`, `src/distribution.py`,
`src/sales_plan.py`, `src/principal.py`). Update here first, then sync
constants._

## Wine Shops (FL-II, LicenseTypeID = 180001)

| Principal | Salesman | SM field | ERP SalesManID |
|---|---|---|---|
| USL | SHASHANK | SM1 | 000014 |
| USL | SACHIN KAMBLE | SM1 | 000012 |
| Diageo + BF | AJAY | SM2 | 000030 |
| Diageo + BF | DEEPAK PATIL | SM2 | 000004 |

## Permit Rooms (FL-III, LicenseTypeID = 180002) — USL + Diageo

| Salesman | SM field | ERP SalesManID |
|---|---|---|
| TULSIRAM | SM1 | 000024 |
| SAURABH | SM1 | 000018 |
| MIRAN DMELLO | SM1 | 000039 |
| PRASHANT THORAT | SM1 | 000025 |
| ATISH | SM1 | 000033 |

_(5 people post-transition — was 6 with Rohit Lakhan before he moved to KW Institution.)_

## UBL KW Beer (FL-BR-II, LicenseTypeID = 180004)

| Salesman | SM field | ERP SalesManID |
|---|---|---|
| ABID (a.k.a. AABID) | SM3 | 000028 |
| OMKAR PAWAR | SM3 | 000032 |

## KW Institution — UBL + BF (NOT Diageo)

| Salesman | SM field | ERP SalesManID | Notes |
|---|---|---|---|
| SHASHANK DESAI | SM3 | 000036 | |
| PRANAV | SM3 | 000026 | |
| RAHUL GHONE (ERP: `RAHUL GONE`) | SM3 | 000037 | Moved from PCMC May 2026 |
| ROHIT LAKHAN | SM3 | 000038 | Reactivated May 2026 |

## PCMC Institution — UBL only

| Salesman | SM field | ERP SalesManID | Notes |
|---|---|---|---|
| GAJENDRA DAS | SM3 | 000042 | |
| AMOL SATHE | SM3 | 000041 | |
| PIYUSH ARORA | SM3 | 000043 | New hire May 2026, PCMC-west belt (Hinjewadi/Wakad/Mulshi/Bhavdhan) |

---

## ID-reuse on team transition (May 2026)

The ERP `MsSalesmanMaster.SalesManID` values are reused when people leave. This is the active mapping right now:

| SalesManID | Previously | Now (May 2026) |
|---|---|---|
| 000037 | Anand Raj (left) | RAHUL GONE (promoted from PCMC) |
| 000038 | Deepak Pangare (left) | ROHIT LAKHAN (reactivated) |
| 000043 | Rahul Ghone (now at 000037) | PIYUSH ARORA (new hire) |

Because party records in `MsPartyMaster.SalesManID1/2/3` reference these IDs (not names), attribution automatically follows the role: every Diageo institution party that pointed to 000037 (Anand Raj) now correctly attributes to Rahul Ghone, and so on.

## Non-field names in `MsSalesmanMaster`

These appear in `MsSalesmanMaster` but should NOT be treated as collectable handlers:

- **`SURESH NAIR`** (000040, 558 SM3 parties) — historic SM3 catch-all dump
- **`CROSS SUPPLY`** (000031) — sub-distribution routing marker, not a person
- **`CLOSED OUTLET`** (000035), **`ONE DAY LIC`** (000029) — status markers
- **`Z *`** prefix — ex-employees (Z Rajendra Prasad, Z Kishor Chandak, etc.)
- **`RAJESH`** (000007, 93 parties SM1), **`ROHIT`** (000034, 23 parties SM1) — unclassified (owner review queue)

## Where this list is enforced

| File | Constant | What it controls |
|---|---|---|
| `src/debtors.py` | `KNOWN_FIELD_SALESMEN`, `PLACEHOLDER_NAMES` | Smart-cascade attribution; "Field salesmen only" filter |
| `src/distribution.py` | `SALESMAN_MAP` | Per-salesman universe build for WOD, scoreboard, etc. |
| `src/sales_plan.py` | `PRINCIPAL_TEAMS` | Per-team target allocation in the Sales Plan tab |
| `src/principal.py` | `PRINCIPAL_CONFIG` | Meeting Pack salesman scoreboard composition |

When the team changes, update **`docs/team_structure.md` first**, then sync the four constants above (they all derive from this single source of truth).

## Wine Shop Pair Fallback (debtors.py only)

Wine shop teams share outlets but split principals:

| Pair | USL (SM1) | Diageo + BF (SM2) |
|------|-----------|-------------------|
| A | Sachin Kamble (000012) | Ajay (000030) |
| B | Shashank (000014) | Deepak Patil (000004) |

ERP `MsPartyMaster` has SM2 blank for 115 wine-shop parties
(80 in Pair A, 35 in Pair B). The `WINE_PAIR_FALLBACK` rule in
`src/debtors.py` routes Diageo/BF bills at these parties to the
correct pair-mate.

This rule is **NOT** applied in `sales_plan.py` / `sales.py` — those
modules show sales attribution from the field as-is.

Accountant cleanup path: populate SM2 in `MsPartyMaster` with
the correct partner ID for the 115 affected parties. Once
populated, the explicit SM2 takes precedence (step 1 fires
first) and `WINE_PAIR_FALLBACK` silently bypasses.
