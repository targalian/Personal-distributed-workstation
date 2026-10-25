"""Product version from VERSION.json in a checkout, or wheel metadata after installation."""

import json
from importlib import metadata
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / 'VERSION.json'


def get_version() -> str:
    """Use the release manifest for source builds and the same built version in wheels."""
    try:
        value = json.loads(VERSION_FILE.read_text(encoding='utf-8-sig')).get('version')
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return metadata.version('lan-mesh')
    except metadata.PackageNotFoundError:
        return '0.0.0'


__version__ = get_version()
