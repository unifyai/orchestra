"""Database utilities package.

This package re-exports utilities from both the parent utils.py module
and the fk_path_parser submodule for backward compatibility.
"""

# Import and re-export from parent utils.py module (sibling file)
# We need to import it as a module from the parent db package
import importlib.util
from pathlib import Path

# Get path to the sibling utils.py file
utils_py_path = Path(__file__).parent.parent / "utils.py"

# Load the utils.py module
spec = importlib.util.spec_from_file_location(
    "orchestra.db.utils_module",
    utils_py_path,
)
utils_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils_module)

# Re-export functions from utils.py
create_database = utils_module.create_database
drop_database = utils_module.drop_database
get_next_order_value = utils_module.get_next_order_value

# Import FK path parser from submodule
from orchestra.db.utils.fk_path_parser import FKPathParser, PathSegment

__all__ = [
    "FKPathParser",
    "PathSegment",
    "create_database",
    "drop_database",
    "get_next_order_value",
]
