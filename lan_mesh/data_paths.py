"""Resolve workstation data storage without changing the user's home directory."""
import os
from pathlib import Path


def data_directory() -> Path:
    """Use an explicit absolute data root, or the existing per-user default."""
    configured = os.environ.get("LAN_MESH_DATA_DIR")
    if configured is None:
        return Path.home() / ".lan_mesh"
    path = Path(configured).expanduser()
    if not configured.strip() or not path.is_absolute():
        raise ValueError("LAN_MESH_DATA_DIR must be a non-empty absolute path")
    return path


def default_shared_directory() -> str:
    """Preserve the legacy shared folder unless a separate data root is selected."""
    return str(data_directory() / "shared") if "LAN_MESH_DATA_DIR" in os.environ else "~/lan_mesh_shared"
