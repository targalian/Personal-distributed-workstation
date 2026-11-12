"""Run dependency-editor behavior checks with the available Node runtime."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_dag_editor_dependency_and_draft_invariants() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for DAG frontend behavior checks')
    script = Path(__file__).parent / 'frontend' / 'dag_model.test.cjs'
    result = subprocess.run([node, '--test-reporter=tap', str(script)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'secretary refresh broadcasts the requested task identity' in result.stdout
