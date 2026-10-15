"""Offline, verified backups of task data, conversations, configuration and execution receipts."""
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import psutil

FORMAT_VERSION = 1
MAX_BYTES = 10 * 1024**3
MAX_FILES = 100000
EXCLUDED_STATE = {"station.lock", "backups", "logs"}


def ensure_stopped() -> None:
    """Refuse known Station/Worker processes; callers must also stop external writers."""
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.pid == os.getpid():
            continue
        args = process.info.get("cmdline") or []
        for index, arg in enumerate(args[:3]):
            if Path(arg).name in ("main.py", "lan-mesh") and any(role in args[index + 1:] for role in ("station", "worker")):
                raise ValueError(f"工作站仍在运行 (PID {process.pid})；请先停止 Station/Worker 和外部写入")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _hash_stream(stream)


def _hash_stream(stream) -> str:
    value = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        value.update(chunk)
    return value.hexdigest()


def _sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def _database_check(path: Path) -> None:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError(f"SQLite 完整性检查失败: {path.name}")


def _safe_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return bool(parts) and not name.startswith("/") and not any(c in name for c in '\\:*?"<>|') and all(
        p not in (".", "..", "") and not p.endswith((".", " ")) and p.split(".")[0].upper() not in reserved
        and not any(ord(c) < 32 for c in p) for p in name.split("/"))


def inventory(roots: dict[str, Path]) -> dict[str, tuple[Path, str, int]]:
    """Hash selected trees, refusing links and nonregular files rather than silently omitting them."""
    found = {}
    if len({label.casefold() for label in roots}) != len(roots):
        raise ValueError("备份根名称不能仅大小写不同")
    for label, raw in roots.items():
        if not label.isidentifier() or label == "RESTORE_INCOMPLETE":
            raise ValueError("备份根名称必须为标识符")
        root = raw.absolute()
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            raise ValueError(f"不备份符号链接根目录: {label}")
        if not root.exists():
            raise ValueError(f"备份源不存在: {root}")
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        if root.is_file() and (_sqlite(root) or root.suffix in (".sqlite", ".sqlite3")):
            # Explicit --db roots must watch WAL changes too, even though the
            # WAL is merged into the snapshot rather than archived separately.
            paths += [sidecar for suffix in ("-wal", "-journal") if (sidecar := root.with_name(root.name + suffix)).exists()]
        for path in paths:
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            if label == "state" and relative.split("/")[0] in EXCLUDED_STATE:
                continue
            if path.is_symlink():
                raise ValueError(f"不备份符号链接: {label}/{relative}")
            if path.is_dir():
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(f"不是普通文件: {label}/{relative}")
            # SQLite readers update the shared-memory WAL index themselves.
            # It is neither durable data nor a meaningful external-write signal.
            if path.name.endswith("-shm"):
                base = path.with_name(path.name[:-4])
                if base.is_file() and _sqlite(base):
                    continue
            name = f"{label}/{relative}"
            if not _safe_name(name):
                raise ValueError(f"不支持的文件名: {name}")
            found[name] = (path, digest(path), path.stat().st_size)
    if len(found) > MAX_FILES or sum(item[2] for item in found.values()) > MAX_BYTES:
        raise ValueError("备份源超过 10 GiB 或 100000 文件限制")
    return found


def _directories(roots: dict[str, Path]) -> list[str]:
    names = []
    for label, root in roots.items():
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if label == "state" and relative.split("/")[0] in EXCLUDED_STATE:
                continue
            if path.is_dir() and not path.is_symlink():
                name = f"{label}/{relative}"
                if not _safe_name(name):
                    raise ValueError(f"不支持的目录名: {name}")
                names.append(name)
    return sorted(names)


