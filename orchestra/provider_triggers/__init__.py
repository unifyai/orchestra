"""Provider-event trigger contracts and supporting primitives.

Owns identity extraction, live provider probes, revision/acceptance fences,
private event blob storage, the curated trigger registry, Composio trigger
adapter, and the cross-service dispatch envelope used by Orchestra when
authorizing Communication or Unity to execute a provider event.

Run-source vocabulary is canonical in
``unify.task_scheduler.types.run_source.RunSource``.
"""
