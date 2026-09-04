import time
from functools import wraps

from flask import Response, g, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

HTTP_REQUESTS = Counter(
    "miage_scholar_http_requests_total",
    "Completed HTTP requests.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "miage_scholar_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route", "status"),
    buckets=_LATENCY_BUCKETS,
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "miage_scholar_http_requests_in_progress",
    "HTTP requests currently being processed.",
    ("method", "route"),
)

SOCKETIO_EVENTS = Counter(
    "miage_scholar_socketio_events_total",
    "Completed Socket.IO application events.",
    ("event", "outcome"),
)
SOCKETIO_EVENT_DURATION = Histogram(
    "miage_scholar_socketio_event_duration_seconds",
    "Socket.IO application event duration in seconds.",
    ("event",),
    buckets=_LATENCY_BUCKETS,
)
SOCKETIO_EVENTS_IN_PROGRESS = Gauge(
    "miage_scholar_socketio_events_in_progress",
    "Socket.IO application events currently being processed.",
    ("event",),
)

PROVIDER_QUERIES = Counter(
    "miage_scholar_provider_queries_total",
    "Queries made by the application to an external scholarly data provider.",
    ("provider",),
)
PROVIDER_QUERY_FAILURES = Counter(
    "miage_scholar_provider_query_failures_total",
    "Failed external scholarly data provider queries.",
    ("provider",),
)
PROVIDER_CACHE_LOOKUPS = Counter(
    "miage_scholar_provider_cache_lookups_total",
    "Cache outcomes for external scholarly data provider queries.",
    ("provider", "outcome"),
)
PROVIDER_UNCACHED_RESULTS = Counter(
    "miage_scholar_provider_uncached_results_total",
    "Results retrieved from an external provider after a cache miss.",
    ("provider",),
)
PROVIDER_PAPERS_RETRIEVED = Counter(
    "miage_scholar_provider_papers_retrieved_total",
    "Papers returned by an external scholarly data provider.",
    ("provider",),
)

for _provider in ("openalex", "scopus", "arxiv"):
    PROVIDER_QUERIES.labels(_provider)
    PROVIDER_QUERY_FAILURES.labels(_provider)
    PROVIDER_UNCACHED_RESULTS.labels(_provider)
    PROVIDER_PAPERS_RETRIEVED.labels(_provider)
    for _outcome in ("hit", "miss"):
        PROVIDER_CACHE_LOOKUPS.labels(_provider, _outcome)

PROCESS_START_TIME = Gauge(
    "miage_scholar_process_start_time_seconds",
    "Unix timestamp when this application process started.",
)
PROCESS_START_TIME.set(time.time())


def _request_route():
    return request.url_rule.rule if request.url_rule is not None else "unmatched"


def _record_http_request(status):
    state = getattr(g, "_prometheus_request_state", None)
    if state is None or state["recorded"]:
        return

    duration = time.monotonic() - state["started_at"]
    labels = (state["method"], state["route"], str(status))
    HTTP_REQUESTS.labels(*labels).inc()
    HTTP_REQUEST_DURATION.labels(*labels).observe(duration)
    HTTP_REQUESTS_IN_PROGRESS.labels(state["method"], state["route"]).dec()
    state["recorded"] = True


def register_http_metrics(app):
    @app.before_request
    def start_http_metrics():
        if request.path == "/metrics":
            return None

        method = request.method
        route = _request_route()
        g._prometheus_request_state = {
            "started_at": time.monotonic(),
            "method": method,
            "route": route,
            "recorded": False,
        }
        HTTP_REQUESTS_IN_PROGRESS.labels(method, route).inc()
        return None

    @app.after_request
    def finish_http_metrics(response):
        _record_http_request(response.status_code)
        return response

    @app.teardown_request
    def finish_failed_http_metrics(error):
        state = getattr(g, "_prometheus_request_state", None)
        if state is not None and not state["recorded"]:
            _record_http_request(500 if error is not None else "unknown")

    @app.route("/metrics", methods=["GET"])
    def prometheus_metrics():
        return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


def monitor_socketio_event(event):
    def decorator(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            started_at = time.monotonic()
            outcome = "success"
            SOCKETIO_EVENTS_IN_PROGRESS.labels(event).inc()
            try:
                return handler(*args, **kwargs)
            except BaseException:
                outcome = "error"
                raise
            finally:
                SOCKETIO_EVENTS.labels(event, outcome).inc()
                SOCKETIO_EVENT_DURATION.labels(event).observe(
                    time.monotonic() - started_at
                )
                SOCKETIO_EVENTS_IN_PROGRESS.labels(event).dec()

        return wrapped

    return decorator


def record_provider_query(provider):
    PROVIDER_QUERIES.labels(provider).inc()


def record_provider_query_failure(provider):
    PROVIDER_QUERY_FAILURES.labels(provider).inc()


def record_provider_cache_lookup(provider, cache_hit):
    outcome = "hit" if cache_hit else "miss"
    PROVIDER_CACHE_LOOKUPS.labels(provider, outcome).inc()


def record_provider_uncached_results(provider, result_count):
    if result_count > 0:
        PROVIDER_UNCACHED_RESULTS.labels(provider).inc(result_count)


def record_provider_papers_retrieved(provider, paper_count):
    if paper_count > 0:
        PROVIDER_PAPERS_RETRIEVED.labels(provider).inc(paper_count)


def _provider_response_result_count(response):
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return 0

    if not isinstance(payload, dict):
        return 0

    results = payload.get("results")
    if isinstance(results, list):
        return len(results)

    search_results = payload.get("search-results")
    if isinstance(search_results, dict):
        entries = search_results.get("entry")
        if isinstance(entries, list):
            return len(entries)
        if isinstance(entries, dict):
            return 1

    if payload.get("id") or payload.get("doi") or payload.get("DOI"):
        return 1
    return 0


def instrument_cached_provider_session(session, provider):
    if getattr(session, "_miage_metrics_provider", None) == provider:
        return session

    original_request = session.request

    @wraps(original_request)
    def instrumented_request(method, url, **kwargs):
        record_provider_query(provider)
        try:
            response = original_request(method, url, **kwargs)
        except BaseException:
            record_provider_query_failure(provider)
            raise

        cache_hit = bool(getattr(response, "from_cache", False))
        record_provider_cache_lookup(provider, cache_hit)
        status_code = getattr(response, "status_code", 0)
        if status_code >= 400:
            record_provider_query_failure(provider)
        else:
            result_count = _provider_response_result_count(response)
            record_provider_papers_retrieved(provider, result_count)
            if not cache_hit:
                record_provider_uncached_results(provider, result_count)
        return response

    session.request = instrumented_request
    session._miage_metrics_provider = provider
    return session
