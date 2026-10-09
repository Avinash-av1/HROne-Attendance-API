"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", -1)])

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup():
    create_indexes()

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
from decimal import Decimal, ROUND_HALF_UP


def normalize_instant(value: datetime) -> datetime:
    """Normalize an instant to IST and truncate fractional seconds."""
    if value.tzinfo is None:
        # PyMongo may return naive datetimes representing UTC.
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(IST).replace(microsecond=0)


def shift_datetime(date_str: str, shift_time: str) -> datetime:
    """Build an IST datetime from an attendance date and HH:MM shift time."""
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    hour, minute = map(int, shift_time.split(":"))
    return datetime(
        day.year, day.month, day.day,
        hour, minute, tzinfo=IST
    )



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    """Calculate late minutes from the attendance shift start."""
    local_punch = normalize_instant(punch_in)
    start = shift_datetime(attendance_date, shift_start)

    elapsed_seconds = int((local_punch - start).total_seconds())

    if elapsed_seconds <= 10 * 60:
        return 0

    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate elapsed hours and round to 2 decimals using half-up."""
    start = normalize_instant(punch_in)
    end = normalize_instant(punch_out)

    seconds = int((end - start).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)

    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))



def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    """Count overtime only when at least 30 minutes after shift end."""
    local_out = normalize_instant(punch_out)
    end = shift_datetime(date_str, shift_end)

    # If the shift crosses midnight, its end is on the next day.
    if shift_end <= shift_start:
        end += timedelta(days=1)

    elapsed_seconds = int((local_out - end).total_seconds())

    # Overtime is counted only when the employee works at least 30 extra minutes.
    if elapsed_seconds < 30 * 60:
        return 0

    return elapsed_seconds // 60




# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    # Validate shift times in HH:MM format.
    try:
        start = datetime.strptime(body.shift_start, "%H:%M")
        end = datetime.strptime(body.shift_end, "%H:%M")
        if start.strftime("%H:%M") != body.shift_start:
            raise ValueError
        if end.strftime("%H:%M") != body.shift_end:
            raise ValueError
        datetime.strptime(body.joined_on, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid shift time or joined_on date format",
        )

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    return doc




@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(400, "page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(400, "page_size must be between 1 and 100")

    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(status_code=400, detail="Invalid attendance status")

    if body.punched_at is not None:
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            )
        except (ValueError, OSError, OverflowError):
            raise HTTPException(status_code=400, detail="Invalid punched_at")
    else:
        ts = datetime.now(timezone.utc)

    ts = normalize_instant(ts)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # Assign early-morning punches to the previous date for overnight shifts.
    attendance_date = ts.date()
    punch_time = ts.strftime("%H:%M")

    if shift_end <= shift_start and punch_time < shift_end:
        attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts.astimezone(timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, shift_start, date_str
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)

    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if body.punched_at is not None:
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            )
        except (ValueError, OSError, OverflowError):
            raise HTTPException(status_code=400, detail="Invalid punched_at")
    else:
        ts = datetime.now(timezone.utc)

    ts = normalize_instant(ts)
    local_ts = ts.astimezone(IST)
    date_str = local_ts.date().isoformat()

    # For an early-morning punch-out, find the previous day's overnight shift.
    log = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": date_str,
    })

    if not log:
        previous_date = (local_ts.date() - timedelta(days=1)).isoformat()
        previous_log = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "date": previous_date,
        })

        if previous_log:
            emp = db.employees.find_one({"emp_code": body.emp_code})
            if emp and emp["shift_end"] <= emp["shift_start"]:
                log = previous_log
                date_str = previous_date

    if not log:
        raise HTTPException(status_code=404, detail="Punch-in record not found")

    if log.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    if ts <= normalize_instant(log["punch_in"]):
        raise HTTPException(
            status_code=400,
            detail="Punch-out must be after punch-in",
        )

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    work_hours = compute_work_hours(log["punch_in"], ts)
    overtime = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        date_str,
    )

    update = {
        "punch_out": ts.astimezone(timezone.utc),
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": work_hours < 4.50,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": log["_id"],
            "punch_out": None,
        },
        {"$set": update},
    )

    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Punch-out already processed")

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    updated["id"] = str(updated.pop("_id"))
    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
