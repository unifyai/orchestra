import importlib
from pathlib import Path

import pytest


def test_kernel_models_are_core_models():
    from orchestra.db.models import orchestra_models
    from orchestra_core.db.models import core_models

    assert orchestra_models.Project is core_models.Project
    assert orchestra_models.Context is core_models.Context
    assert orchestra_models.LogEvent is core_models.LogEvent
    assert orchestra_models.FieldType is core_models.FieldType


@pytest.mark.parametrize(
    "module_name",
    [
        "orchestra.db.dao.log_event_dao",
        "orchestra.web.api.log.schema",
        "orchestra.web.api.log.python2SQL",
        "orchestra.web.api.log.utils",
    ],
)
def test_platform_kernel_forks_are_not_importable(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_platform_kernel_fork_paths_do_not_exist():
    root = Path(__file__).resolve().parents[3]

    assert not (root / "orchestra/db/dao/log_event_dao.py").exists()
    assert not (root / "orchestra/web/api/log/schema.py").exists()
    assert not (root / "orchestra/web/api/log/python2SQL").exists()
    assert not (root / "orchestra/web/api/log/utils").exists()
