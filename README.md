# Prometheus metrics

The application exposes Prometheus metrics at `GET /metrics`. This endpoint is
not authenticated and should only be reachable by Prometheus through a trusted
network or reverse-proxy rule.

Application metrics include normalized Flask route traffic and Socket.IO event
traffic. Query text, feed IDs, DOI values, and other user-controlled values are
never used as metric labels.

Useful PromQL queries:

```promql
# Request rate
sum(rate(miage_scholar_http_requests_total[5m]))
```

```promql
# HTTP 5xx ratio
sum(rate(miage_scholar_http_requests_total{status=~"5.."}[5m]))
/
clamp_min(sum(rate(miage_scholar_http_requests_total[5m])), 0.000001)
```

```promql
# HTTP p95 latency
histogram_quantile(
  0.95,
  sum by (le) (rate(miage_scholar_http_request_duration_seconds_bucket[5m]))
)
```

```promql
# Application process restarts observed during the last 24 hours
changes(miage_scholar_process_start_time_seconds[24h])
```

Socket.IO errors can be monitored with:

```promql
sum(rate(miage_scholar_socketio_events_total{outcome="error"}[5m])) by (event)
```

Provider query rates and uncached result throughput:

```promql
sum(rate(miage_scholar_provider_queries_total[5m])) by (provider)
```

```promql
sum(rate(miage_scholar_provider_uncached_results_total[5m])) by (provider)
```

Cache hit ratio by provider:

```promql
sum(rate(miage_scholar_provider_cache_lookups_total{outcome="hit"}[5m])) by (provider)
/
clamp_min(
  sum(rate(miage_scholar_provider_cache_lookups_total[5m])) by (provider),
  0.000001
)
```

`provider_queries_total` counts logical API operations, including cache hits.
`provider_uncached_results_total` counts records in successful responses obtained
after cache misses. Provider retries remain part of the same logical operation.

The restart query assumes the production configuration of one Gunicorn worker.
Prometheus multiprocess mode is required before increasing the worker count.
