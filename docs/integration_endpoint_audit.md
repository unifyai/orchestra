# Integration Endpoint Audit

This audit captures local call-site evidence for the provider integration API.
It exists to make endpoint cleanup explicit: routes should be removed only after
their Console, Unity, Unify, Unity Deploy, and Orchestra bootstrap consumers have
migrated.

## Runtime Routes

| Endpoint | Local consumers | Status |
| --- | --- | --- |
| `GET /v0/integrations/apps` | Console catalog UI, Unify SDK | Keep; canonical paginated app list contract |
| `GET /v0/integrations/apps/search` | Unify SDK, Unity integration primitives | Keep; ranked/semantic app discovery |
| `GET /v0/integrations/apps/{canonical_app_slug}` | Console detail UI | Keep |
| `GET /v0/integrations/connections` | Console, Unify SDK, Unity sync state | Keep |
| `POST /v0/integrations/connect/start` | Console connect flow | Keep |
| `POST /v0/integrations/connections/complete-by-provider` | Console OAuth callback | Keep |
| `POST /v0/integrations/connections/{connection_id}/complete` | Console OAuth callback fallback | Keep |
| Connection lifecycle routes | Console connection management | Keep |
| `GET /v0/integrations/tools` | Unify SDK, Unity FunctionManager materialization | Keep; canonical paginated/filter-based tool list |
| `GET /v0/integrations/tools/search` | Unify SDK, Unity integration primitives, local Console seed helper | Keep; ranked/semantic tool discovery |
| Tool schema/run routes | Unify SDK, Unity tool execution | Keep |

## Admin Routes

| Endpoint | Local consumers | Status |
| --- | --- | --- |
| `GET /v0/admin/integrations/backends` | Console admin client tests | Keep; basic backend row list |
| `GET /v0/admin/integrations/backends/status` | Operators and deploy diagnostics | Canonical status view |
| `POST /v0/admin/integrations/backends` | Orchestra cloud bootstrap, Unify SDK, local Console bootstrap | Keep |
| `PATCH /v0/admin/integrations/backends/{backend_id}` | Unify SDK, Console admin client tests | Keep |
| `POST /v0/admin/integrations/sync` | Orchestra cloud bootstrap, Unity Deploy catalog publish, Unify SDK | Keep |
| `GET/PUT /v0/admin/integrations/bootstrap-state` | Orchestra cloud bootstrap | Keep; detailed desired-state diagnostics |

## Removed Read Routes

The redundant POST read routes were removed in favor of GET query-parameter
contracts:

| Removed endpoint | Replacement |
| --- | --- |
| `POST /v0/integrations/apps/get` | `GET /v0/integrations/apps` |
| `POST /v0/integrations/apps/search` | `GET /v0/integrations/apps/search` |
| `POST /v0/integrations/tools/get` | `GET /v0/integrations/tools` |
| `POST /v0/integrations/tools/search` | `GET /v0/integrations/tools/search` |

Mutation/action routes remain POST/PATCH/PUT because they change state or invoke
provider work: connect, complete, disconnect, cancel, reconnect, health test,
tool run, admin sync, backend upsert/patch, and bootstrap-state updates.
