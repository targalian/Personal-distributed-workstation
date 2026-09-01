# -*- coding: utf-8 -*-
"""静态扫描 lan_mesh/ 中「被引用但从未绑定」的全局名。

补的是 py_compile 的盲区: 语法检查不做名字解析, 因此
「方法搬去新模块后 import 被误剪」这类缺陷能一路过编译, 若用例
恰好不覆盖该运行路径 (如不真起 uvicorn 服务器), pytest 也放行。
iter-75 拆分正是这样让 station_lifecycle.start() 漏掉 import uvicorn。

用法:
    python scripts/check_unbound_names.py          # 全库扫描
    python scripts/check_unbound_names.py a.py b.py  # 只扫指定文件
退出码: 0 = 干净, 1 = 发现未绑定引用。
"""
import ast
import builtins
import pathlib
import sys
from typing import List, Set, Tuple

MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                  "__spec__", "__loader__", "__builtins__"}


def collect_bound(tree: ast.Module) -> Set[str]:
    """收集模块内所有会绑定名字的位置 (import/def/class/赋值/参数等)。"""
    bound: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
    return bound


def scan_file(path: pathlib.Path) -> List[Tuple[int, str]]:
    """返回该文件内未绑定引用的 (行号, 名字), 每个名字只报首次出现。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    allowed = collect_bound(tree) | set(dir(builtins)) | MODULE_DUNDERS
    first: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in allowed and node.id not in first:
                first[node.id] = node.lineno
    return sorted(((line, name) for name, line in first.items()))


def main(argv: List[str]) -> int:
    if argv:
        targets = [pathlib.Path(a) for a in argv]
    else:
        targets = sorted(pathlib.Path("lan_mesh").rglob("*.py"))

    total = 0
    for path in targets:
        if not path.exists():
            print("[Preflight] 跳过 (不存在): %s" % path)
            continue
        for line, name in scan_file(path):
            print("[Preflight] %s:%d 引用未绑定的名字: %s" % (path, line, name))
            total += 1

    if total:
        print("[Preflight] FAIL — 发现 %d 处未绑定引用 "
              "(多为 import 被误删, 会在运行时 NameError)" % total)
        return 1
    print("[Preflight] PASS — %d 个文件无未绑定引用" % len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
