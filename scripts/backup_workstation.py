"""Create, verify or restore an offline workstation backup without overwriting live data."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lan_mesh.backup import EXCLUDED_STATE, create_backup, restore_backup, verify_backup  # noqa: E402
from lan_mesh.config import AppConfig, get_db_path, load_config  # noqa: E402


def backup_roots(args) -> dict[str, Path]:
    state = Path(args.state_dir).expanduser().absolute()
    config_candidates = [Path(p).expanduser() for p in (args.config, os.environ.get("LAN_MESH_CONFIG")) if p]
    config_candidates += [state / "config.yaml", ROOT / "config.yaml"]
    if args.config and not Path(args.config).expanduser().is_file():
        raise ValueError("指定配置文件不存在")
    config = next((p for p in config_candidates if p.is_file()), None)
    cfg = load_config(str(config)) if config else AppConfig()
    db = Path(args.db).expanduser().absolute() if args.db else get_db_path(cfg).absolute()
    shared = Path(args.shared or cfg.secretary.shared_folder).expanduser().absolute()
    worker_shared = Path(args.worker_shared or args.shared or cfg.worker.shared_folder).expanduser().absolute()
    roots = {}
    def covered(path):
        path = path.absolute()
        for label, root in roots.items():
            if root == path:
                return True
            if root in path.parents:
                if label == "state" and path.relative_to(root).parts[0] in EXCLUDED_STATE:
                    continue
                return True
        return False
    # An explicitly selected missing data source is an error, not an empty backup.
    for label, path, explicit in (("state", state, True), ("shared", shared, bool(args.shared)),
                                  ("worker_shared", worker_shared, bool(args.worker_shared))):
        if path.exists() and not (label == "worker_shared" and path == shared):
            roots[label] = path
        elif explicit and not path.exists():
            raise ValueError(f"备份目录不存在: {path}")
    if args.db and not db.is_file():
        raise ValueError("指定主库不存在")
    for label, path in (("database", db), ("execution_receipts", db.with_name(db.stem + "-executions.sqlite3")),
                        ("pm_start_receipts", db.with_name(db.stem + "-pm-starts.sqlite3"))):
        if path.exists() and not covered(path):
            roots[label] = path
    configs = [config, ROOT / ".env", ROOT / "lan_mesh" / "resources.yaml", ROOT / "lan_mesh" / "model_pool.yaml",
               ROOT / "model_pool.yaml"]
    if os.environ.get("LAN_MESH_MODEL_POOL"):
        configs.append(Path(os.environ["LAN_MESH_MODEL_POOL"]).expanduser())
    configs.extend(Path(path).expanduser() for path in args.extra_file)
    for index, path in enumerate(configs):
        if path is not None and path.is_file() and not covered(path):
            roots[f"config_{index}"] = path.absolute()
    for path in args.extra_file:
        if not Path(path).expanduser().is_file():
            raise ValueError(f"额外配置文件不存在: {path}")
    return roots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="停止所有写入后创建备份")
    create.add_argument("archive", type=Path)
    create.add_argument("--config")
    create.add_argument("--db")
    create.add_argument("--shared")
    create.add_argument("--worker-shared")
    create.add_argument("--state-dir", default=str(Path.home() / ".lan_mesh"))
    create.add_argument("--extra-file", action="append", default=[])
    create.add_argument("--offline-confirmed", action="store_true", help="确认所有节点和外部工具已停止写入这些数据")
    create.add_argument("--plan", action="store_true", help="仅列出将备份的路径，不读取文件内容或创建备份")
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    restore = commands.add_parser("restore", help="校验后恢复到新目录，不覆盖原数据")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            roots = backup_roots(args)
            for label, path in roots.items():
                print(f"{label}: {path}")
            print("排除 state 中的 station.lock、backups 和 logs；归档可能包含密钥")
            if args.plan:
                return 0
            manifest = create_backup(roots, args.archive, offline_confirmed=args.offline_confirmed)
        elif args.command == "verify":
            manifest = verify_backup(args.archive)
        else:
            manifest = restore_backup(args.archive, args.destination)
        print(f"PASS: {args.command}; {len(manifest['files'])} files; format {manifest['format']}")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
