"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from datetime import datetime, timedelta, timezone, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING), ("status", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_dt(ms: int) -> datetime:
    """epoch milliseconds -> UTC datetime, cut to whole seconds (R1)"""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def to_ms(dt: datetime) -> int:
    """datetime from Mongo -> epoch milliseconds"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start          # "06:00" <= "22:00"


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str):
    """Returns (date string, datetime when this shift started), both in IST (R1)."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        if local.time() < time(eh, em):      # before shift end -> belongs to yesterday's shift
            day = day - timedelta(days=1)
    sh, sm = map(int, shift_start.split(":"))
    start_dt = datetime.combine(day, time(sh, sm), tzinfo=IST)
    return day.isoformat(), start_dt


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    seconds = int((punch_in - shift_start_dt).total_seconds())
    return seconds // 60 if seconds > 600 else 0          # strictly more than 10:00 (R2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    eh, em = map(int, shift_end.split(":"))
    end_day = datetime.fromisoformat(date_str).date()
    if is_overnight(shift_start, shift_end):
        end_day = end_day + timedelta(days=1)             # shift ends the next day
    end_dt = datetime.combine(end_day, time(eh, em), tzinfo=IST)
    minutes = int((punch_out - end_dt).total_seconds()) // 60
    return minutes if minutes >= 30 else 0                # R3


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)                                   # R4


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
EPOCH_MIN, EPOCH_MAX = 100000000000, 4102444800000


def check_date(v: str) -> str:
    datetime.strptime(v, "%Y-%m-%d")          # raises ValueError if not a real YYYY-MM-DD date
    return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_RE)
    shift_end: str = Field(default="18:30", pattern=TIME_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_date(cls, v):
        return check_date(v)

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def validate_body(cls, value):
        if isinstance(value, dict):
            allowed = {"status", "punch_in", "punch_out", "reason", "regularized_by"}
            for key in ("status", "punch_in", "punch_out"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} must be omitted or contain a value")
            # Derived fields are explicitly ignored; arbitrary input remains ignored by the API contract.
            return {k: v for k, v in value.items() if k in allowed}
        return value


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


def employee_out(e: dict) -> dict:
    e = dict(e)
    e.pop("_id", None)
    if e.get("created_at") is not None:
        e["created_at"] = to_ms(e["created_at"])
    return e


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(e) for e in cursor], "total": total,
            "page": page, "page_size": page_size}


