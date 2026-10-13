# Code review findings

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Replaces clock fields on the punch-in date, mishandling overnight shifts and not consistently interpreting IST. | Test a 22:00–06:00 shift and a 03:00 punch-in. | Build timezone-aware shift instants in IST and infer the prior start date only when needed. |
| 2 | `compute_work_hours` | Python `round()` uses ties-to-even rather than the required decimal half-up rounding. | Use a duration exactly at a half-cent rounding boundary. | Use `Decimal.quantize(..., ROUND_HALF_UP)`. |
| 3 | `compute_overtime` | Always uses the same calendar date and counts overtime from minute zero. | Test an overnight shift and an early punch-out less than 30 minutes after shift end. | Resolve the correct shift end instant and only count overtime when floored minutes are at least 30. |
| 4 | `/health` | Always returns success even if MongoDB is unavailable. | Stop MongoDB and request `/health`. | Run `ping` and return 503 on connection failure. |
| 5 | `create_employee` | Check-then-insert is not concurrency-safe and timestamps are naive. | Send duplicate concurrent creates. | Enforce a unique index and catch `DuplicateKeyError`; store UTC-aware timestamps. |
| 6 | `list_employees` | Uses `page * page_size`, counts unfiltered records, and has no explicit sort or bounds. | Page 1 omits the first page; filtered totals disagree. | Use `(page-1)*page_size`, filtered count, ascending `emp_code` sort, and page-size bounds. |
| 7 | `punch_in` | Does not check unknown employees, accepts unsupported status values, uses local server time, and races on check-then-insert. | Unknown code; invalid status; simultaneous requests; non-IST server. | Validate inputs, normalize epoch milliseconds, check employee existence, and rely on a unique `(emp_code,date)` index. |
| 8 | `list_attendance` | Loads every matching document into Python and paginates after sorting. | Large dataset causes memory and latency growth. | Count in MongoDB and apply indexed sort, skip, and limit in the database. |
| 9 | Attendance outputs | Internal `_id` leaked in the original list path; BSON dates were not converted to epoch milliseconds. | Inspect response shape and timestamp types. | Serialize the natural record fields and convert all instants, including nested audit changes, to milliseconds. |
| 10 | Entire app | Required indexes and seven endpoints were absent. | Inspect startup indexes and OpenAPI route coverage. | Create indexes at startup and implement punch-out, regularization, analytics, trend, and explain routes. |

## Reviewed and retained

- The natural attendance key `(emp_code, date)` matches the data model; a unique compound index now enforces it.
- `attendance_logs` is the correct collection name, and employee references use `emp_code`, not MongoDB `_id`.
- The use of FastAPI and PyMongo is appropriate for this assignment; analytics are expressed as MongoDB aggregation pipelines.
