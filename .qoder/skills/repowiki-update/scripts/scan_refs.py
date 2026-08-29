# -*- coding: utf-8 -*-
"""Repo Wiki 过时引用扫描/修复/验证工具。

三种模式:
    python scan_refs.py              # 扫描: 输出引用缺失文件的清单 (默认)
    python scan_refs.py --fix        # 修复: 按内置映射自动替换过时引用
    python scan_refs.py --verify     # 验证: 零残留 + yaml 可解析, 退出码 0/1

被 repowiki-update 技能与 post-commit hook 共用。
"""
import argparse
import io
import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
LAN = os.path.join(REPO_ROOT, "lan_mesh")
RW = os.path.join(REPO_ROOT, ".qoder", "repowiki")

# ── 真实文件收集: 根目录 + lan_mesh + 前端相关目录 ──────────
_EXTRA_DIRS = [
    os.path.join(LAN, "web"),
    os.path.join(LAN, "web", "templates"),
    os.path.join(REPO_ROOT, "webui"),
    os.path.join(REPO_ROOT, "quicklan-main", "src"),
    os.path.join(REPO_ROOT, "quicklan-main", "src-tauri"),
    os.path.join(REPO_ROOT, "quicklan-main", "src-tauri", "src"),
]

# 引用提取: file://xxx / lan_mesh/xxx / `xxx`
REF_PAT = re.compile(
    r"(?:file://|lan_mesh/|`)([A-Za-z0-9_\-]+\.(?:py|html|yaml|yml|ts|rs|json|css|js))"
)

# 已删除文件 → 承接文件的映射规则 (API 文档语境 → station_api, 其余 → station_controller)
FILE_PAT = re.compile(r"(?:lan_mesh/)?(master|secretary)\.py(?:[#:]\d+(?:-\d+)?)?")

LABELS = [
    ("MasterController", "StationDirector"),
    ("Master 控制器", "Station Director"),
    ("Master控制器", "StationDirector"),
    ("Master 节点", "Station 节点"),
    ("Master/Worker", "Station/Worker"),
    ("master/worker", "station/worker"),
    ("SecretaryController", "StationDirector"),
    ("Secretary 控制器", "Station Director"),
    ("Secretary 节点", "Station 节点"),
    ("Secretary节点", "Station 节点"),
    ("SecretaryState", "WebSocket 状态"),
]

API_HINTS = ("API 参考手册", "Web API 接口")
WS_SPECIAL = "WebSocket 实时通信.md"


def collect_real_files() -> set:
    real = set(os.listdir(REPO_ROOT))
    real |= {f for f in os.listdir(LAN) if os.path.isfile(os.path.join(LAN, f))}
    for d in _EXTRA_DIRS:
        if os.path.isdir(d):
            real |= set(os.listdir(d))
    return real


def walk_rw_files():
    for dirpath, dirs, files in os.walk(RW):
        for f in files:
            if f.endswith(".md") or f.endswith(".yaml"):
                yield os.path.join(dirpath, f)


def scan_missing():
    """返回 [(文件, 行号, 行内容, 缺失文件名), ...]"""
    real = collect_real_files()
    missing = []
    for mf in walk_rw_files():
        text = io.open(mf, encoding="utf-8").read()
        for i, ln in enumerate(text.splitlines()):
            for m in REF_PAT.finditer(ln):
                name = m.group(1)
                if name.endswith(".py") and name not in real:
                    missing.append((mf, i + 1, ln.strip(), name))
    return missing


def _target_py(rel_path: str) -> str:
    for hint in API_HINTS:
        if hint in rel_path:
            return "station_api.py"
    if rel_path.endswith(WS_SPECIAL):
        return "station_api.py"
    return "station_controller.py"


def _fix_line(line: str, rel_path: str):
    """替换已删除文件引用与同行角色标签, 返回 (新行, 是否变更)。"""
    if "master.py" not in line and "secretary.py" not in line:
        return line, False
    new = FILE_PAT.sub(lambda m: _target_py(rel_path), line)
    for old, rep in LABELS:
        new = new.replace(old, rep)
    return new, new != line


def fix_all() -> int:
    """按映射修复全部文件, 返回修改行数。"""
    changed = 0
    for mf in walk_rw_files():
        rel = os.path.relpath(mf, REPO_ROOT)
        raw = io.open(mf, encoding="utf-8").read()
        nl = "\r\n" if "\r\n" in raw else "\n"
        lines = raw.splitlines()
        n = 0
        for i, ln in enumerate(lines):
            new_ln, hit = _fix_line(ln, rel)
            if hit:
                lines[i] = new_ln
                n += 1
        if n:
            io.open(mf, "w", encoding="utf-8", newline="").write(nl.join(lines) + nl)
            changed += n
    # 修复 file:// URL 丢失的 lan_mesh/ 前缀
    for mf in walk_rw_files():
        raw = io.open(mf, encoding="utf-8").read()
        if "file://station_" not in raw:
            continue
        new = raw.replace("file://station_controller.py", "file://lan_mesh/station_controller.py")
        new = new.replace("file://station_api.py", "file://lan_mesh/station_api.py")
        if new != raw:
            io.open(mf, "w", encoding="utf-8", newline="").write(new)
    return changed


def verify_all() -> int:
    """验证零残留 + yaml 可解析, 返回 0 通过 / 1 失败。"""
    ok = True
    missing = scan_missing()
    if missing:
        ok = False
        print("[FAIL] 残留缺失文件引用 %d 处:" % len(missing))
        for mf, ln_no, ln, name in missing:
            print("  %s L%d: %s" % (os.path.relpath(mf, RW), ln_no, ln[:100]))
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        for mf in walk_rw_files():
            if not mf.endswith(".yaml"):
                continue
            try:
                yaml.safe_load(io.open(mf, encoding="utf-8").read())
            except Exception as e:
                ok = False
                print("[FAIL] YAML 解析失败: %s: %s" % (os.path.relpath(mf, RW), e))
    if ok:
        print("[PASS] 零残留引用, _module.yaml 全部可解析")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Repo Wiki 过时引用扫描/修复/验证")
    parser.add_argument("--fix", action="store_true", help="按映射规则自动修复")
    parser.add_argument("--verify", action="store_true", help="验证零残留 + yaml 可解析")
    args = parser.parse_args()

    if args.verify:
        return verify_all()

    missing = scan_missing()
    if not missing:
        print("[PASS] 无过时引用, wiki 与当前代码一致")
        return 0

    print("[FOUND] 引用缺失文件 %d 处:" % len(missing))
    for mf, ln_no, ln, name in missing:
        print("  %s L%d: %s" % (os.path.relpath(mf, RW), ln_no, ln[:100]))

    if args.fix:
        n = fix_all()
        print("[FIXED] 已修复 %d 行" % n)
        return verify_all()
    return 1


if __name__ == "__main__":
    sys.exit(main())
