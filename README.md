# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. The API supports employee CRUD, punch-in/out, audited corrections, monthly and department analytics, late ranking, department trends, and MongoDB explain output.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
python sample_seed.py
uvicorn app.main:app --port 8000