"""Background workers for Orchestra.

This package contains background workers that process queued tasks asynchronously,
decoupling slow operations from user-facing API requests.

    Available workers:
- embedding_generator: Stage 1 - Generates embedding vectors (parallel-safe)
- embedding_inserter: Stage 2 - Bulk inserts vectors into indexed table (serial)
    - index_maintenance: Maintains HNSW indexes for embedding table
"""
