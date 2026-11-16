"""Explicit private runtime configuration; credentials never enter API responses."""
import json
from pathlib import Path

def read_runtime(path):
    try:
        value=json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Private runtime configuration is unavailable') from None
    if not isinstance(value,dict) or not isinstance(value.get('database_url'),str):
        raise ValueError('Private runtime configuration requires database_url')
    return value
