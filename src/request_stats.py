# FILE: financial-market-project/src/request_stats.py   (NEW FILE)
# PLACE: same folder as serve_api.py.
# PURPOSE: counts real API calls per day so the dashboard shows a true, stable number.
# NOTE: the count lives in memory, so it restarts from 0 when uvicorn restarts.

from datetime import date

from fastapi import Depends, Request

IGNORED_PREFIXES = ("/ui", "/stats", "/docs", "/redoc", "/openapi.json", "/favicon.ico")

todays_requests = {"day": date.today(), "total": 0, "failed": 0}


# Starts a fresh count when the calendar day changes so "today" stays true.
def reset_if_new_day():
    if todays_requests["day"] != date.today():
        todays_requests.update(day=date.today(), total=0, failed=0)


# Skips page loads, docs and the stats call itself so only real API calls are counted.
def should_count(path):
    if path == "/":
        return False
    return not path.startswith(IGNORED_PREFIXES)


# Adds one finished request to today's totals and tracks the failed ones separately.
def record_request(status_code):
    reset_if_new_day()
    todays_requests["total"] += 1
    if status_code >= 400:
        todays_requests["failed"] += 1


# Plugs the counter middleware and the /stats route into the existing FastAPI app.
def install_request_counter(app, require_key):
    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        response = await call_next(request)
        if should_count(request.url.path):
            record_request(response.status_code)
        return response

    @app.get("/stats", dependencies=[Depends(require_key)])
    def request_stats():
        reset_if_new_day()
        return {
            "day": todays_requests["day"].isoformat(),
            "requests_today": todays_requests["total"],
            "failed_today": todays_requests["failed"],
        }