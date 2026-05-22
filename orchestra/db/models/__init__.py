"""Platform model loader.

Imports the kernel models from `orchestra_core` first so they are registered
on the shared `Base.metadata`, then walks the platform model package to load
the rest. Both halves end up registered on the same `meta` and Alembic's
autogenerate sees the union.
"""

import pkgutil
from pathlib import Path


def load_all_models() -> None:
    """Load all models — kernel first, then platform."""
    import orchestra_core.db.models  # noqa: F401

    orchestra_core.db.models.load_all_models()

    package_dir = Path(__file__).resolve().parent
    modules = pkgutil.walk_packages(
        path=[str(package_dir)],
        prefix="orchestra.db.models.",
    )
    for module in modules:
        __import__(module.name)  # noqa: WPS421
