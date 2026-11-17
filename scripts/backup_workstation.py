"""Create, verify or restore an offline workstation backup without overwriting live data."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lan_mesh.backup import create_backup, restore_backup, verify_backup  # noqa: E402
from lan_mesh.config import AppConfig, _expand, get_db_path, get_shared_folder, load_config  # noqa: E402


def backup_roots(args: argparse.Namespace) -> dict[str, Path]:
    """Select the live database, shared folder, configuration and node state."""
    config_candidates = [Path(p).expanduser() for p in (args.config, os.environ.get("LAN_MESH_CONFIG")) if p]
    config_candidates += [Path.home() / ".lan_mesh" / "config.yaml", Path("config.yaml")]
    if args.config and not Path(args.config).expanduser().is_file():
        raise ValueError("指定配置文件不存在")
    config = next((path for path in config_candidates if path.is_file()), None)
    cfg = load_config(str(config)) if config else AppConfig()
    db = Path(args.db).expanduser().absolute() if args.db else get_db_path(cfg).absolute()
    if not db.is_file():
        raise ValueError(f"主库不存在: {db}")
    role = getattr(args, "role", "secretary")
    if role not in {"secretary", "worker"}:
        raise ValueError(f"不支持的角色: {role}")
    role_config = cfg.secretary if role == "secretary" else cfg.worker
    shared = (Path(_expand(args.shared)) if args.shared else get_shared_folder(cfg, role)).absolute()
    roots = {"database": db}
    if shared.is_dir():
        roots["shared"] = shared
    elif args.shared or role_config.shared_folder != "~/lan_mesh_shared":
        raise ValueError(f"共享目录不存在: {shared}")
    else:
        print(f"[Station] WARNING: 默认共享目录不存在，跳过 shared: {shared}", file=sys.stderr)
    state = Path.home() / ".lan_mesh"
    if state.is_dir():
        roots["state"] = state.absolute()
    configs = [config, ROOT / ".env", ROOT / "lan_mesh" / "model_pool.yaml", ROOT / "model_pool.yaml",
               ROOT / "lan_mesh" / "resources.yaml"]
    configs.extend(Path(path).expanduser() for path in args.extra_file)
    for index, path in enumerate(configs):
        if path is not None and path.is_file() and all(path.absolute() != root for root in roots.values()):
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
    create.add_argument("--role", choices=("secretary", "worker"), default="secretary",
                        help="选择共享目录对应的角色（默认 secretary）")
    create.add_argument("--extra-file", action="append", default=[])
    create.add_argument("--offline-confirmed", action="store_true",
                        help="确认所有节点和外部工具已停止写入这些数据")
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
            print("归档可能包含密钥，请限制访问")
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
