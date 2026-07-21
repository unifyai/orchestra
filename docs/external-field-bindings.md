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

- `auth_secret_ref` is resolved from the Orchestra process environment only.
  It is never returned by `GET /logs/fields`.
- Prefer `http.batch_url` when the remote API accepts arrays; otherwise the
  planner still issues **one** `batch_fetch` per group and the connector applies
  bounded concurrency.

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

## Write-through (Phase 4 — not in this release)

External columns **observe** remote state. Mutations that must not diverge
belong on a future outbox / through-write intent API. Do not encode side-effect
writes inside hydrate connectors.

## UniSDK / Unify

- UniSDK merges `external_entries` into `Log` objects like derived entries.
- DataManager exposes `create_external_column` and `filter(..., hydrate=...)`.
