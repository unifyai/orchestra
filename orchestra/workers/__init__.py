"""Background workers for Orchestra.

This package contains background workers that process queued tasks asynchronously,
decoupling slow operations from user-facing API requests.

    Available workers:
    - embedding_worker: Processes pending embeddings queue for embedding generation
    - index_maintenance: Maintains HNSW indexes for embedding table
"""
