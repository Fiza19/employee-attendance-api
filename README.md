# Employee Attendance & Analytics API

FastAPI and PyMongo implementation of the supplied Employee Attendance & Analytics API contract. All application code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0 recommended for the aggregation stages used here)

## Run locally

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Configure MONGO_URI and MONGO_DB as environment variables.
uvicorn app.main:app --port 8000
```

The default connection is `mongodb://localhost:27017` and database `attendance_db`. Indexes are created at startup. `GET /health` checks MongoDB connectivity. API timestamps are integer epoch milliseconds; MongoDB stores instants as BSON dates in UTC. Shift and attendance calendar rules use IST (UTC+05:30).

## Included

- Employee creation and listing
- Punch-in/punch-out and audited manual regularization
- Monthly, department, leaderboard, and daily trend analytics using MongoDB aggregation pipelines
- Explain endpoint for selected query plans
- `REVIEW.md` and `DECISIONS.md` for the assignment's review and design questions

## Validation note

The application source passes Python syntax compilation. Full endpoint and query-plan integration tests require a running MongoDB instance and should be run in that environment before relying on the results. The assignment's hidden dataset is not available locally.
