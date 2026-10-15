# Employee Attendance & Analytics API

A Python API built with FastAPI and MongoDB for employee attendance tracking. It supports employee records, punch-in and punch-out, manual attendance corrections with an audit trail, and attendance analytics using MongoDB aggregation pipelines.

## Features

- Create and manage employee records.
- Record employee punch-in and punch-out events.
- Calculate attendance fields such as work hours, late minutes, overtime, and half-day status.
- Correct an attendance record with a reason and the identity of the person making the correction.
- Keep a history of attendance corrections.
- Provide department and monthly attendance analytics, late rankings, and department trends.
- Provide MongoDB explain output for supported analytics queries.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Git

## Setup

From the project root, create and activate a virtual environment in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set the MongoDB connection settings for the current PowerShell session:

```powershell
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
```

For local development, the application can also load settings from a `.env` file if `python-dotenv` is installed. Do not commit `.env` files, database credentials, or other secrets.

## Seed sample data

With MongoDB running and the environment variables set:

```powershell
python sample_seed.py
```

Review the seed script before running it against a database that contains data you need to keep.

## Run the API

```powershell
uvicorn app.main:app --reload --port 8000
```

Open the interactive API documentation at:

- Swagger UI: <http://127.0.0.1:8000/docs>
- OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>

## Project layout

```text
app/
  main.py                 FastAPI application and API routes
sample_data/              Sample employee and attendance data
sample_seed.py            Loads sample data into MongoDB
openapi.yaml              API contract
DATA_MODEL.md             Data model notes
REVIEW.md                 Review of the supplied starter code
DECISIONS.md              Design decisions and trade-offs
requirements.txt          Python dependencies
```

## Configuration

| Variable | Purpose | Example |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

## Verification status

The employee creation, punch-in, punch-out, and correction endpoints returned successful responses during a local Swagger UI run. A correction also added an audit entry to the attendance record.

That manual run exposed suspicious `late_minutes` and `overtime_minutes` values for a short demo shift. Those calculations require further review, so attendance calculations and analytics should not be considered fully verified yet. The implementation has not been benchmarked against a 100,000-record dataset.

## Assignment files

The repository includes `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this `README.md`, as requested in the assignment. Check `.gitignore` and `git status` before pushing to ensure virtual environments, bytecode caches, `.env` files, and secrets are not committed.
