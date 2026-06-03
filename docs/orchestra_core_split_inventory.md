# Orchestra Core Split Inventory

## Core-Owned Paths

These concerns are owned by `orchestra-core` and platform must import them from
`orchestra_core`:

- Kernel ORM models: `orchestra_core/db/models/core_models.py`
- Kernel DAOs: `context_dao.py`, `field_type_dao.py`, `log_event_dao.py`,
  `project_dao.py`, `embedding_dao.py`, `unique_constraint_dao.py`
- Log schemas: `orchestra_core/web/api/log/schema.py`
- Log query machinery: `orchestra_core/web/api/log/python2SQL/`
- Log utility modules: `orchestra_core/web/api/log/utils/`
- Kernel project/context/log/storage routers in `orchestra_core/web/api/`

## Platform-Owned Paths

These concerns remain private platform code:

- Tenant/account/billing/assistant models in `orchestra/db/models/orchestra_models.py`
- Tenant access DAOs, including `organization_member_dao.py`,
  `resource_access_dao.py`, and the platform `ProjectDAO` subclass
- Hosted-product routers under `orchestra/web/api/assistant`,
  `billing`, `organization`, `space`, `teams`, `users`, and integrations
- Task-machine admin endpoints and schemas under `orchestra/web/api/log/`
- Hosted storage implementation in `orchestra/services/bucket_service.py`

## Deleted Platform Forks

The following platform paths duplicated kernel code and must not be restored:

- `orchestra/db/dao/log_event_dao.py`
- `orchestra/web/api/log/schema.py`
- `orchestra/web/api/log/python2SQL/`
- `orchestra/web/api/log/utils/`

`scripts/check_no_kernel_forks.sh` enforces this list in CI.