def record_out(d: dict) -> dict:
    d = dict(d)
    d.pop("_id", None)
    d["punch_in"] = to_ms(d["punch_in"]) if d.get("punch_in") else None
    d["punch_out"] = to_ms(d["punch_out"]) if d.get("punch_out") else None
    d["work_hours"] = d.get("work_hours")
    d["late_minutes"] = d.get("late_minutes", 0)
    d["overtime_minutes"] = d.get("overtime_minutes", 0)
    d["half_day"] = d.get("half_day", False)
    history = []
    for h in d.get("history") or []:
        h = dict(h)
        h["at"] = to_ms(h["at"])
        changes = {}
        for field, ch in h.get("changes", {}).items():
            ch = dict(ch)
            if field in ("punch_in", "punch_out"):
                for k in ("from", "to"):
                    if isinstance(ch.get(k), datetime):
                        ch[k] = to_ms(ch[k])
            changes[field] = ch
        h["changes"] = changes
        history.append(h)
    d["history"] = history
    return d


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    date_str, shift_start_dt = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for value in (date_from, date_to):
        if value is not None:
            try: check_date(value)
            except ValueError: raise HTTPException(422, "dates must be valid YYYY-MM-DD values")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    page_docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in page_docs], "total": total,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = to_dt(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec: raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    elapsed = (ts - rec["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600: raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    work = compute_work_hours(rec["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"])
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work, "overtime_minutes": overtime, "half_day": work < 4.50}},
        return_document=True,
    )
    if result is None: raise HTTPException(409, "record already punched out")
    return record_out(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try: check_date(date)
    except ValueError: raise HTTPException(422, "date must be YYYY-MM-DD")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec: raise HTTPException(404, "attendance record not found")
    vals = {k: rec.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    if body.status is not None: vals["status"] = body.status
    if body.punch_in is not None: vals["punch_in"] = to_dt(body.punch_in)
    if body.punch_out is not None: vals["punch_out"] = to_dt(body.punch_out)
    if vals["status"] in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        vals.update(punch_in=None, punch_out=None, late_minutes=0, work_hours=None, overtime_minutes=0, half_day=False)
    else:
        if vals["punch_in"] is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(vals["punch_in"], emp["shift_start"], emp["shift_end"])[0] != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        _, shift_start_dt = attendance_date(vals["punch_in"], emp["shift_start"], emp["shift_end"])
        vals["late_minutes"] = compute_late_minutes(vals["punch_in"], shift_start_dt)
        if vals["punch_out"] is None:
            vals.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            delta = (vals["punch_out"] - vals["punch_in"]).total_seconds()
            if delta <= 0 or delta > 86400: raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            vals["work_hours"] = compute_work_hours(vals["punch_in"], vals["punch_out"])
            vals["overtime_minutes"] = compute_overtime(vals["punch_out"], emp["shift_start"], emp["shift_end"], date)
            vals["half_day"] = vals["work_hours"] < 4.50
    defaults = {"status": None, "punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    old = {k: rec.get(k, defaults[k]) for k in vals}
    changes = {}
    for field, new in vals.items():
        before = old[field]
        if isinstance(before, datetime) and isinstance(new, datetime):
            equal = before.replace(microsecond=0) == new.replace(microsecond=0)
        else: equal = before == new
        if not equal: changes[field] = {"from": before, "to": new}
    if not changes: raise HTTPException(422, "request does not change the record")
    at = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    update = {**vals, "history": {"$each": [history_entry]}}
    # Compare-and-swap on the observed history avoids losing concurrent appends.
    query = {"_id": rec["_id"], "$or": [{"history": rec.get("history", [])}, {"history": {"$exists": False}}]}
    updated = db.attendance_logs.find_one_and_update(query, {"$set": {k: v for k, v in vals.items()}, "$push": {"history": history_entry}}, return_document=True)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return record_out(updated)


def month_bounds(month: str):
    try:
        start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError: raise HTTPException(422, "month must be YYYY-MM")
    if month != start.strftime("%Y-%m"): raise HTTPException(422, "month must be YYYY-MM")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start, end


def working_days(start, end):
    return sum(1 for i in range((end-start).days + 1) if (start+timedelta(days=i)).weekday() < 5)


PRESENT = ["PRESENT", "WFH", "ON_DUTY"]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end, first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    eligible = max(first, datetime.strptime(emp["joined_on"], "%Y-%m-%d").date())
    days = working_days(eligible, last) if eligible <= last else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", PRESENT]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]
    row = next(db.attendance_logs.aggregate(pipeline), {})
    pres = 0
    # R7 excludes weekend present records; calculate this in the aggregation on stored status/half_day.
    p2 = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date"}}}}, {"$match": {"$expr": {"$and": [{"$gte": ["$date", max(start, emp['joined_on'])]}, {"$lt": [{"$isoDayOfWeek": "$_d"}, 6]}]}}}, {"$group": {"_id": None, "n": {"$sum": {"$cond": [{"$in": ["$status", PRESENT]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}}}]
    pres_row = next(db.attendance_logs.aggregate(p2), {})
    pres = pres_row.get("n", 0)
    rate = None if days == 0 else float((Decimal(str(pres)) * Decimal(100) / Decimal(days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": float(pres), "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": rate}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start, end, first, last = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}, {"$gte": ["$date", "$$joined"]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", PRESENT]}, {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENT]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$addFields": {"headcount": {"$size": "$headcount_codes"}}},
        {"$project": {"department": "$_id", "_id": 0, "headcount": 1, "present_days": 1, "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = float(Decimal(str(item["avg_work_hours"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end, _, _ = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}, {"$match": {"total_late_minutes": {"$gt": 0}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}}, {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    try: first, last = datetime.strptime(from_date, "%Y-%m-%d").date(), datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError: raise HTTPException(422, "from and to must be YYYY-MM-DD")
    if last < first or (last-first).days > 91: raise HTTPException(422, "range must be ordered and at most 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    # Seed each day from employees, then join that day's logs. This naturally retains days with no records.
    pipeline = [{"$match": {"department": department}}, {"$project": {"emp_code": 1, "joined_on": 1, "days": {"$map": {"input": {"$range": [0, (last-first).days+1]}, "as": "n", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$$n"}}, "format": "%Y-%m-%d"}}}}}}, {"$unwind": "$days"}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "day": "$days"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}}], "as": "log"}}, {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}}, {"$group": {"_id": "$days", "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$days"]}, 1, 0]}}, "present_count": {"$sum": {"$cond": [{"$and": [{"$lte": ["$joined_on", "$days"]}, {"$in": ["$log.status", PRESENT]}]}, {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$and": [{"$lte": ["$joined_on", "$days"]}, {"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}]}, 1, 0]}}}}, {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$_id"}}}}, {"$addFields": {"is_working_day": {"$lt": [{"$isoDayOfWeek": "$dateObj"}, 6]}, "attendance_rate": {"$cond": [{"$and": [{"$lt": [{"$isoDayOfWeek": "$dateObj"}, 6]}, {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}}, {"$setWindowFields": {"sortBy": {"dateObj": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}}, {"$project": {"_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}}}, {"$sort": {"date": 1}}]
    return {"department": department, "items": list(db.employees.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"], emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None, from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    # Explain the exact indexed operation used by each endpoint, with the same relevant filters.
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if status: q["status"] = status
        bounds = {}
        try:
            if date_from is not None: bounds["$gte"] = check_date(date_from)
            if date_to is not None: bounds["$lte"] = check_date(date_to)
        except ValueError:
            raise HTTPException(422, "dates must be valid YYYY-MM-DD values")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        if bounds: q["date"] = bounds
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}
        coll = "attendance_logs"
    else:
        if not month and endpoint != "department_trend": raise HTTPException(422, "month is required")
        if endpoint == "employee_monthly":
            if not emp_code: raise HTTPException(422, "emp_code is required")
            start, end, _, _ = month_bounds(month)
            pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}]
            coll = "attendance_logs"
        elif endpoint == "department_summary":
            start, end, _, _ = month_bounds(month); q = {"joined_on": {"$lte": end}}
            if department: q["department"] = department
            pipe = [{"$match": q}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}}], "as": "logs"}}]; coll = "employees"
        elif endpoint == "late_leaderboard":
            start, end, _, _ = month_bounds(month)
            pipe = [{"$match": {"date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
            if department is not None: pipe.append({"$match": {"employee.department": department}})
            coll = "attendance_logs"
        else:
            if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
            try: check_date(from_date); check_date(to_date)
            except ValueError: raise HTTPException(422, "invalid date")
            try:
                range_start = datetime.strptime(from_date, "%Y-%m-%d").date()
                range_end = datetime.strptime(to_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(422, "invalid date")
            if range_end < range_start or (range_end - range_start).days > 91:
                raise HTTPException(422, "range must be ordered and at most 92 days")
            pipe = [{"$match": {"department": department, "joined_on": {"$lte": to_date}}}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", from_date]}, {"$lte": ["$date", to_date]}]}}}], "as": "logs"}}]; coll = "employees"
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    try:
        exp = db.command("explain", cmd, verbosity="executionStats")
    except Exception as exc:
        raise HTTPException(422, f"could not explain query: {exc}")
    return {"endpoint": endpoint, "collection": coll, "explain": exp}
# --------------------------------------------------------------------------- #
