"""Shared constants for Orchestra context-name conventions.

Centralizes the few path-shape literals that multiple modules have to agree on
when classifying a context by name. Today the only entry is
:data:`HIVE_CONTEXT_PREFIX`; additional constants land here when a second
consumer forces the decision.

A context name starting with :data:`HIVE_CONTEXT_PREFIX` denotes a Hive-scoped
tree (``Hives/{hive_id}/...``) whose rows belong to a Hive entity rather than
a single ``{user_id}/{assistant_id}/...`` body. Call sites that branch on this
prefix — task-machine classifiers, sibling-context cleanup, the atomic-upsert
``All/*`` mirror — all import the constant from here so there is exactly one
literal definition of the Hive naming contract in the codebase.
"""

HIVE_CONTEXT_PREFIX = "Hives/"