def create_backup(roots: dict[str, Path], archive: Path, *, offline_confirmed: bool = False) -> dict:
    """Take stable file copies and SQLite snapshots, then publish a new archive exclusively."""
    if not offline_confirmed:
        raise ValueError("请先停止所有写入并使用 --offline-confirmed")
    ensure_stopped()
    archive = archive.absolute()
    for root in roots.values():
        resolved = root.resolve()
        if archive.resolve() == resolved or resolved in archive.resolve().parents:
            raise ValueError("备份文件不能位于备份源内部")
    before = inventory(roots)
    manifest = {"format": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "scope": "offline; all writers must be stopped", "files": [],
                "roots": {key: str(value.absolute()) for key, value in roots.items()},
                "root_types": {key: "file" if value.is_file() else "directory" for key, value in roots.items()},
                "directories": _directories(roots),
                "excluded_state": sorted(EXCLUDED_STATE)}
    with tempfile.TemporaryDirectory(prefix="lan-mesh-backup-") as directory:
        stage = Path(directory)
        sqlite_names = {name for name, (path, _, _) in before.items()
                        if _sqlite(path) or path.suffix in (".sqlite", ".sqlite3") or name.startswith("database/")}
        for name, (source, _, _) in before.items():
            if any(name == base + suffix for base in sqlite_names for suffix in ("-wal", "-shm")):
                continue
            if name.endswith("-journal"):
                raise ValueError("发现 SQLite journal，请核实数据库已正常停止")
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in sqlite_names:
                with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(str(target))) as dst:
                        src.backup(dst)
                _database_check(target)
            else:
                shutil.copyfile(source, target)
            if name not in sqlite_names and digest(target) != before[name][1]:
                raise ValueError("备份期间源文件发生变化")
            manifest["files"].append({"path": name, "sha256": digest(target), "size": target.stat().st_size,
                                      "executable": bool(source.stat().st_mode & 0o111),
                                      "kind": "sqlite" if name in sqlite_names else "file"})
        ensure_stopped()
        if before != inventory(roots) or manifest["directories"] != _directories(roots):
            raise ValueError("备份期间源文件发生变化，请停止所有写入后重试")
        # Build and verify privately before publishing, so failed validation never
        # leaves a plausible archive at the requested path.
        packed = stage / "bundle.zip"
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for item in manifest["files"]:
                bundle.write(stage / item["path"], item["path"])
        verify_backup(packed)
        descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output, packed.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
    return manifest


def verify_backup(archive: Path) -> dict:
    """Check the entire member set, safe names, size limits, hashes and SQLite integrity."""
    if archive.stat().st_size > MAX_BYTES:
        raise ValueError("归档体积超过 10 GiB")
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        names = [info.filename for info in infos]
        if (len(names) != len(set(names)) or len(names) > MAX_FILES + 1 or
                len({name.casefold() for name in names}) != len(names) or
                any(not _safe_name(name) for name in names) or
                any(stat.S_ISLNK(info.external_attr >> 16) or info.is_dir() for info in infos)):
            raise ValueError("归档包含重复路径、非法路径或链接")
        if sum(info.file_size for info in infos) > MAX_BYTES:
            raise ValueError("归档解压体积超过 10 GiB")
        if "manifest.json" not in names or bundle.getinfo("manifest.json").file_size > 32 * 1024**2:
            raise ValueError("缺少或过大的备份清单")
        manifest = json.loads(bundle.read("manifest.json"))
        if manifest.get("format") != FORMAT_VERSION or not isinstance(manifest.get("files"), list):
            raise ValueError("不支持的备份格式")
        if (not isinstance(manifest.get("roots"), dict) or not manifest["roots"] or
                any(not key.isidentifier() or not _safe_name(key) or key == "RESTORE_INCOMPLETE" for key in manifest["roots"]) or
                len({key.casefold() for key in manifest["roots"]}) != len(manifest["roots"]) or
                set(manifest.get("root_types", {})) != set(manifest["roots"]) or
                any(value not in ("file", "directory") for value in manifest["root_types"].values())):
            raise ValueError("备份根清单无效")
        items = manifest["files"]
        expected = [item["path"] for item in items]
        if len(expected) != len(set(expected)) or set(names) != {"manifest.json", *expected}:
            raise ValueError("归档文件与清单不一致")
        directories = manifest.get("directories", [])
        if (not isinstance(directories, list) or len(directories) > MAX_FILES or
                any(not _safe_name(name) or name.split("/")[0] not in manifest["roots"] for name in directories) or
                any(name in expected for name in directories)):
            raise ValueError("目录清单无效")
        file_set = set(expected)
        if any(str(parent) in file_set for name in [*expected, *directories] for parent in PurePosixPath(name).parents):
            raise ValueError("清单中存在文件与目录路径冲突")
        # Validate history references as well as archive hashes: an already
        # missing committed revision must not become a 'verified' backup.
        for item in items:
            name = item["path"]
            if name.endswith("/brief.json") and "project_briefs" in PurePosixPath(name).parts:
                if bundle.getinfo(name).file_size > 32 * 1024**2:
                    raise ValueError("项目描述文件超过 32 MiB")
                brief = json.loads(bundle.read(name))
                for entry in brief.get("_history", []):
                    filename = entry.get("file")
                    if filename and (not _safe_name(filename) or "/" in filename or
                                     str(PurePosixPath(name).parent / "revisions" / filename) not in expected):
                        raise ValueError(f"项目描述缺少已提交历史版本: {name}")
        with tempfile.TemporaryDirectory(prefix="lan-mesh-verify-") as directory:
            for index, item in enumerate(items):
                if (item["kind"] not in ("file", "sqlite") or len(item["path"].split("/")) < 2 or
                        item["path"].split("/")[0] not in manifest["roots"]):
                    raise ValueError("清单包含未知内容类型或根目录")
                target = Path(directory) / str(index)
                with bundle.open(item["path"]) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                if target.stat().st_size != item["size"] or digest(target) != item["sha256"]:
                    raise ValueError(f"文件校验失败: {item['path']}")
                if item["kind"] == "sqlite":
                    _database_check(target)
        return manifest


