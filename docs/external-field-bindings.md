# External Field Bindings

Orchestra can declare context columns whose values are **lazily hydrated from
external REST APIs**, with dependency hashing, TTL freshness, and batched
connector execution.

This is distinct from SQL/`equation` derived columns (`ActiveDerivedLog`), which
remain purely in-Orchestra expressions.

## Concepts

| Piece | Role |
|---|---|
| `field_category=external_entry` | Immutable column whose value comes from a connector |
| `external_field_binding` | Binding contract (connector id, inputs, cache, HTTP config) |
| Hydrate planner | Groups rows, calls `connector.batch_fetch`, scatters results |
| Cache sidecar `__ext__{field}` | Stores `{hash, fetched_at, token, error}` on `LogEvent.data` |
| Response bucket `external_entries` | Parallel to `derived_entries` |

## Binding shape

```json
{
  "connector_id": "http.generic",
  "inputs": [{"name": "item_id", "column": "item_id"}],
  "batch": {"group_by": [], "max": 100, "concurrency": 8},
  "cache": {"mode": "hash_ttl", "ttl_seconds": 300},
  "on_error": "fail",
  "auth_secret_ref": "MY_API_TOKEN",
  "http": {
    "method": "GET",
    "url_template": "https://api.example.com/items/{item_id}",
    "response_jsonpath": "$.status"
  }
}
```

- ``auth_secret_ref`` is resolved from the **tenant Secrets vault** owned by
  the bound table's context:
  - ``Teams/{team_id}/…`` → ``Teams/{team_id}/Secrets``
  - ``{user_id}/{agent_id}/…`` → ``{user_id}/{agent_id}/Secrets``
  Process environment is a **local/dev fallback only**. Production bindings
  must plant the named secret in the vault (e.g. via SecretManager /
  ``primitives.secrets``). The name is never returned by ``GET /logs/fields``.
- Auth placement (optional `auth` object on the binding):
  - default / `"placement": "bearer"` → `Authorization: Bearer <secret>`
  - `"placement": "query", "param": "api_key"` → append `?api_key=<secret>`
    (SmartLead and similar query-param APIs)
- Prefer `http.batch_url` when the remote API accepts arrays; otherwise the
  planner still issues **one** `batch_fetch` per group and the connector applies
  bounded concurrency.
- **SSRF baseline:** `http.generic` refuses non-`http(s)` schemes and
  private / link-local / metadata targets (including `169.254.169.254` and
  `metadata.google.internal`).

## Create a bound field

`POST /logs/fields`:

```json
{
  "project_name": "demo",
  "context": "Data/things",
  "fields": {
    "remote_status": {
      "type": "str",
      "category": "external_entry",
      "binding": { "...": "..." }
    }
  }
}
```

## Read / hydrate

`GET /logs` query params:

| Param | Default | Meaning |
|---|---|---|
| `hydrate` | `stale_ok` | `none` \| `stale_ok` \| `force` |
| `hydrate_fields` | all | Ampersand-separated field names |
| `materialize` | `true` | Persist value + sidecar into `LogEvent.data` |

Explicit: `POST /logs/hydrate`.

Update binding (bumps `binding_version`, invalidating caches):
`PUT /logs/fields/binding`.

## Filter / sort rules (v1)

SQL filter/sort applies to **stored** JSONB only. After materialization,
external values are stored under the field name and can participate in later
filters. Virtual-only (never materialized) external columns are not
pushdown-filterable.

## Hydrate on grouped and federated reads

`hydrate` / `hydrate_fields` / `materialize` apply on:

| Surface | Behavior |
|---|---|
| `GET /logs` (flat) | After `_format_logs` |
| `GET /logs` (grouped / nested) | Per leaf fetch in `_build_grouped_data` |
| `GET /logs` (flat groups) | After `_format_flat_logs` (no sidecar reload) |
| `POST /logs/query` | Same as GET for grouped and non-grouped |
| Federated `POST /logs/federated` | **Per context branch** after that branch formats |

`groups_only` / ids-only responses skip hydrate (no row payloads).

## Write-through / outbox (Phase 4)

External columns **observe** remote state. Mutations go through an outbox:

1. `POST /logs/external_write` enqueues an `external_write_intent`
   (`deliver=async` default, or `deliver=sync` for in-request delivery).
2. Admin `POST /admin/external_writes/drain` delivers pending intents.
3. On confirm, hydrate sidecars for `log_event_ids` are invalidated so the
   next read re-fetches.

### Cloud Scheduler (drain)

Jobs live in GCP project `gcp-project-saas` / location `us-central1`. Auth matches
other Orchestra admin crons: static Bearer `ORCHESTRA_ADMIN_KEY` from Secret
Manager (not OIDC).

| Job | URI | Schedule |
|---|---|---|
| `orchestra-external-writes-drain-scheduler-staging` | `https://internal.example.com/v0/admin/external_writes/drain` | `* * * * *` (every minute) |
| `orchestra-external-writes-drain-scheduler` | `https://api.unify.ai/v0/admin/external_writes/drain` | `* * * * *` (every minute) |

Body: `{"limit": 100}`. Attempt deadline 180s; one retry with 10–120s backoff.

Ensure / update idempotently:

```bash
bash deploy/ensure_external_writes_drain_scheduler.sh
bash deploy/ensure_external_writes_drain_scheduler.sh --dry-run
```

Latency-sensitive callers (e.g. SmartLead reply drain inside a tick) may still
use `deliver=sync`. Prefer `deliver=async` for fire-and-forget mutations that
can wait up to ~1 minute for the scheduler.

Idempotency is unique on `(project_id, idempotency_key)`. Connectors implement
`execute_write` (see `http.generic` with `binding.write.url_template`).

```json
{
  "project_name": "demo",
  "context": "Data/things",
  "field_name": "remote_status",
  "idempotency_key": "reply-job-42",
  "payload": {"thread_id": "…", "body": "…"},
  "log_event_ids": [123],
  "deliver": "async"
}
```

Do not encode side-effect sends inside hydrate `batch_fetch`.

## UniSDK / Unify

- UniSDK merges `external_entries` into `Log` objects like derived entries.
- UniSDK: `get_logs(hydrate=…)`, `hydrate_logs`, `update_external_field_binding`,
  `request_external_write`.
- DataManager / `primitives.data`: `create_external_column`,
  `filter(..., hydrate=...)`, `request_external_write`.
- Auth: plant named secrets in the owning `Secrets` context before hydrate or
  write. Orchestra does not need vendor API keys on Cloud Run.

## Catalog convention (user data)

Optional reusable REST target rows (e.g. `Data/ExternalApiTargets`) can store
base URLs, auth placement, and binding templates. CodeAct / operators plant
domain tables and `external_entry` columns from those rows. Orchestra stays
connector-generic (`http.generic` only).
