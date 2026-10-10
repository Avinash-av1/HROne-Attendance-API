# Code Review and Verification Notes

This review records the main areas addressed in the current implementation and the checks performed manually in Swagger during development. It is not a claim that every edge case or the grader's large dataset has been fully tested.

## Issues addressed in the implementation

- **Duplicate attendance records:** a unique compound index on `(emp_code, date)` enforces the natural key at the database level.
- **Patch conflict on legacy records:** some older attendance documents omit `history`. The PATCH update filter now matches either the existing history array or the fact that the field is absent.
- **Patch audit trail:** a successful correction appends one history entry and records before/after values only for changed tracked fields.
- **Timestamp response format:** API instants are serialized as epoch milliseconds; MongoDB stores BSON UTC datetimes. Missing punch timestamps serialize as `null`.
- **Timezone and precision:** instants are normalized to IST for business calculations and truncated to whole seconds; UTC datetimes are stored in MongoDB.
- **Late and overtime boundaries:** late time uses the strict 10-minute grace rule; overtime is recorded only at 30 or more minutes after shift end.
- **Attendance list scale:** filtered counts and MongoDB-side sorting, skipping and limits avoid loading the entire collection into Python.
- **Punch-out selection:** the implementation locates the latest punch-in not later than the punch-out instant and rejects punch-out after 24 hours.
- **Analytics requirements:** monthly working days and present-day rules, cross-collection department headcount, rank ties, trend gap-filling and the 7-day moving average are handled in aggregation pipelines.
- **Explain serialization:** BSON values in explain output are converted to Extended JSON after the original code produced a FastAPI serialization error for a `Timestamp` value.

## Manual verification observed during development

The developer tested the following against the configured MongoDB Atlas database through Swagger/terminal:

| Check | Observed result |
|---|---|
| Application syntax check with `python -m py_compile app/main.py` | Passed after the final replacement file was installed |
| `GET /health` | Returned a successful database health response before the final explain fix |
| `GET /employees` | Returned employee records and pagination metadata |
| `GET /attendance` | Returned attendance rows and pagination metadata |
| `PATCH /attendance/{emp_code}/{date}` | Returned success, changed `PRESENT` to `WFH`, and appended a history entry |
| Employee monthly analytics | Returned an employee/month summary; observed July example had 15 working days and 1 present day |
| Department summary | Executed successfully for July 2026 |
| Late leaderboard | Executed successfully for July 2026 |
| Department trend | Returned one row per day for 2026-07-01 through 2026-07-10, including weekend rows and a moving average |
| `GET /admin/explain/attendance_list` | Returned successfully after Extended JSON conversion; the observed winning plan used `IXSCAN` on `date_-1_emp_code_1`, examined 9 keys/documents, and reported no `COLLSCAN` in the winning plan |
| Route registration | Terminal command listed all 12 required API operations |
| `git diff --check` | Passed with no output |

The health response and basic CRUD checks should be rerun after replacing `app/main.py`; a prior successful response is not a substitute for post-change regression testing.

## Remaining tests recommended before submission

These are important checks that have not been established as fully covered by the manual observations above:

1. Test 422 responses for malformed dates/months, invalid epoch milliseconds, invalid status, invalid page size, and reversed date ranges.
2. Test exact late boundary instants (`09:40:00` and `09:40:01` for a `09:30` shift), overtime just below/at 30 minutes, half-day at rounded `4.50` hours, and whole-second truncation.
3. Test an overnight shift across midnight, including punch-in before `shift_end`, punch-out selection, date assignment, and the 24-hour limit.
4. Test PATCH no-op rejection, ABSENT/LEAVE timestamp clearing, rejection of timestamps supplied with ABSENT/LEAVE, history preservation for both legacy and already-corrected documents, and concurrent edits.
5. Verify leaderboard ties produce competition ranks such as `1, 2, 2, 4`, including when a tie crosses `limit`.
6. Test employees with no logs, mid-month join dates, weekends, zero-working-day months, department filters, and a trend range at the 92-day maximum.
7. Run explain requests for each named endpoint with the required parameters and inspect whether the query plan uses appropriate indexes on the target dataset.
8. Run regression tests against the assignment's large dataset (around 100,000 attendance documents) and compare aggregation responses to independently calculated expected values.

## Operational cautions

- Keep `.env` out of Git and never put MongoDB credentials in a README, screenshot, or commit.
- `sample_seed.py` deletes all documents in the configured `employees` and `attendance_logs` collections before inserting samples; do not run it against data you need to preserve.
- Manual Swagger checks on a small sample dataset demonstrate that paths execute; they do not prove every rule or large-dataset performance target.
