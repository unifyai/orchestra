# Orchestra Endpoints

This directory holds the `schema` and `view` files for every endpoint under the orchestra API.

<!-- TOC tocDepth:2..3 chapterDepth:2..6 -->

- [How to add a new endpoint](#how-to-add-a-new-endpoint)
- [Async vs Sync Route Handlers](#async-vs-sync-route-handlers)
- [How to secure a endpoint](#how-to-secure-a-endpoint)
    - [User-Facing](#user-facing)
    - [Admin Authentication](#admin-authentication)

<!-- /TOC -->

## How to add a new endpoint

TODO

## Async vs Sync Route Handlers

FastAPI treats `async def` and `def` route handlers **fundamentally differently**, with real performance implications.

### The Rule

| Your Code Contains | Use | Why |
|-------------------|-----|-----|
| `await` calls (async I/O) | `async def` | Cooperates with event loop |
| No `await` calls (sync DB, CPU work) | `def` | Let FastAPI use threadpool |

### Why This Matters

**`async def` routes run directly on the event loop.** If your function contains no `await` calls, it blocks the entire event loop for its duration. No other requests can be processed until it completes.

**`def` routes are offloaded to a threadpool.** FastAPI automatically runs sync functions in worker threads, keeping the event loop free for other async operations.

### Example: 10 concurrent requests to a 200ms sync operation

With `async def` (wrong):
```
Request 1: ████ (blocks event loop)
Request 2:     ████ (waits, then blocks)
Request 3:         ████
...
Total: ~2000ms (serialized)
```

With `def` (correct):
```
Thread 1: ████ (200ms)
Thread 2: ████ (parallel)
Thread 3: ████ (parallel)
...
Total: ~200ms (parallelized)
```

### Our Codebase

Most routes in orchestra use **sync SQLAlchemy sessions** and should be declared as `def`. Only use `async def` when you're actually awaiting async operations (e.g., async HTTP clients, async database drivers).

## How to secure a endpoint

### User-Facing

To enable user API key authentication on endpoints, you should add the following
in the `orchestra/web/api/router.py` file:

```python
api_router.include_router(
    ...,
    dependencies=API_KEY_AUTH,
)
```

For example, this will protect all endpoints in the `/dummy` router:
```python
api_router.include_router(
    dummy.router,
    prefix="/dummy",
    tags=["dummy"],
    dependencies=API_KEY_AUTH,
)
```

### Admin Authentication

To enable admin-only API key authentication on endpoints, you should add the following
in the `orchestra/web/api/router.py` file:

```python
api_router.include_router(
    ...,
    dependencies=ADMIN_AUTH,
)
```

For example, this will protect all endpoints in the `/dummy` router
to allow admin-only access:

```python
api_router.include_router(
    dummy.router,
    prefix="/dummy",
    tags=["dummy"],
    dependencies=ADMIN_AUTH,
)
```

For testing purposes, an example is to add `ORCHESTRA_ADMIN_KEY="testing-123"` to the `.env` file for verifying the behaviour of the admin key authentication.
