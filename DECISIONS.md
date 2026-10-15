1. **Indexes.** The unique `emp_code` index protects employee identity. The unique `(emp_code, date)` attendance index both enforces one record per employee-day and supports an employee's monthly range. The date-descending/employee-ascending index supports the attendance list's required order. The employee department and join-date indexes support headcount filtering and joins. I considered indexing every analytics field, but that adds write cost without helping the main range lookups.

2. **Punch-in race.** Both requests may read the employee and calculate the same attendance date. They then try to insert the natural key. MongoDB's unique index accepts one insert; the other gets `DuplicateKeyError`, which the API returns as 409.

3. **Ties.** The leaderboard uses MongoDB competition ranking. If the limit lands on a tied rank, every employee at that rank is returned. Rows with equal totals are ordered by employee code.

4. **Headcount.** The department summary starts with eligible employee documents and looks up that month's logs. It preserves employees without logs and counts unique employee codes, while excluding people who join after the month ends.

5. **100x data.** I would measure the actual query plans and latency, then consider pre-aggregated daily facts refreshed incrementally. I would keep the raw logs as the source of truth and verify every aggregate against them.
