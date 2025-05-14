import json


def format_default(value):
    """Return a JS-literal string for any Python value (bool, number, str, list, dict)."""
    return json.dumps(value)
