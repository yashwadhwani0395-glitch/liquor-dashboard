# LiquorBiz Dashboard

A Streamlit analytics dashboard for liquor distribution businesses, connecting to SQL Server.

## Prerequisites

- Python 3.10+
- [ODBC Driver 17 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) installed on your machine

## Setup

### 1. Create and activate a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure database credentials

Edit the `.env` file in the project root:

```
DB_SERVER=your_sql_server_host
DB_NAME=your_database_name
DB_USER=your_sql_login_username
DB_PASSWORD=your_password
```

> `.env` is git-ignored — your credentials will never be committed.

### 4. Run the app

```bash
streamlit run app.py
```

The dashboard opens at `http://localhost:8501` by default.

## Project Structure

```
liquor-dashboard/
├── app.py                  # Main entry point & sidebar navigation
├── db.py                   # DB connection + run_query() helper
├── requirements.txt
├── .env                    # DB credentials (git-ignored)
├── .gitignore
├── pages/
│   └── sales.py            # Sales & Revenue page
├── utils/
│   └── helpers.py          # Shared utilities (KPI cards, formatters, date picker)
└── .streamlit/
    └── config.toml         # Dark theme & server config
```

## Swapping Mock Data for Real Queries

Each page uses a `_mock_*()` function while the DB is not configured. Once your `.env`
is filled in, uncomment the `run_query(...)` block at the top of the data section in each
page and delete the mock call.

## Adding New Pages

1. Create `pages/your_page.py` with a `render()` function.
2. Add the page name to the `options` list in `app.py`.
3. Add an `elif page == "Your Page":` routing block in `app.py`.
