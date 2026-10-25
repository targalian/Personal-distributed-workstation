"""Keep source-install requirements generated from the wheel dependency contract."""

import argparse
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Check the generated projection, or write it when explicitly requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    project = tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))
    content = '# Generated from pyproject.toml; update with python scripts/sync_dependencies.py --write\n'
    content += '\n'.join(project['project']['dependencies'])+'\n'
    target = ROOT/'requirements.txt'
    if args.write:
        target.write_text(content, encoding='utf-8')
    if target.read_text(encoding='utf-8') != content:
        print('[Preflight] requirements.txt differs from pyproject.toml')
        return 1
    print('[Preflight] Dependency manifests agree')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
