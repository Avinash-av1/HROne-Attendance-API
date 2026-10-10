"""Employee Attendance & Analytics API for the HROne engineering assignment.

Run from the repository root:
    uvicorn app.main:app --port 8000

Configuration is read from MONGO_URI and MONGO_DB. The .env file is only a
local convenience; real environment variables take precedence.
"""
from __future__ import annotations

import os
import re
import calendar
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=10_000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes() -> None:
    """Create idempotent indexes used by both API queries and aggregations."""
    # Keep default names for the two indexes created by earlier starter versions,
    # so re-running startup does not conflict with existing Atlas indexes.
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING)])
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])


@app.on_event("startup")
def startup() -> None:
    create_indexes()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1, max_length=20)
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1, max_length=20)
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    # Contract only permits the named editable fields. Derived fields are ignored
    # by the model because the contract says clients cannot set them.
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------
def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}; use YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(status_code=422, detail="Invalid month; use YYYY-MM")
    year, month = map(int, value.split("-"))
    try:
        start = date(year, month, 1)
        if month == 12:
            next_start = date(year + 1, 1, 1)
        else:
            next_start = date(year, month + 1, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month; use YYYY-MM")
    return start, next_start


def parse_shift(value: str, field_name: str) -> time:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}; use HH:MM")
    return datetime.strptime(value, "%H:%M").time()


def validate_epoch_ms(value: Any, field_name: str = "punched_at") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid epoch-millisecond integer")
    return value


def from_epoch_ms(value: int, field_name: str = "timestamp") -> datetime:
    value = validate_epoch_ms(value, field_name)
    try:
        instant = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}")
    return normalize_instant(instant)


