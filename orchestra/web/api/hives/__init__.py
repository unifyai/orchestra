"""Hive API.

Submodules are imported lazily by their consumers to keep this package free
of load-order coupling with peers (notably ``assistant`` whose schema module
references :class:`HiveSummary` at import time).
"""