def restore_backup(archive: Path, destination: Path) -> dict:
    """Restore only into a newly created directory, leaving live data untouched."""
    # Pin the archive bytes for both verification and extraction (no path reopen race).
    with tempfile.TemporaryDirectory(prefix="lan-mesh-restore-") as directory:
        if archive.stat().st_size > MAX_BYTES:
            raise ValueError("归档体积超过 10 GiB")
        pinned = Path(directory) / "bundle.zip"
        shutil.copyfile(archive, pinned)
        manifest = verify_backup(pinned)
        destination = destination.absolute()
        if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
            raise ValueError("恢复目标不能经过符号链接")
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        marker = destination / "RESTORE_INCOMPLETE"
        marker.write_text("恢复未完成，不可切换使用\n", encoding="utf-8")
        for label in manifest["roots"]:
            (destination / label).mkdir(mode=0o700)
        for name in manifest.get("directories", []):
            (destination / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        with zipfile.ZipFile(pinned) as bundle:
            for item in manifest["files"]:
                path = destination / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with bundle.open(item["path"]) as source, path.open("xb") as output:
                    os.chmod(path, 0o700 if item.get("executable") else 0o600)
                    shutil.copyfileobj(source, output)
                if digest(path) != item["sha256"]:
                    raise ValueError("恢复后的文件校验失败，保留未完成标记")
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        mapping = "\n".join(f"- {label}: {destination / label} → 原路径 {original}" for label, original in manifest["roots"].items())
        (destination / "恢复说明.md").write_text(
            "# 恢复结果\n\n所有文件摘要已校验；SQLite 已通过完整性检查。\n\n" + mapping +
            "\n\n切换前停止 Station/Worker 和外部工具，另备份现有数据。按上表将各目录内容还原到原路径；"
            "单文件根还原该文件。主库、执行回执和启动回执必须一起恢复。不要复制旧的 WAL/SHM 覆盖快照。"
            "配置、密钥和节点身份需在原节点恢复；跨机器部署应另核对路径和节点身份。"
            "数据恢复不等于在途任务续跑，旧执行结果需核实。归档可能包含密钥，应限制访问。\n",
            encoding="utf-8")
        marker.unlink()
    return manifest
