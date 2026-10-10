# Engineering Decisions

This document records the main implementation choices for the Employee Attendance & Analytics API and why they were selected. The assignment contract in `openapi.yaml`, `DATA_MODEL.md`, and `PROBLEM_STATEMENT.docx` remains authoritative.

## 1. Employee identity and attendance natural key

**Decision:** Use `emp_code` as the business identifier and `(emp_code, date)` as the attendance-record key. Do not expose MongoDB `_id` as an API identifier.

**Reason:** The assignment states that employee codes are customer-generated, stable identifiers. The unique indexes prevent duplicate employee codes and duplicate attendance records for the same employee/date, including concurrent writes.

## 2. MongoDB indexes created at startup

**Decision:** Create indexes idempotently during application startup. They cover unique employee code, joined date/department queries, unique attendance employee/date, date ordering/ranges, and status/date queries.

**Reason:** The contract requires indexes to be created by the application and expects the attendance list and analytics to stay efficient on a large dataset. Composite index order is chosen to support the filters and sort patterns used by the endpoints. Explain output is available to inspect query plans.

## 3. UTC storage and epoch-millisecond API values

**Decision:** Store timestamps as BSON datetimes in UTC, convert instants to IST when applying attendance rules, truncate to whole seconds, and expose instants as epoch-millisecond integers.

**Reason:** BSON dates represent instants; the API contract uses epoch milliseconds to avoid ambiguous timezone-formatted strings. Explicit conversion also handles PyMongo's default naive UTC datetimes. Calendar dates (`date`, `joined_on`) and shift times remain strings because they are calendar values, not instants.

## 4. Explicit business-rule helpers

**Decision:** Keep date/month/time parsing, timezone normalization, late minutes, work hours, overtime, working days, and rounding in shared helpers.

**Reason:** Shared helpers reduce inconsistent calculations across punch-in, punch-out, PATCH, and analytics. `Decimal` with `ROUND_HALF_UP` implements the assignment's required rounding rather than Python's default bankers' rounding.

## 5. Race-safe punch-in and punch-out

**Decision:** Rely on the unique `(emp_code, date)` index for duplicate punch-in detection. For punch-out, find the latest punch-in at or before the supplied event time, validate the elapsed time, and update only if `punch_out` is still null.

**Reason:** A check-then-insert alone is vulnerable to simultaneous requests. Database uniqueness and conditional updates let one operation win and report a conflict for a duplicate/concurrent operation.

## 6. PATCH as an auditable correction

**Decision:** PATCH only accepts status and punch timestamp changes plus `reason` and `regularized_by`. It recomputes derived fields from the final values, rejects no-op changes, appends one history entry, and uses an optimistic update filter to detect a concurrent change. Legacy records with no `history` field are handled as having an empty history.

**Reason:** The assignment requires an append-only audit trail that records only fields that actually changed. Matching existing values in the update filter prevents two overlapping edits from silently overwriting each other or losing history.

## 7. MongoDB aggregation for reporting

**Decision:** Use MongoDB aggregation pipelines for monthly summaries, cross-collection department summaries, the late leaderboard, and daily department trends. The late leaderboard uses `$rank`, not array position; the daily trend uses `$densify` to fill dates that have no attendance records.

**Reason:** The assignment specifically evaluates aggregation work, correct headcount across employees with no logs, ties in ranking, and daily gap filling. Stored derived fields are treated as authoritative for analytics.

## 8. Explain output compatibility

**Decision:** Run MongoDB explain with `executionStats` and convert BSON-only values to Extended JSON before returning the payload.

**Reason:** Explain results may contain BSON types such as `Timestamp` that FastAPI cannot encode directly. Extended JSON preserves the plan information while making the response JSON serializable.

## 9. Configuration and secrets

**Decision:** Load `MONGO_URI` and `MONGO_DB` from environment variables, with `.env` as a local convenience. Do not include credentials in source code or documentation.

**Reason:** This keeps credentials separate from application code and allows local and deployment environments to configure MongoDB independently.
