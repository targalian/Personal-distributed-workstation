"""Check every declared runtime requirement offline before launching the workstation."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

from pip._vendor.packaging.requirements import Requirement


def check_requirements(path: Path) -> list[str]:
    """Return missing or incompatible distributions, respecting environment markers."""
    problems = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            problems.append(f'{requirement.name}: not installed')
            continue
        if installed not in requirement.specifier:
            problems.append(f'{requirement.name}: installed {installed}, requires {requirement.specifier}')
        for dependency in requirement.extras:
            # uvicorn[standard] brings optional runtime modules used by the server.
            if requirement.name.casefold() == 'uvicorn' and dependency == 'standard':
                for name in ('httptools', 'watchfiles', 'websockets', 'PyYAML', 'python-dotenv'):
                    try:
                        version(name)
                    except PackageNotFoundError:
                        problems.append(f'{name}: required by uvicorn[standard]')
    return problems


def main() -> int:
    """Report only dependency names and versions, without importing user configuration."""
    path = Path(__file__).resolve().parent.parent / 'requirements.txt'
    try:
        problems = check_requirements(path)
    except (OSError, ValueError) as exc:
        print(f'[Preflight] Dependency check failed: {exc}')
        return 1
    for problem in problems:
        print(f'[Preflight] {problem}')
    if problems:
        return 1
    print('[Preflight] All runtime requirements are installed and compatible')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