def normalize_instant(value: datetime) -> datetime:
    """Return an IST-aware whole-second datetime."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # PyMongo's default naive datetimes are UTC.
    return value.astimezone(IST).replace(microsecond=0)


def utc_datetime(value: datetime) -> datetime:
    return normalize_instant(value).astimezone(UTC).replace(microsecond=0)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    instant = normalize_instant(value).astimezone(UTC)
    return int(instant.timestamp()) * 1000


def round_half_up(value: Optional[float | int | Decimal], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quant = Decimal("1") if places == 0 else Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def shift_datetime(date_str: str, shift_time: str) -> datetime:
    day = parse_date(date_str)
    st = parse_shift(shift_time, "shift time")
    return datetime.combine(day, st, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    local_punch = normalize_instant(punch_in)
    start = shift_datetime(attendance_date, shift_start)
    elapsed_seconds = int((local_punch - start).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    start = normalize_instant(punch_in)
    end = normalize_instant(punch_out)
    seconds = int((end - start).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    local_out = normalize_instant(punch_out)
    end = shift_datetime(attendance_date, shift_end)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    elapsed_seconds = int((local_out - end).total_seconds())
    if elapsed_seconds < 30 * 60:
        return 0
    return elapsed_seconds // 60


def attendance_date_for_punch(local_ts: datetime, shift_start: str, shift_end: str) -> date:
    day = local_ts.date()
    if shift_end <= shift_start and local_ts.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def present_day_value(doc: dict) -> float:
    try:
        weekday = parse_date(doc["date"]).weekday() < 5
    except Exception:
        weekday = False
    if doc.get("status") not in PRESENCE_STATUSES or not weekday:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


def serialise_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = epoch_ms(result["created_at"])
    return result


def serialise_history_entry(entry: dict) -> dict:
    result = dict(entry)
    if isinstance(result.get("at"), datetime):
        result["at"] = epoch_ms(result["at"])
    changes = {}
    for field, values in (result.get("changes") or {}).items():
        pair = dict(values)
        if field in {"punch_in", "punch_out"}:
            for side in ("from", "to"):
                val = pair.get(side)
                if isinstance(val, datetime):
                    pair[side] = epoch_ms(val)
        changes[field] = pair
    result["changes"] = changes
    return result


def serialise_attendance(doc: dict) -> dict:
    """Return only contract fields; never expose MongoDB _id or an invented record id."""
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": epoch_ms(doc.get("punch_in")) if isinstance(doc.get("punch_in"), datetime) else None,
        "punch_out": epoch_ms(doc.get("punch_out")) if isinstance(doc.get("punch_out"), datetime) else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialise_history_entry(h) for h in (doc.get("history") or [])],
    }


def month_date_filter(month: str) -> tuple[str, str, date, date]:
    start, next_start = parse_month(month)
    return start.isoformat(), next_start.isoformat(), start, next_start


def working_days(start: date, end_exclusive: date) -> int:
    count = 0
    cur = start
    while cur < end_exclusive:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def validate_page(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")


def aggregation_explain(collection_name: str, pipeline: list[dict]) -> dict:
    return db.command(
        "explain",
        {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def date_expr_from_string(field: str = "$date") -> dict:
    return {"$dateFromString": {"dateString": field, "format": "%Y-%m-%d"}}


# ---------------------------------------------------------------------------
# Health and employees
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not re.fullmatch(r"EMP\d{4,6}", body.emp_code):
        raise HTTPException(status_code=422, detail="emp_code must be EMP followed by 4-6 digits")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", body.email):
        raise HTTPException(status_code=422, detail="Invalid email")
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    parse_shift(body.shift_start, "shift_start")
    parse_shift(body.shift_end, "shift_end")
    parse_date(body.joined_on, "joined_on")
    created = datetime.now(UTC).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialise_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialise_employee(d) for d in items], "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance: punch-in, punch-out, list and regularization
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(status_code=422, detail="punched_at must be epoch milliseconds when supplied")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid attendance status")
    ts = from_epoch_ms(body.punched_at, "punched_at") if body.punched_at is not None else normalize_instant(datetime.now(UTC))
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]
    attendance_day = attendance_date_for_punch(ts, shift_start, shift_end)
    date_str = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": utc_datetime(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start, date_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    return serialise_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(status_code=422, detail="punched_at must be epoch milliseconds when supplied")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = from_epoch_ms(body.punched_at, "punched_at") if body.punched_at is not None else normalize_instant(datetime.now(UTC))
    ts_utc = utc_datetime(ts)
    # Match the newest punch-in that is not in the future relative to this event.
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if not log:
        raise HTTPException(status_code=404, detail="Punch-in record not found")
    if log.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    punch_in_ts = normalize_instant(log["punch_in"])
    elapsed = (ts - punch_in_ts).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(punch_in_ts, ts)
    update = {
        "punch_out": ts_utc,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], log["date"]),
        "half_day": work_hours < 4.50,
    }
    result = db.attendance_logs.update_one({"_id": log["_id"], "punch_out": None}, {"$set": update})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Already punched out")
    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return serialise_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialise_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    requested = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    if not requested:
        raise HTTPException(status_code=422, detail="No changes supplied")
    if "status" in requested and requested["status"] not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")
    for field in ("punch_in", "punch_out"):
        if field in requested and requested[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} must be epoch milliseconds when supplied")
        if field in requested:
            validate_epoch_ms(requested[field], field)

    final_status = requested.get("status", log["status"])
    if final_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in requested or "punch_out" in requested:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot include punch timestamps")
        final_in = final_out = None
    else:
        final_in = from_epoch_ms(requested["punch_in"], "punch_in") if "punch_in" in requested else (normalize_instant(log["punch_in"]) if log.get("punch_in") else None)
        final_out = from_epoch_ms(requested["punch_out"], "punch_out") if "punch_out" in requested else (normalize_instant(log["punch_out"]) if log.get("punch_out") else None)
        if final_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        if final_in.date().isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the attendance date in IST")
        if final_out is not None:
            seconds = (final_out - final_in).total_seconds()
            if seconds <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if seconds > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    proposed = {
        "status": final_status,
        "punch_in": utc_datetime(final_in) if final_in else None,
        "punch_out": utc_datetime(final_out) if final_out else None,
    }
    if final_in is None:
        proposed.update({"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False})
    else:
        proposed["late_minutes"] = compute_late_minutes(final_in, emp["shift_start"], date)
        if final_out is None:
            proposed.update({"work_hours": None, "overtime_minutes": 0, "half_day": False})
        else:
            wh = compute_work_hours(final_in, final_out)
            proposed.update({
                "work_hours": wh,
                "overtime_minutes": compute_overtime(final_out, emp["shift_start"], emp["shift_end"], date),
                "half_day": wh < 4.50,
            })

    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes: dict[str, dict[str, Any]] = {}
    for field in tracked:
        if field in {"punch_in", "punch_out"}:
            old = normalize_instant(log[field]).astimezone(UTC) if isinstance(log.get(field), datetime) else None
            new = utc_datetime(proposed[field]) if isinstance(proposed.get(field), datetime) else None
        else:
            defaults = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
            old = log.get(field, defaults.get(field))
            new = proposed.get(field, defaults.get(field))
        if old != new:
            changes[field] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(status_code=422, detail="Request makes no changes")

    history_entry = {
        "at": utc_datetime(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency control while still matching legacy records without history.
    update_filter: dict[str, Any] = {
        "_id": log["_id"],
        "status": log["status"],
        "punch_in": log.get("punch_in"),
        "punch_out": log.get("punch_out"),
    }
    if "history" in log:
        update_filter["history"] = log["history"]
    else:
        update_filter["history"] = {"$exists": False}
    result = db.attendance_logs.update_one(update_filter, {"$set": proposed, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry with the latest record")
    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return serialise_attendance(updated)


# ---------------------------------------------------------------------------
# Shared aggregation snippets
# ---------------------------------------------------------------------------
def month_match(month: str) -> tuple[str, str, date, date, dict]:
    start_str, next_str, start_date, next_date = month_date_filter(month)
    return start_str, next_str, start_date, next_date, {"date": {"$gte": start_str, "$lt": next_str}}


def log_stats_pipeline(match: dict, emp_code: Optional[str] = None) -> list[dict]:
    query = dict(match)
    if emp_code is not None:
        query["emp_code"] = emp_code
    return [
        {"$match": query},
        {"$addFields": {"_cal_date": date_expr_from_string()}},
        {"$addFields": {"_weekday": {"$dayOfWeek": "$_cal_date"}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


# ---------------------------------------------------------------------------
# Analytics: employee monthly summary
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_str, next_str, start, next_start, base_match = month_match(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    rows = list(db.attendance_logs.aggregate(log_stats_pipeline(base_match, emp_code)))
    stats = rows[0] if rows else {}
    effective_start = max(start, parse_date(emp["joined_on"], "joined_on"))
    days = working_days(effective_start, next_start) if effective_start < next_start else 0
    present = round_half_up(stats.get("present_days", 0.0), 2) or 0.0
    pct = round_half_up((present / days * 100), 4) if days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---------------------------------------------------------------------------
# Analytics: cross-collection department summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_str, next_str, start, next_start, _ = month_match(month)
    month_end = (next_start - timedelta(days=1)).isoformat()
    emp_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start_str]},
                    {"$lt": ["$date", next_str]},
                ]}}},
                {"$addFields": {"_cal_date": date_expr_from_string()}},
                {"$addFields": {"_weekday": {"$dayOfWeek": "$_cal_date"}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$gte": ["$logs._weekday", 2]}, {"$lte": ["$logs._weekday", 6]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]}, 0.0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]}, "$logs.work_hours", None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
            "present_days": 1, "avg_work_hours": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(row.get("present_days", 0), 2),
            "avg_work_hours": round_half_up(row.get("avg_work_hours"), 2),
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics: competition-ranked late leaderboard
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_str, next_str, _, _, match = month_match(month)
    pipeline: list[dict] = [
        {"$match": match},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ])
    rows = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": [{
        "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
    } for r in rows]}


# ---------------------------------------------------------------------------
# Analytics: daily trend, densified inside MongoDB
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    start = parse_date(from_date, "from")
    end = parse_date(to, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_utc = datetime.combine(start, time.min, tzinfo=UTC)
    end_exclusive_utc = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    pipeline = [
        {"$documents": [{"day": start_utc, "_synthetic": True, "status": "", "late_minutes": 0, "half_day": False}]},
        {"$unionWith": {
            "coll": "attendance_logs",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$set": {"day": date_expr_from_string(), "_synthetic": False}},
                {"$project": {"day": 1, "_synthetic": 1, "status": 1, "late_minutes": 1, "half_day": 1}},
            ],
        }},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": {"$cond": [
                {"$and": [{"$eq": [{"$ifNull": ["$_synthetic", False]}, False]}, {"$in": ["$status", list(PRESENCE_STATUSES)]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0.0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$and": [{"$eq": [{"$ifNull": ["$_synthetic", False]}, False]}, {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}]}, 1, 0,
            ]}},
        }},
        {"$set": {"day": "$_id"}},
        {"$unset": "_id"},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_utc, end_exclusive_utc]}}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "day_string": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "weekday": {"$dayOfWeek": "$day"},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day_string": "$day_string"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day_string"]},
            ]}}}],
            "as": "joined_employees",
        }},
        {"$set": {
            "headcount": {"$size": "$joined_employees"},
            "is_working_day": {"$in": ["$weekday", [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]}, None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, "current"]},
            }},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": "$day_string", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]
    rows = list(db.aggregate(pipeline))
    items = []
    for row in rows:
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": round_half_up(row.get("present_count", 0), 2),
            "late_count": int(row.get("late_count", 0)),
            "attendance_rate": round_half_up(row.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(row.get("moving_avg_7d"), 4),
        })
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# MongoDB explain plans for the contract's named endpoint queries
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail=f"endpoint must be one of {sorted(allowed)}")
    explain: dict
    collection_name = "attendance_logs"
    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status is not None:
            if status not in ALL_STATUSES:
                raise HTTPException(status_code=422, detail="Invalid status")
            q["status"] = status
        explain = db.command("explain", {
            "find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size,
        }, verbosity="executionStats")
    else:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for this endpoint")
        start_str, next_str, start, next_start, match = month_match(month)
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
            collection_name = "attendance_logs"
            pipeline = log_stats_pipeline(match, emp_code)
            explain = aggregation_explain(collection_name, pipeline)
        elif endpoint == "department_summary":
            collection_name = "employees"
            month_end = (next_start - timedelta(days=1)).isoformat()
            emp_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
            if department is not None:
                emp_match["department"] = department
            pipeline = [{"$match": emp_match}, {"$lookup": {
                "from": "attendance_logs", "let": {"code": "$emp_code"},
                "pipeline": [{"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_str]}, {"$lt": ["$date", next_str]},
                ]}}}], "as": "logs",
            }}]
            explain = aggregation_explain(collection_name, pipeline)
        elif endpoint == "late_leaderboard":
            pipeline = [
                {"$match": match},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
            ]
            if department is not None:
                pipeline.append({"$match": {"employee.department": department}})
            pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}}]
            explain = aggregation_explain("attendance_logs", pipeline)
        else:
            if not department:
                raise HTTPException(status_code=422, detail="department is required for department_trend")
            if not from_date or not to:
                raise HTTPException(status_code=422, detail="from and to are required for department_trend")
            # Match the trend endpoint's day-range scan and department join query shape.
            start_day, end_day = parse_date(from_date, "from"), parse_date(to, "to")
            if end_day < start_day or (end_day - start_day).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Invalid department trend date range")
            collection_name = "attendance_logs"
            pipeline = [
                {"$match": {"date": {"$gte": start_day.isoformat(), "$lte": end_day.isoformat()}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
            ]
            explain = aggregation_explain("attendance_logs", pipeline)
    # MongoDB explain output can contain BSON-only values (notably Timestamp) that
    # FastAPI cannot encode directly. Convert the complete document to Extended JSON
    # so its query-plan structure remains visible and the response is JSON serializable.
    serialisable_explain = json.loads(json_util.dumps(explain))
    return {"endpoint": endpoint, "collection": collection_name, "explain": serialisable_explain}
