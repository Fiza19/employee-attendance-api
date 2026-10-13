# Design decisions

1. **Indexes.** A unique `employees.emp_code` index prevents duplicate employee identities. A unique `(emp_code, date)` attendance index guarantees one daily record and makes punch-in race-safe. Employee department/code and department/join-date indexes support sorted listing, department filtering and headcount. Attendance date/employee, date/status/employee, and employee/punch-in indexes support filtered listing, monthly analytics, and locating an open punch.

2. **Punch-in race.** Both requests may check the employee successfully, but both insert attempts compete at the unique compound index. MongoDB accepts one; the other receives `DuplicateKeyError` and the API returns 409. No pre-check is treated as the concurrency guarantee.

3. **Ties.** The leaderboard uses `$rank`, not row position, then returns every row whose rank is at most `limit`. A tie at the cutoff is therefore included in full, and the next rank is skipped.

4. **Headcount.** The department summary starts from eligible employee documents and looks up attendance records. It preserves employees with an empty log array, excludes employees who joined after month-end, and therefore counts staff with no attendance records.

5. **100x scale.** I would validate plans and workload with production-like data, add observability and caching for stable reports, consider pre-aggregated daily summaries, and move heavy analytics to scheduled materialized collections if latency required it.
