"""Adapter contract and conservative plain-output implementation.

Preparation accepts an already filtered environment from the caller. It neither
reads credentials nor runs commands; runtime environment policy stays in runtime.
"""

import shutil
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Iterator, Protocol

from .events import HarnessEvent, HarnessOutcome
from .spec import HarnessSpec


class HarnessAdapter(Protocol):
    """Interface implemented by registered harness adapters."""

    spec: HarnessSpec

    def stream(self, request: dict) -> Iterator[HarnessEvent]: ...

    def detect(self) -> bool: ...

    def prepare(self, request: dict) -> dict: ...

    def parse(self, stdout: str, stderr: str, returncode: int) -> HarnessOutcome: ...

    def classify_error(self, returncode: int, stderr: str, outcome: HarnessOutcome) -> str: ...


@dataclass
class LegacyAdapter:
    """Wrap an existing command template without assuming structured telemetry."""

    spec: HarnessSpec

    def detect(self) -> bool:
        """Check executable availability without launching the CLI."""
        return shutil.which(self.spec.detect) is not None

    def prepare(self, request: dict) -> dict:
        """Build argv and copy the caller's filtered environment."""
        cwd = request["cwd"]
        return {
            "argv": self.spec.build_cmd(request["prompt"], cwd),
            "env": dict(request.get("env", {})),
            "cwd": cwd,
        }

    def parse(self, stdout: str, stderr: str, returncode: int) -> HarnessOutcome:
        """Preserve raw output and mark unavailable telemetry explicitly."""
        outcome = HarnessOutcome(
            status="completed" if returncode == 0 else "failed",
            result=stdout,
            usage={"input_tokens": 0, "output_tokens": 0},
            usage_known=False,
            cost_usd=0.0,
            cost_estimated=True,
            session_id="",
            degraded=["usage_unavailable", "cost_unavailable", "elapsed_unavailable"],
            error_class="",
            elapsed_s=0.0,
        )
        if returncode != 0:
            outcome.error_class = self.classify_error(returncode, stderr, outcome)
        return outcome

    def classify_error(self, returncode: int, stderr: str, outcome: HarnessOutcome) -> str:
        """Classify CLI failures using stable exit-code and diagnostic signals."""
        message = stderr.lower()
        if returncode == 127 or "not found" in message:
            return "not_installed"
        if any(term in message for term in ("usage limit", "rate limit")):
            return "usage_limit"
        if any(term in message for term in ("auth", "login", "401")):
            return "auth"
        if any(term in message for term in (
            "connectionerror", "connection error", "connection refused",
            "connection reset", "network", "timed out", "timeout", "dns",
        )):
            return "network"
        if "sandbox" in message:
            return "sandbox"
        return "unknown"


