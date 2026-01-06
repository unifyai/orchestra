# Orchestra Logging & Tracing

This document covers the logging infrastructure for Orchestra: server-side API request traces with OpenTelemetry instrumentation.

---

## Log Directory Overview

All logs are organized under `logs/` with one main subdirectory:

| Directory | Purpose | Structure | Control |
|-----------|---------|-----------|---------|
| `logs/orchestra/` | Server-side API traces | Per-request JSON with spans | `ORCHESTRA_LOG_DIR` |

---

## Orchestra Logs (`logs/orchestra/`)

Orchestra logs capture detailed server-side API request traces using OpenTelemetry. Each request generates a JSON file containing all spans from the request lifecycle, including HTTP handling, database queries, and any external calls.

### Directory Structure

```
logs/orchestra/
└── 2026-01-05T22-00-00_orchestrapid12345/
    └── requests/
        ├── 2026-01-05T22-00-01.123_GET_projects_45ms_200_f124f0d3.json
        ├── 2026-01-05T22-00-02.456_POST_logs_120ms_201_a1b2c3d4.json
        ├── 2026-01-05T22-00-03.789_DELETE_project-name_PENDING_e5f6g7h8.json
        └── ...
```

### Log File Naming

Each request generates a JSON file with a descriptive filename:

```
{datetime}_{METHOD}_{route}_{duration}ms_{status}_{trace_id_short}.json
```

| Component | Example | Description |
|-----------|---------|-------------|
| `datetime` | `2026-01-05T22-00-01.123` | Request start time (millisecond precision) |
| `METHOD` | `GET`, `POST`, `DELETE` | HTTP method |
| `route` | `projects`, `logs`, `project-name` | API route (path params replaced with placeholders) |
| `duration` | `45ms`, `PENDING` | Request duration (or `PENDING` while in-flight) |
| `status` | `200`, `404`, `500` | HTTP status code |
| `trace_id_short` | `f124f0d3` | Last 8 chars of OpenTelemetry trace ID |

### Log File Contents

Each JSON file contains the full request trace with all spans:

```json
{
  "trace_id": "099b207f89222185695d25977be454fc",
  "status": "complete",
  "spans": [
    {
      "name": "GET /v0/projects",
      "span_id": "a1b2c3d4e5f6a7b8",
      "parent_span_id": null,
      "start_time": "2026-01-05T22:00:01.123Z",
      "end_time": "2026-01-05T22:00:01.168Z",
      "duration_ms": 45,
      "attributes": {
        "http.method": "GET",
        "http.route": "/v0/projects",
        "http.status_code": 200,
        "http.request.query_params": "{}",
        "http.request.body": "{...}"
      }
    },
    {
      "name": "SELECT projects",
      "span_id": "i9j0k1l2m3n4o5p6",
      "parent_span_id": "a1b2c3d4e5f6a7b8",
      "attributes": {
        "db.system": "postgresql",
        "db.statement": "SELECT id, name, ... FROM project WHERE ...",
        "db.operation": "SELECT"
      }
    },
    {
      "name": "SELECT contexts",
      "span_id": "q7r8s9t0u1v2w3x4",
      "parent_span_id": "a1b2c3d4e5f6a7b8",
      "attributes": {
        "db.system": "postgresql",
        "db.statement": "SELECT id, name, ... FROM context WHERE ...",
        "db.operation": "SELECT"
      }
    }
  ]
}
```

### Key Span Attributes

**HTTP spans** (request handling):

| Attribute | Description |
|-----------|-------------|
| `http.method` | HTTP method (GET, POST, etc.) |
| `http.route` | API route pattern (e.g., `/v0/projects/{project}`) |
| `http.status_code` | Response status code |
| `http.request.query_params` | Query parameters (JSON) |
| `http.request.body` | Request body (JSON, truncated for large payloads) |
| `http.response.body` | Response body (JSON, truncated) |

**Database spans** (SQL queries):

| Attribute | Description |
|-----------|-------------|
| `db.system` | Database type (`postgresql`) |
| `db.statement` | Full SQL query |
| `db.operation` | Operation type (`SELECT`, `INSERT`, `UPDATE`, `DELETE`) |
| `db.rows_affected` | Number of rows affected (for mutations) |

**External call spans** (OpenAI, etc.):

| Attribute | Description |
|-----------|-------------|
| `openai.model` | Model name |
| `openai.prompt_tokens` | Input token count |
| `openai.completion_tokens` | Output token count |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCHESTRA_LOG_DIR` | `""` (disabled) | Directory for per-request trace files |

**Enabling file logging:**
```bash
export ORCHESTRA_LOG_DIR=/path/to/logs/orchestra
```

The test infrastructure sets this automatically via `pytest_sessionstart`.

### In-Progress Traces

For long-running requests, trace files are written incrementally:
1. File created immediately with `status: "in_progress"` and `PENDING` in filename
2. Updated periodically (every 500ms) as new spans complete
3. Renamed on completion with actual duration and status code

This allows debugging long-running requests before they complete.

---

## Trace Correlation

Orchestra traces correlate with client-side traces via the OpenTelemetry trace ID. When requests come from:

- **Unify SDK**: The `traceparent` header propagates the trace context
- **Unity**: Same trace ID flows through unillm → unify → orchestra

The `trace_id_short` suffix (last 8 chars) in filenames enables quick correlation:

```bash
# Find all traces for a specific trace ID
ls logs/orchestra/*/requests/*f124f0d3*
```

---

## Reading Trace Files

```bash
# View all spans for a request (pretty-printed)
cat logs/orchestra/2026-01-05T22-00-00_orchestrapid12345/requests/*f124f0d3*.json | jq .

# Find slow requests (>1s)
for f in logs/orchestra/*/requests/*.json; do
  duration=$(jq -r '.spans[0].duration_ms // 0' "$f" 2>/dev/null)
  if [ "$duration" -gt 1000 ] 2>/dev/null; then
    echo "$f: ${duration}ms"
  fi
done

# Find failed requests
ls logs/orchestra/*/requests/*_4??_*.json logs/orchestra/*/requests/*_5??_*.json 2>/dev/null

# Find all database queries in a trace
cat logs/orchestra/*/requests/*f124f0d3*.json | jq '.spans[] | select(.attributes["db.statement"] != null) | {name, statement: .attributes["db.statement"], duration_ms}'
```

---

## Integration with Other Repos

When Orchestra runs as part of the full stack:

1. **Unity** creates the root trace span
2. **Unillm** creates LLM call spans
3. **Unify SDK** creates HTTP client spans
4. **Orchestra** receives the trace context and creates server-side spans

All services can write to a shared `logs/all/` directory for unified trace analysis. Set `ORCHESTRA_OTEL_LOG_DIR` to enable this:

```bash
export ORCHESTRA_OTEL_LOG_DIR=/path/to/logs/all
```

---

## Disabling Trace Logging

To disable trace file generation:

```bash
unset ORCHESTRA_LOG_DIR
# or
export ORCHESTRA_LOG_DIR=""
```

OpenTelemetry instrumentation remains active for observability platforms (Tempo, Jaeger) but no local files are written.
