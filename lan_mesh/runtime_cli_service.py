"""RuntimeCliService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import agent_runtime as policy
from .component_service import ComponentService

class RuntimeCliService(ComponentService):
    """RuntimeCliService business operations with explicit state ownership."""
    OPERATIONS = ('_handle_cli_agent', '_parse_cli_output', 'list_cli_agents')

    def _handle_cli_agent(self, input_data: dict) -> dict:
        """调用外部 CLI Agent 自主完成编码任务。

        与单轮 LLM API 调用不同, CLI Agent 能够:
        - 自主探索代码库、读取文件
        - 编辑多个文件、创建新文件
        - 运行命令、执行测试
        - 迭代修复错误直到任务完成

        支持后端: claude (Claude Code) / codex / aider
        通过环境变量 CLI_AGENT_BACKEND 指定, 或自动检测。

        input_data 字段:
            requirement/description: 任务描述
            cwd: 工作目录 (默认 shared_folder)
            backend: 强制指定后端 (可选)
            timeout: 超时秒数 (可选)
            allowed_tools: 允许的工具列表 (claude 专用, 可选)
        """
        requirement = input_data.get('requirement', input_data.get('description', ''))
        if not requirement:
            return {'error': '未提供任务描述 (requirement)'}
        cwd = input_data.get('cwd', self.owner.shared_folder)
        timeout = min(input_data.get('timeout', policy.CLI_AGENT_TIMEOUT), 1800)
        allowed_roots = [self.owner.shared_folder, *policy.CLI_AGENT_ALLOWED_ROOTS]
        reject = policy.validate_cli_agent_cwd(cwd, allowed_roots)
        if reject:
            policy.logger.warning('[CLI Agent] cwd 校验拒绝: %s', reject)
            return {'error': reject, 'cwd': cwd, 'status': 'failed'}
        backend, backend_err = policy.resolve_cli_backend(input_data.get('backend', ''))
        if backend_err:
            return backend_err
        cfg = policy.CLI_AGENT_BACKENDS[backend]
        policy.logger.info('[CLI Agent] 后端=%s, cwd=%s, timeout=%ds', backend, cwd, timeout)
        policy.logger.info('[CLI Agent] 任务: %s', requirement[:200])
        cmd = self._build_cli_command(cfg, backend, requirement, cwd, input_data)
        start_time = policy.time.time()
        try:
            result = policy.subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=policy._build_cli_env(backend))
            elapsed = policy.time.time() - start_time
            stdout = result.stdout[:policy.CLI_AGENT_MAX_OUTPUT]
            stderr = result.stderr[:policy.CLI_AGENT_MAX_OUTPUT // 4]
            output = self.owner._parse_cli_output(backend, stdout, stderr, result.returncode)
            output['backend'] = backend
            output['elapsed_secs'] = round(elapsed, 1)
            output['returncode'] = result.returncode
            if result.returncode == 0:
                policy.logger.info('[CLI Agent] 完成 (%.1fs, backend=%s)', elapsed, backend)
            else:
                policy.logger.warning('[CLI Agent] 非零退出 (code=%d, %.1fs)', result.returncode, elapsed)
            try:
                from . import runtime_trace
                runtime_trace.trace_llm_call(model=f'cli:{backend}', input_tokens=0, output_tokens=0, ttft_ms=0, total_ms=elapsed * 1000, status='ok' if result.returncode == 0 else 'error', call_type='cli')
            except Exception:
                pass
            return output
        except policy.subprocess.TimeoutExpired:
            elapsed = policy.time.time() - start_time
            policy.logger.error('[CLI Agent] 超时 (%ds, backend=%s)', timeout, backend)
            try:
                from . import runtime_trace
                runtime_trace.trace_llm_call(model=f'cli:{backend}', input_tokens=0, output_tokens=0, ttft_ms=0, total_ms=elapsed * 1000, status='timeout', error=f'超时 {timeout}s', call_type='cli')
            except Exception:
                pass
            return {'error': f'CLI Agent 执行超时 ({timeout}s)', 'backend': backend, 'elapsed_secs': round(elapsed, 1), 'status': 'timeout'}
        except FileNotFoundError:
            return {'error': f"CLI Agent '{backend}' 命令未找到, 请确认已安装并在 PATH 中", 'backend': backend}
        except Exception as e:
            policy.logger.error('[CLI Agent] 执行异常: %s', e)
            return {'error': str(e), 'backend': backend}

    def _build_cli_command(self, cfg: dict, backend: str, requirement: str, cwd: str, input_data: dict) -> list:
        """Compose backend arguments while retaining configured safety policy."""
        cmd = cfg['build_cmd'](requirement, cwd)
        resolved_exe = policy._which_with_fallback(cfg['detect'])
        if resolved_exe:
            cmd[0] = resolved_exe
        if backend == 'claude':
            allowed_tools = input_data.get('allowed_tools', [])
            if allowed_tools:
                for tool in allowed_tools:
                    cmd.insert(-1, f'--allowedTools={tool}')
            system_prompt = self.owner._build_system_prompt(requirement)
            if system_prompt and len(system_prompt) < 2000:
                cmd.insert(-1, f'--system-prompt={system_prompt[:1500]}')
        return cmd

    def _parse_cli_output(self, backend: str, stdout: str, stderr: str, returncode: int) -> dict:
        """解析 CLI Agent 的输出。

        Claude Code 使用 --output-format json 时输出 JSON 结构:
        {"type": "result", "result": "...", "cost_usd": 0.05, ...}
        """
        if backend == 'claude' and stdout.strip():
            try:
                data = policy._json.loads(stdout)
                if isinstance(data, dict):
                    return {'result': data.get('result', stdout), 'cost_usd': data.get('cost_usd', 0), 'duration_ms': data.get('duration_ms', 0), 'num_turns': data.get('num_turns', 0), 'session_id': data.get('session_id', ''), 'status': 'completed' if returncode == 0 else 'failed'}
            except policy._json.JSONDecodeError:
                pass
        return {'result': stdout if stdout else stderr, 'status': 'completed' if returncode == 0 else 'failed', 'stderr': stderr[:2000] if stderr else ''}

    def list_cli_agents(self) -> dict:
        """列出可用的 CLI Agent 后端及其状态。"""
        agents = []
        for name, cfg in policy.CLI_AGENT_BACKENDS.items():
            path = policy._which_with_fallback(cfg['detect'])
            installed = path is not None
            agents.append({'name': name, 'description': cfg['description'], 'installed': installed, 'path': path or ''})
        preferred = policy.get_preferred_cli_agent()
        return {'agents': agents, 'preferred': preferred, 'timeout': policy.CLI_AGENT_TIMEOUT}
