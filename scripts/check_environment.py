"""Validate startup dependencies and imports without contacting a package index."""
import argparse
import importlib
import importlib.metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORTS = (
    "fastapi", "uvicorn", "websockets.sync.client", "psutil", "pydantic", "yaml",
    "requests", "multipart", "dotenv", "boto3", "cryptography.hazmat.primitives.ciphers.aead",
)


def requirements(path: Path) -> list[str]:
    """Read the local requirements files, including their relative -r references."""
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.extend(requirements(path.parent / line[3:].strip()))
        else:
            result.append(line)
    return result


def check_environment(dev: bool = False) -> list[str]:
    """Return actionable errors for missing, incompatible or broken dependencies."""
    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError:
        return ["pip is unavailable; run this interpreter with -m ensurepip --upgrade"]
    errors = []
    source = ROOT / ("requirements-dev.txt" if dev else "requirements.txt")
    for raw in requirements(source):
        requirement = Requirement(raw)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"Missing dependency: {requirement}")
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            errors.append(f"Incompatible dependency: {requirement.name} {installed}; need {requirement.specifier}")
    if not errors:
        for name in IMPORTS + (("pytest", "httpx") if dev else ()):
            try:
                importlib.import_module(name)
            except Exception as exc:
                errors.append(f"Cannot import {name}: {type(exc).__name__}: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also check development dependencies")
    args = parser.parse_args()
    errors = check_environment(args.dev)
    for error in errors:
        print(f"[Preflight] {error}")
    if not errors:
        print("[Preflight] Dependency versions and imports passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
