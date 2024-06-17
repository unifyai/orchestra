# Orchestra Endpoints

This directory holds the `schema` and `view` files for every endpoint under the orchestra API.

<!-- TOC tocDepth:2..3 chapterDepth:2..6 -->

- [How to add a new endpoint](#how-to-add-a-new-endpoint)
- [How to secure a endpoint](#how-to-secure-a-endpoint)
    - [User-Facing](#user-facing)
    - [Admin Authentication](#admin-authentication)

<!-- /TOC -->

## How to add a new endpoint

TODO

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
