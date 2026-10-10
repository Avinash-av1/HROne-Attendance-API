# Employee Attendance & Analytics API

FastAPI and MongoDB API for recording employee attendance, correcting attendance with an audit trail, and reporting attendance analytics. Built for the HROne engineering assignment.

## Technology

- Python
- FastAPI and Uvicorn
- PyMongo
- MongoDB Atlas (or a reachable local MongoDB instance)
- Pydantic v2 and `python-dotenv`

## Features

- Employee creation and paginated employee listing.
- Punch-in and punch-out with duplicate/concurrency protection.
- Attendance listing with employee, date-range and status filters.
- Attendance regularization with derived-field recalculation and an append-only history entry.
- Monthly employee analytics and department summaries.
- Late-comer leaderboard using competition ranking (ties share a rank; subsequent ranks are skipped).
- Department daily trend with date gap-filling and a seven-day moving average.
- MongoDB execution-plan inspection through the admin explain endpoint.
- Startup index creation for the main lookup, filtering and sort patterns.

## Project layout

```text
HROne-Attendance-API/
├── app/
│   └── main.py              # FastAPI application and endpoint implementations
├── sample_data/
│   ├── employees.json       # Example employee documents
│   └── attendance_logs.json # Example attendance documents
├── sample_seed.py           # Loads sample data (destructive to the target collections)
├── requirements.txt
├── openapi.yaml             # Assignment API contract
├── DATA_MODEL.md            # MongoDB document model
├── PROBLEM_STATEMENT.docx   # Assignment requirements
├── README.md
├── DECISIONS.md
└── REVIEW.md
```

## Prerequisites

- Python 3.10 or later (use a version compatible with the installed dependencies).
- A MongoDB Atlas cluster or local MongoDB deployment reachable from your machine.
- Atlas network access configured to allow your current public IP, if using Atlas.

## Setup on Windows (Git Bash)

Run commands from the repository root, `HROne-Attendance-API`.

```bash
python -m venv .venv
source .venv/Scripts/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create a local `.env` file in the repository root. Use your own MongoDB connection details; do not copy credentials into the README or commit `.env`.

```dotenv
MONGO_URI=mongodb+srv://<username>:<password>@<your-cluster-host>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

If your Atlas password contains reserved URI characters, URL-encode them in the connection string. The cluster host, database user, password and network access settings must match the cluster you intend to use. Real environment variables take precedence over `.env` values.

Install dependencies if not already installed:

```bash
pip install -r requirements.txt
```

## Run the API

```bash
python -m py_compile app/main.py
python -m uvicorn app.main:app --reload --port 8000
```

Open the interactive Swagger UI at <http://127.0.0.1:8000/docs>. Readiness check: <http://127.0.0.1:8000/health>.

The server must remain running while testing from Swagger. Stop it with `Ctrl+C` in the server terminal.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Check that MongoDB responds to a ping |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List/filter employees with pagination |
| POST | `/attendance/punch-in` | Create a daily attendance record |
| POST | `/attendance/punch-out` | Close the latest eligible open attendance record |
| GET | `/attendance` | Filter and paginate attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record and append an audit entry |
| GET | `/analytics/employees/{emp_code}/monthly?month=YYYY-MM` | Monthly employee statistics |
| GET | `/analytics/departments/summary?month=YYYY-MM` | Department-level monthly statistics |
| GET | `/analytics/leaderboard/late?month=YYYY-MM` | Late-comers ranking |
| GET | `/analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD` | Daily department trend |
| GET | `/admin/explain/{endpoint}` | Inspect MongoDB query/aggregation explain output |

Pagination uses `page` starting at 1 and `page_size` from 1 to 100 (default 20). Supported attendance status values are `PRESENT`, `WFH`, `ON_DUTY`, `ABSENT`, and `LEAVE`; punch-in accepts only the three presence statuses.

## API and data conventions

- Employee identity is `emp_code`; MongoDB `_id` is internal and is not returned.
- Attendance records are identified by `(emp_code, date)`.
- `date` and `joined_on` use `YYYY-MM-DD`. Shift times use 24-hour `HH:MM` in IST.
- Request/response instants use integer epoch milliseconds. MongoDB stores instants as BSON UTC datetimes.
- Instants are truncated to whole seconds before they are calculated/stored/returned.
- Derived fields (`late_minutes`, `work_hours`, `overtime_minutes`, `half_day`) are calculated by the API. Analytics read stored derived values.
- A history entry is created only by a successful PATCH; punch-in/out do not modify history.
- Overnight shifts are those where `shift_end <= shift_start`; their attendance date belongs to the date the shift started.

See `openapi.yaml`, `DATA_MODEL.md`, and `PROBLEM_STATEMENT.docx` for the full assignment contract and rules R1–R10.

## Testing checklist

1. Start the server and confirm `GET /health` succeeds.
2. Test `GET /employees` and `GET /attendance`.
3. Test a new punch-in and the matching punch-out. Avoid reusing an employee/date that already has a punch-in unless you are specifically checking the expected conflict response.
4. Test PATCH on an existing record and confirm that exactly one history entry is appended.
5. Test each analytics endpoint for a month/date range that exists in the seeded data.
6. Test `GET /admin/explain/attendance_list` with a date range and confirm the response includes a winning plan using an index (`IXSCAN`).
7. Before submission, test validation errors, overnight boundaries, no-op PATCH, missing employees/records, ties in the leaderboard, and ranges up to the contract limit.

### Seed data warning

`sample_seed.py` deletes documents from both the `employees` and `attendance_logs` collections in the configured database before inserting sample rows. Run it only against a database that you are willing to reset. Do not run it casually against a database containing data you need to keep.

## Security notes

- Keep `.env` out of version control. Commit only a placeholder/example configuration if one is needed.
- Never publish MongoDB passwords, full connection strings, or Atlas secrets in screenshots or documentation.
- Restrict Atlas network access to trusted IP addresses rather than leaving the cluster broadly exposed.
