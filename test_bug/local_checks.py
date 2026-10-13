"""Local development loop: syntax, unit tests, UI and docs checks without a Station."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def check_syntax(root: Path) -> dict:
    """Compile project Python sources without importing them or touching runtime data."""
    files = [root / "main.py"]
    for folder in ("lan_mesh", "scripts", "tests", "test_bug"):
        files.extend(sorted((root / folder).rglob("*.py")))
    errors = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8-sig")
            compile(source, str(path), "exec")
        except (OSError, SyntaxError) as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")
    return {"name": "python_syntax", "status": "failed" if errors else "passed",
            "output": "\n".join(errors) if errors else f"Compiled {len(files)} Python files"}


def check_loop_state(root: Path) -> dict:
    """Reject unreadable loop history before it can break status or planning commands."""
    try:
        state = json.loads((root / "loop_status.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("loop_status.json must contain an object")
    except (OSError, ValueError) as exc:
        return {"name": "loop_state", "status": "failed", "output": str(exc)}
    return {"name": "loop_state", "status": "passed", "output": "Loop history is valid JSON"}


def check_command(name: str, command: list[str], root: Path, timeout: float) -> dict:
    """Run one bounded check using an isolated child environment and retain its output."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        status = "passed" if result.returncode == 0 else "failed"
        output = result.stdout + result.stderr
        code = result.returncode
    except subprocess.TimeoutExpired:
        status, output, code = "failed", f"Check exceeded {timeout:g} seconds", None
    except OSError as exc:
        status, output, code = "failed", str(exc), None
    return {"name": name, "command": command, "status": status, "returncode": code,
            "seconds": round(time.monotonic() - started, 3), "output": output[-20000:]}


def run_local_checks(root: Path = PROJECT_ROOT, timeout: float = 300,
                     report_path: Path | None = None) -> bool:
    """Execute one local verification cycle; missing optional tools are reported as skipped."""
    checks = [check_syntax(root), check_loop_state(root)]
    python = sys.executable
    commands = [
        ("unit_tests", [python, "-m", "pytest", "tests", "-q"]),
        ("ui_static", [python, "test_bug/ui_tests.py"]),
        ("design_docs", [python, "scripts/sync_docs.py"]),
    ]
    if shutil.which("bash"):
        commands.append(("shell_syntax", [shutil.which("bash"), "-n", "scripts/start_workstation.sh"]))
    else:
        checks.append({"name": "shell_syntax", "status": "skipped", "output": "bash is not installed"})
    for name, command in commands:
        print(f"[Loop] Running {name}...", flush=True)
        checks.append(check_command(name, command, root, timeout))
    node = shutil.which("node")
    if node:
        with tempfile.TemporaryDirectory(prefix="lan-mesh-js-") as directory:
            html = (root / "lan_mesh/web/templates/dashboard.html").read_text(encoding="utf-8")
            scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
            inline = Path(directory) / "dashboard.js"
            inline.write_text("\n".join(scripts), encoding="utf-8")
            checks.append(check_command("dashboard_js", [node, "--check", str(inline)], root, timeout))
        for path in sorted((root / "lan_mesh/web/static").glob("*.js")):
            checks.append(check_command(path.name, [node, "--check", str(path)], root, timeout))
    else:
        checks.append({"name": "javascript_syntax", "status": "skipped", "output": "Node.js is not installed"})
    success = all(check["status"] != "failed" for check in checks)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "python": python,
              "scope": "local; no live API, LLM, browser or remote-host tests",
              "status": "passed" if success else "failed", "checks": checks}
    target = report_path or root / ".lan_mesh/local-checks.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for check in checks:
        print(f"[Loop] {check['status'].upper()}: {check['name']}")
        if check["status"] != "passed":
            print(check["output"])
    print(f"[Loop] Report: {target}")
    return success


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300, help="timeout per check in seconds")
    parser.add_argument("--report", type=Path, help="JSON report path")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return 0 if run_local_checks(timeout=args.timeout, report_path=args.report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
