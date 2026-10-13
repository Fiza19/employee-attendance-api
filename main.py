"""Employee Attendance & Analytics API, contract v2.0."""
import os, re, math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
attendance = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENT | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def init_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_open_punch")


@app.on_event("startup")
def startup():
    init_indexes()


def parse_date(s: str) -> date:
    try: return date.fromisoformat(s)
    except (ValueError, TypeError): raise HTTPException(422, "date must be YYYY-MM-DD")


def parse_month(s: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, s.split("-")); first = date(y, m, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return first, nxt


def millis_to_dt(ms: int | None) -> datetime | None:
    if ms is None: return None
    if type(ms) is not int or not 100_000_000_000 <= ms <= 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds as an integer")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)


def dt_to_ms(value: datetime | None) -> int | None:
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None: return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def half_up(value: float | int, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def shift_instant(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":")); return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def shift_bounds(emp: dict, punch_in: datetime, record_day: date | None = None) -> tuple[datetime, datetime]:
    local = as_utc(punch_in).astimezone(IST)
    start_day = record_day or local.date()
    sh, sm = map(int, emp["shift_start"].split(":")); eh, em = map(int, emp["shift_end"].split(":"))
    # Infer the shift start date only when caller has not already supplied the attendance date.
    if record_day is None and emp["shift_end"] <= emp["shift_start"] and (local.hour * 60 + local.minute < eh * 60 + em):
        start_day -= timedelta(days=1)
    start = shift_instant(start_day, emp["shift_start"])
    end_day = start_day + (timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else timedelta())
    end = shift_instant(end_day, emp["shift_end"])
    return start, end


def compute_late_minutes(punch_in: datetime | None, emp: dict, day: date | None = None) -> int:
    if punch_in is None: return 0
    start, _ = shift_bounds(emp, punch_in, day)
    delta = (as_utc(punch_in) - start).total_seconds()
    return int(delta // 60) if delta > 600 else 0


def compute_work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None: return None
    return half_up((as_utc(punch_out) - as_utc(punch_in)).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime | None, emp: dict, punch_in: datetime | None, day: date | None = None) -> int:
    if punch_out is None or punch_in is None: return 0
    _, end = shift_bounds(emp, punch_in, day)
    mins = int((as_utc(punch_out) - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def working_days(start: date, end_exclusive: date) -> int:
    total = 0
    while start < end_exclusive:
        if start.weekday() < 5: total += 1
        start += timedelta(days=1)
    return total


def serialize_attendance(doc: dict) -> dict:
    d = dict(doc); d.pop("_id", None)
    for k in ("punch_in", "punch_out"):
        d[k] = dt_to_ms(as_utc(d.get(k)))
    d["history"] = d.get("history", []) or []
    for h in d["history"]:
        h["at"] = dt_to_ms(as_utc(h.get("at")))
        for v in h.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(v.get(side), datetime): v[side] = dt_to_ms(as_utc(v[side]))
    d.setdefault("late_minutes", 0); d.setdefault("overtime_minutes", 0); d.setdefault("half_day", False)
    return d


def serialize_employee(doc: dict) -> dict:
    d = dict(doc); d.pop("_id", None)
    if isinstance(d.get("created_at"), datetime): d["created_at"] = dt_to_ms(as_utc(d["created_at"]))
    return d


def valid_page(page: int, page_size: int):
    if page < 1 or not 1 <= page_size <= 100: raise HTTPException(422, "page must be >= 1 and page_size must be 1..100")


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def date_valid(cls, v): date.fromisoformat(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None: millis_to_dt(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None: millis_to_dt(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamps(cls, v):
        if v is not None: millis_to_dt(v)
        return v


@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status": "ok"}
    except PyMongoError: raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = datetime.now(UTC)
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    valid_page(page, page_size); q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = [serialize_employee(x) for x in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = millis_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC)
    ts = ts.replace(microsecond=0); local_day = ts.astimezone(IST).date()
    if emp["shift_end"] <= emp["shift_start"]:
        eh, em = map(int, emp["shift_end"].split(":"))
        if local_day == ts.astimezone(IST).date() and (ts.astimezone(IST).hour*60+ts.astimezone(IST).minute) < eh*60+em:
            local_day -= timedelta(days=1)
    d = local_day.isoformat()
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp, local_day), "overtime_minutes": 0, "half_day": False, "history": []}
    try: attendance.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    valid_page(page, page_size)
    if status and status not in STATUSES: raise HTTPException(422, "invalid status")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if status: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            parse_date(date_from); q["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to); q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
    total = attendance.count_documents(q)
    docs = attendance.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [serialize_attendance(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    out = millis_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC); out = out.replace(microsecond=0)
    # Find latest punch-in not after the requested instant; then atomically close it.
    doc = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out}, "punch_out": None}, sort=[("punch_in", DESCENDING)])
    if not doc:
        previous = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out}}, sort=[("punch_in", DESCENDING)])
        if previous and previous.get("punch_out") is not None:
            raise HTTPException(409, "record already punched out")
        raise HTTPException(404, "no open punch-in found")
    pin = as_utc(doc["punch_in"])
    if out <= pin or out - pin > timedelta(hours=24): raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(pin, out); overtime = compute_overtime(out, emp, pin, parse_date(doc["date"]))
    result = attendance.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": {"punch_out": out, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "record already punched out")
    return serialize_attendance(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date); emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, "attendance record not found")
    new = {k: old.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    new["status"] = body.status if body.status is not None else old["status"]
    for k in ("punch_in", "punch_out"):
        if k in body.model_fields_set:
            new[k] = millis_to_dt(getattr(body, k))
    if new["status"] in {"ABSENT", "LEAVE"}:
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new["punch_in"] = new["punch_out"] = None
    elif new["punch_in"] is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new["punch_in"] is not None:
        local_pin = as_utc(new["punch_in"]).astimezone(IST)
        valid_dates = {day}
        if emp["shift_end"] <= emp["shift_start"]:
            end_h, end_m = map(int, emp["shift_end"].split(":"))
            if local_pin.hour * 60 + local_pin.minute < end_h * 60 + end_m:
                valid_dates.add(day + timedelta(days=1))
        if local_pin.date() not in valid_dates: raise HTTPException(422, "punch_in must belong to the attendance shift date")
    if new["punch_out"] is not None:
        if new["punch_in"] is None or as_utc(new["punch_out"]) <= as_utc(new["punch_in"]) or as_utc(new["punch_out"])-as_utc(new["punch_in"]) > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if new["status"] in {"ABSENT", "LEAVE"}:
        new.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        new["late_minutes"] = compute_late_minutes(new["punch_in"], emp, day)
        new["work_hours"] = compute_work_hours(new["punch_in"], new["punch_out"])
        new["overtime_minutes"] = compute_overtime(new["punch_out"], emp, new["punch_in"], day)
        new["half_day"] = bool(new["work_hours"] is not None and new["work_hours"] < 4.5)
    changes = {}
    for k, v in new.items():
        ov = old.get(k, False if k == "half_day" else 0 if k in ("late_minutes", "overtime_minutes") else None)
        if k in ("punch_in", "punch_out"):
            ov, v = as_utc(ov), as_utc(v)
        if ov != v: changes[k] = {"from": ov, "to": v}
    if not changes: raise HTTPException(422, "request changes nothing")
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Conditional compare-and-swap ensures simultaneous corrections do not overwrite each other.
    query = {"_id": old["_id"]}
    if "history" in old:
        query["history"] = old["history"]
    else:
        query["history"] = {"$exists": False}
    sets = {k: v for k, v in new.items()}
    result = attendance.find_one_and_update(query, {"$set": sets, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "record changed concurrently; retry")
    return serialize_attendance(result)


def month_bounds(month: str):
    first, nxt = parse_month(month); return first.isoformat(), nxt.isoformat(), first, nxt


def base_month_pipeline(emp_code: str, month: str):
    first, nxt, _, _ = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lt": nxt}}},
            {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
             "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
             "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "employee not found")
    first_s, nxt_s, first, nxt = month_bounds(month)
    working_start = max(first, parse_date(emp["joined_on"]))
    wd = working_days(working_start, nxt) if working_start < nxt else 0
    rows = list(attendance.aggregate(base_month_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present = half_up(r.get("present_days", 0), 2)
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": int(r.get("leave_days", 0)), "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)), "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
            "attendance_pct": half_up(present / wd * 100, 4) if wd else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first_s, nxt_s, first, nxt = month_bounds(month)
    eq = {"$eq": ["$department", department]} if department is not None else None
    pipeline = [{"$match": {"joined_on": {"$lt": nxt_s}, **({"department": department} if department is not None else {})}},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lt": ["$date", nxt_s]}]}}}], "as": "logs"}},
      {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department", "codes": {"$addToSet": "$emp_code"},
       "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
       "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
       "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
       "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
       "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
       "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$codes"}, "present_days": 1,
       "avg_work_hours": {"$let": {"vars": {"h": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$h"}, 0]}, {"$avg": "$$h"}, None]}}},
       "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
    rows = list(employees.aggregate(pipeline))
    out = []
    for r in rows:
        if r["headcount"] == 0: continue
        r["present_days"] = half_up(r.get("present_days", 0), 2)
        if r.get("avg_work_hours") is not None: r["avg_work_hours"] = half_up(r["avg_work_hours"], 2)
        for k in ("late_count", "total_late_minutes", "leave_count", "on_duty_count"): r[k] = int(r.get(k, 0))
        out.append(r)
    return {"month": month, "items": out}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_s, nxt_s, _, _ = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first_s, "$lt": nxt_s}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}}]
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = parse_date(from_), parse_date(to)
    if end < start or (end-start).days + 1 > 92: raise HTTPException(422, "invalid date range (maximum 92 days)")
    if not employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    # MongoDB densifies dates and computes the moving average in-server. Rates are rounded after aggregation.
    pipeline = [{"$match": {"date": {"$gte": from_, "$lte": to}}},
      {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"},
      {"$match": {"employee.department": department}},
      {"$addFields": {"_day": {"$dateFromString": {"dateString": "$date"}}, "_present": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}},
      {"$group": {"_id": "$_day", "present_count": {"$sum": "$_present"}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
      {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
      {"$unionWith": {"coll": "employees", "pipeline": [{"$match": {"department": department}}, {"$limit": 1}, {"$project": {"_id": 0, "date": {"$literal": datetime.combine(start, time.min)}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}}]}},
      {"$group": {"_id": "$date", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}}, {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
      {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min), datetime.combine(end, time.min) + timedelta(days=1)]}}},
      {"$addFields": {"date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
      {"$lookup": {"from": "employees", "let": {"d": "$date_str"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}}, {"$count": "n"}], "as": "hc"}},
      {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
      {"$addFields": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]}, {"$lte": [{"$dayOfWeek": "$date"}, 6]}]},
       "attendance_rate": {"$cond": [{"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]}, {"$lte": [{"$dayOfWeek": "$date"}, 6]}, {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 10000]}, 0.5]}}, 10000]}, None]}}},
      {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}, None]}}}, {"$sort": {"date": 1}}]
    return {"department": department, "items": list(attendance.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "unsupported endpoint")
    valid_page(page, page_size)
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from: parse_date(date_from); q.setdefault("date", {})["$gte"] = date_from
        if date_to: parse_date(date_to); q.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
        exp = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        first, nxt, _, _ = month_bounds(month); pipe = base_month_pipeline(emp_code, month)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats"); coll = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        # Explain the indexed employee range and joined attendance lookup used by the summary.
        first, nxt, _, _ = month_bounds(month); q = {"joined_on": {"$lt": nxt}}
        if department is not None: q["department"] = department
        exp = db.command("explain", {"find": "employees", "filter": q}, verbosity="executionStats"); coll = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        first, nxt, _, _ = month_bounds(month)
        pipe = [{"$match": {"date": {"$gte": first, "$lt": nxt}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats"); coll = "attendance_logs"
    else:
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        parse_date(from_); parse_date(to)
        q = {"department": department, "joined_on": {"$lte": to}}
        exp = db.command("explain", {"find": "employees", "filter": q}, verbosity="executionStats"); coll = "employees"
    return {"endpoint": endpoint, "collection": coll, "explain": exp}
