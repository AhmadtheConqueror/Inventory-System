# Inventory System

Flask-based general inventory, sales, cash, debt, expense, invoice, and reporting system. The app creates a fresh local SQLite database on first run and includes optional Clerk authentication plus a Gemini-powered AI analyst on the Reports page.

## Features

- Track products, quantities, cost prices, selling prices, and expiry dates.
- Sell inventory through a cart workflow.
- Record full and partial customer payments.
- Automatically reduce stock after sales.
- Track cash history as sales, debt payments, expenses, and manual cash snapshots happen.
- Track customers and outstanding debts.
- Track expenses by category.
- Generate invoices and receipts.
- Export sales and monthly asset reports.
- View assets, stock value, debt, revenue, COGS, expenses, gross profit, net profit, daily analysis, and monthly asset trends.
- Ask the Gemini AI assistant business questions from the Reports page.

## Tech Stack

- Python 3.14
- Flask 3
- Flask-Login
- Flask-SQLAlchemy
- SQLite
- Jinja templates
- Bootstrap
- Chart.js
- xhtml2pdf / ReportLab for PDF output
- Gemini API for AI report insights
- Optional Clerk authentication

## Project Structure

```text
.
|-- app.py             Main Flask app, routes, business logic, AI endpoint
|-- models.py          SQLAlchemy database models
|-- requirements.txt   Python dependencies
|-- start_app.bat      Windows local launcher, runs Flask on port 5052
|-- .env               Local placeholder config; replace values locally
|-- templates/         HTML pages
`-- static/            CSS and JavaScript
```

Runtime-generated files such as `instance/database.db`, `backups/`, `.venv/`, logs, and Python caches are intentionally ignored for GitHub.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --port 5052
```

Then open:

```text
http://127.0.0.1:5052
```

You can also run the included Windows helper:

```powershell
.\start_app.bat
```

## Environment Variables

The app reads `.env` from the project root.

```env
CLERK_PUBLISHABLE_KEY=your_clerk_publishable_key
CLERK_SECRET_KEY=your_clerk_secret_key
CLERK_FRONTEND_API=https://your-clerk-frontend-api

FLASK_SECRET_KEY=change-this-to-a-long-random-value

GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash
```



## Database

The local SQLite database is generated at:

```text
instance/database.db
```

Main tables:

- `user`
- `product`
- `customer`
- `sale`
- `sale_item`
- `debt`
- `expense`
- `cash_history`
- `setting`
- `monthly_asset_override`

The app uses `db.create_all()` on startup, so a clean deploy starts with an empty database automatically.

## Useful Commands

Check Python syntax:

```powershell
python -m py_compile app.py models.py
```

List Flask routes:

```powershell
python -m flask --app app routes
```

Run with Gunicorn on a Linux host:

```bash
gunicorn app:app
```


