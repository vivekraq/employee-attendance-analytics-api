# Starter review

| # | Where | What is wrong | How you would notice | How you fixed it |
|---|---|---|---|---|
| 1 | `GET /attendance` | It materialized and sorted every matching record in Python, which becomes slow and memory-heavy on the required large dataset. | Seed many records, request the first page, and watch the endpoint fetch far more than `page_size`. | Count, sort, skip, and limit in MongoDB; only serialize the requested page. |
| 2 | `GET /attendance` | `page` and `page_size` had no bounds. | Try `page=0` or `page_size=1000`; both were accepted and produced unintended results. | Added `page >= 1` and `1 <= page_size <= 100` validation. |
| 3 | `GET /attendance` | Invalid calendar dates and reversed date ranges were accepted as query filters. | Try `date_from=2026-02-30` or a `date_from` after `date_to`. | Validate real `YYYY-MM-DD` dates and return 422 for an invalid or reversed range. |
| 4 | `GET /attendance` | `status` accepted values outside the contract. | Query `status=HOLIDAY`; the starter returned an empty page instead of 422. | Constrained it to the five supported attendance statuses. |
| 5 | `GET /employees` | An explicitly supplied empty `department` value was treated as if the filter were omitted. | Query with `department=` and observe unfiltered results. | Apply the filter whenever the parameter is present, including the empty string. |

I checked the existing indexes and found them appropriate for the starter endpoints: the unique `(emp_code, date)` index enforces the attendance natural key, and the date/employee index supports the attendance sort. The time helpers also match the documented truncation, overnight-date, grace-period, overtime-threshold, and half-up rounding rules. The other routes were TODOs explicitly assigned as the build portion of the exercise, so I implemented them separately rather than listing them as defects in the existing starter.