def _number(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _consume(line: str, outcome: HarnessOutcome, state: dict) -> tuple[str, dict]:
    """Normalize known JSONL records, retaining unrecognized output as text."""
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        outcome.result += line
        return 'assistant', {'text': line}
    kind = data.get('type', data.get('kind', ''))
    state['model'] = data.get('model', state.get('model', ''))
    outcome.session_id = data.get('session_id', data.get('thread_id', outcome.session_id))
    message = data.get('message', {})
    usage = data.get('usage', message.get('usage', {}) if isinstance(message, dict) else {})
    if isinstance(usage, dict):
        for target, alias in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')]:
            value = _number(usage.get(target, usage.get(alias)))
            if value is not None:
                outcome.usage[target] = int(value)
                state.setdefault('usage_fields', set()).add(target)
        outcome.usage_known = len(state.get('usage_fields', ())) == 2
    cost = _number(data.get('cost_usd', data.get('total_cost_usd')))
    if cost is not None:
        outcome.cost_usd, outcome.cost_estimated = cost, False
        state['usd'] = True
    text = data.get('text', '')
    if kind == 'assistant' and isinstance(message, dict) and 'content' in message:
        content = message.get('content', [])
        text = content if isinstance(content, str) else ''.join(
            item.get('text', '') for item in content if isinstance(item, dict))
        state['model'] = message.get('model', state.get('model', ''))
    item = data.get('item', {})
    if isinstance(item, dict) and item.get('type') == 'agent_message':
        text = item.get('text', '')
    if kind in ('error', 'turn.failed', 'run.failed'):
        outcome.status = 'failed'
        state['diagnostic'] = str(data.get('error', data.get('message', data)))
    if kind in ('result', 'run.completed'):
        if isinstance(data.get('result'), str):
            outcome.result = data['result']
        if data.get('is_error') or data.get('status') == 'failed':
            outcome.status = 'failed'
        return 'telemetry', data
    if text:
        outcome.result += str(text)
        return 'assistant', {'text': text}
    if usage:
        return 'usage', data
    if kind in ('system', 'thread.started', 'turn.started', 'run.started'):
        return 'telemetry', data
    outcome.result += line
    return 'assistant', {'text': line}


def _estimate_cost(outcome: HarnessOutcome, state: dict) -> None:
    if not outcome.cost_estimated or not outcome.usage_known or not state.get('model'):
        return
    try:
        from ..config import load_model_pool
        if 'prices' not in state:
            state['prices'] = load_model_pool().models
        for entry in state['prices']:
            if state['model'] in (entry.id, entry.model_name):
                prices = (entry.cost_input_per_1k, entry.cost_output_per_1k)
                if any(prices):
                    outcome.cost_usd = sum(outcome.usage[key] * price / 1000 for key, price in
                                           zip(('input_tokens', 'output_tokens'), prices))
                    state['usd'] = True
                break
    except Exception:
        pass


def _windows_job(process: subprocess.Popen) -> object:
    """Attach children to a job so cleanup also works when taskkill is restricted."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if handle and kernel.AssignProcessToJobObject(handle, int(process._handle)):
        return kernel, handle
    if handle:
        kernel.CloseHandle(handle)
    return None


def _stop_tree(process: subprocess.Popen, job: object = None) -> None:
    """Terminate the isolated child process tree and reap the direct child."""
    try:
        if os.name == 'nt' and process.poll() is None:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                           capture_output=True, timeout=10)
        elif os.name != 'nt':
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if job is not None:
        kernel, handle = job
        kernel.TerminateJobObject(handle, 1)
        kernel.CloseHandle(handle)
    try:
        if process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def _read_pipe(pipe: object, channel: str, pending: queue.Queue, limit: int,
               stopped: threading.Event) -> None:
    try:
        while True:
            line = pipe.readline(limit)
            if not line:
                break
            while not stopped.is_set():
                try:
                    pending.put((channel, line), timeout=0.05)
                    break
                except queue.Full:
                    continue
            if stopped.is_set():
                break
    finally:
        pipe.close()
        while not stopped.is_set():
            try:
                pending.put((channel, None), timeout=0.05)
                break
            except queue.Full:
                continue


@dataclass
class StreamingAdapter(LegacyAdapter):
    """Run an isolated process with concurrent pipe draining and cooperative cancellation."""

    def stream(self, request: dict) -> Iterator[HarnessEvent]:
        started, seq = time.monotonic(), 0
        outcome = self.parse('', '', 0)
        outcome.degraded = []
        state, stderr, process, job = {}, '', None, None
        stopped, readers = threading.Event(), []
        yield HarnessEvent('run.started', seq, time.time(), {'backend': self.spec.name})
        try:
            cancel = request.get('cancel_event')
            if cancel is not None and cancel.is_set():
                outcome.status = 'cancelled'
            else:
                prepared = request if 'argv' in request else self.prepare(request)
                options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
                           else {'start_new_session': True})
                process = subprocess.Popen(prepared['argv'], cwd=prepared['cwd'], env=prepared['env'],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           encoding='utf-8', errors='replace', **options)
                job = _windows_job(process)
                pending, readers = queue.Queue(maxsize=128), []
                for channel in ('stdout', 'stderr'):
                    reader = threading.Thread(target=_read_pipe, args=(getattr(process, channel),
                                              channel, pending, 65536, stopped), daemon=True)
                    reader.start()
                    readers.append(reader)
                yield from self._drain(process, pending, request, outcome, state, started)
                stderr = state.get('stderr', '')
        except FileNotFoundError as exc:
            outcome.status, outcome.error_class, stderr = 'failed', 'not_installed', str(exc)
        except Exception as exc:
            outcome.status, outcome.error_class, stderr = 'failed', 'unknown', str(exc)
        finally:
            stopped.set()
            if process is not None:
                _stop_tree(process, job)
                for reader in readers:
                    reader.join(timeout=1)
        _estimate_cost(outcome, state)
        if not outcome.usage_known:
            outcome.degraded.append('usage')
        if not state.get('usd'):
            outcome.degraded.append('usd')
            if request.get('budget_usd') is not None:
                outcome.degraded.append('budget')
        outcome.elapsed_s = time.monotonic() - started
        code = process.returncode if process is not None else 127
        if outcome.status == 'completed' and code != 0:
            outcome.status = 'failed'
        if outcome.status == 'failed' and not outcome.error_class:
            outcome.error_class = self.classify_error(code, stderr or state.get('diagnostic') or outcome.result, outcome)
        data = asdict(outcome)
        data.update(returncode=code, stderr=stderr, elapsed_secs=round(outcome.elapsed_s, 1))
        if outcome.status != 'completed':
            data['error'] = stderr or outcome.error_class or outcome.status
        yield HarnessEvent('run.completed', state.get('seq', 0) + 1, time.time(), data)

    def _drain(self, process: subprocess.Popen, pending: queue.Queue, request: dict,
               outcome: HarnessOutcome, state: dict, started: float) -> Iterator[HarnessEvent]:
        closed, size = 0, 0
        limit = request.get('max_output', 100000)
        while closed < 2 or process.poll() is None:
            cancel = request.get('cancel_event')
            if cancel is not None and cancel.is_set():
                outcome.status = 'cancelled'
                break
            if time.monotonic() - started >= request.get('timeout', self.spec.timeout):
                outcome.status, outcome.error_class = 'timeout', 'timeout'
                break
            try:
                channel, line = pending.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                closed += 1
                continue
            if channel == 'stderr':
                state['stderr'] = (state.get('stderr', '') + line)[:limit // 4]
                continue
            kind, data = _consume(line, outcome, state)
            outcome.result = outcome.result[:limit]
            _estimate_cost(outcome, state)
            budget = _number(request.get('budget_usd'))
            if budget is not None and state.get('usd') and outcome.cost_usd > budget:
                outcome.status, outcome.error_class = 'failed', 'budget_exceeded'
                break
            if size < limit:
                state['seq'] = state.get('seq', 0) + 1
                yield HarnessEvent(kind, state['seq'], time.time(), data)
            size += len(line)
