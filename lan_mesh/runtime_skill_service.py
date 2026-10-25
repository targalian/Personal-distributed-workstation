"""RuntimeSkillService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import agent_runtime as policy
from .component_service import ComponentService

class RuntimeSkillService(ComponentService):
    """RuntimeSkillService business operations with explicit state ownership."""
    OPERATIONS = ('_handle_code_generation', '_handle_code_review', '_handle_document_summary', '_handle_rag_search', '_handle_shell_exec', '_handle_file_ops', '_handle_monitoring')

    def _handle_code_generation(self, input_data: dict) -> dict:
        """代码生成 — 调用 LLM API。"""
        requirement = input_data.get('requirement', input_data.get('description', ''))
        language = input_data.get('language', 'python')
        context = input_data.get('context', '')
        prompt = f'请用 {language} 编写以下需求的代码:\n{requirement}\n\n要求:\n- 直接输出完整可运行的代码, 不要反问、不要请求补充信息\n- 需求中未明确的细节, 按最佳实践自行假设 (在代码注释中注明)\n- 包含必要的错误处理和输入验证\n- 代码末尾附带简要使用说明'
        if context:
            prompt += f'\n\n已有代码/上下文:\n{context}'
        resp = self.owner._call_llm_with_routing(prompt, input_data)
        return {'code': resp['content'], 'language': language, 'usage': {'model': resp['model'], 'input_tokens': resp['input_tokens'], 'output_tokens': resp['output_tokens']}}

    def _handle_code_review(self, input_data: dict) -> dict:
        """代码审查 — 调用 LLM API。"""
        code = input_data.get('code', input_data.get('requirement', ''))
        language = input_data.get('language', '')
        prompt = f'请审查以下代码,指出问题并给出改进建议:\n```\n{code}\n```'
        if language:
            prompt = f'语言: {language}\n' + prompt
        resp = self.owner._call_llm_with_routing(prompt, input_data)
        return {'review': resp['content'], 'usage': {'model': resp['model'], 'input_tokens': resp['input_tokens'], 'output_tokens': resp['output_tokens']}}

    def _handle_document_summary(self, input_data: dict) -> dict:
        """文档摘要 — 调用 LLM API。"""
        text = input_data.get('text', input_data.get('requirement', ''))
        max_length = input_data.get('max_length', 500)
        prompt = f'请将以下内容生成不超过 {max_length} 字的摘要:\n{text}'
        resp = self.owner._call_llm_with_routing(prompt, input_data)
        return {'summary': resp['content'], 'usage': {'model': resp['model'], 'input_tokens': resp['input_tokens'], 'output_tokens': resp['output_tokens']}}

    def _handle_rag_search(self, input_data: dict) -> dict:
        """RAG 检索 — 预留接口,当前返回提示。"""
        return {'query': input_data.get('query', ''), 'results': [], 'note': 'RAG 检索尚未实现,需要接入向量数据库'}

    def _handle_shell_exec(self, input_data: dict) -> dict:
        """Shell 命令执行 (沙箱限制)。

        安全策略:
        - 禁止危险命令 (rm -rf /, mkfs, sudo 等)
        - 工作目录限制在 shared_folder 内
        - 输出截断保护
        """
        command = input_data.get('command', input_data.get('requirement', ''))
        from .tool_registry import mark_git_safe_directory_denied, normalize_tool_timeout
        timeout = normalize_tool_timeout(input_data.get('timeout', 30))
        cmd_lower = command.lower().strip()
        for pattern in policy.SHELL_BLOCKED_PATTERNS:
            if pattern in cmd_lower:
                return {'stdout': '', 'stderr': f"[Sandbox] 命令被拒绝: 包含禁止模式 '{pattern}'", 'returncode': -1}
        cwd = input_data.get('cwd', self.owner.shared_folder)
        try:
            result = policy.subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
            stdout = result.stdout[:policy.MAX_OUTPUT_LENGTH]
            stderr = result.stderr[:policy.MAX_OUTPUT_LENGTH]
            return mark_git_safe_directory_denied({'stdout': stdout, 'stderr': stderr, 'returncode': result.returncode})
        except policy.subprocess.TimeoutExpired:
            return {'stdout': '', 'stderr': f'命令超时 ({timeout}s)', 'returncode': -1, 'timed_out': True}

    def _handle_file_ops(self, input_data: dict) -> dict:
        """文件读写操作 (沙箱限制)。

        安全策略:
        - 路径必须在允许的目录内 (shared_folder 及其子目录)
        - 禁止路径穿越 (../)
        - 写入大小限制
        """
        action = input_data.get('action', 'read')
        path = input_data.get('path', '')
        content = input_data.get('content', '')
        if not path:
            return {'error': '未指定文件路径'}
        import os
        resolved = os.path.realpath(os.path.expanduser(path))
        allowed_root = os.path.realpath(self.owner.shared_folder)
        allowed_roots = [allowed_root, '/tmp']
        is_allowed = any((resolved == root or resolved.startswith(root + os.sep) for root in allowed_roots))
        if not is_allowed:
            return {'error': f'[Sandbox] 路径被拒绝: {path} 不在允许目录内 (允许: {self.owner.shared_folder}, /tmp)'}
        if action == 'read':
            if not os.path.isfile(resolved):
                return {'error': f'文件不存在: {path}'}
            if os.path.getsize(resolved) > policy.MAX_FILE_SIZE:
                return {'error': f'文件过大 (> {policy.MAX_FILE_SIZE // 1024 // 1024}MB)'}
            with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                return {'content': f.read(), 'path': path}
        elif action == 'write':
            if len(content) > policy.MAX_FILE_SIZE:
                return {'error': f'写入内容过大 (> {policy.MAX_FILE_SIZE // 1024 // 1024}MB)'}
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            with open(resolved, 'w', encoding='utf-8') as f:
                f.write(content)
            return {'path': path, 'written': len(content)}
        elif action == 'list':
            if not os.path.isdir(resolved):
                return {'error': f'目录不存在: {path}'}
            entries = os.listdir(resolved)
            return {'entries': entries, 'path': path}
        elif action == 'delete':
            if os.path.isfile(resolved):
                os.remove(resolved)
                return {'deleted': path}
            return {'error': f'文件不存在: {path}'}
        else:
            return {'error': f'未知操作: {action}'}

    def _handle_monitoring(self, input_data: dict) -> dict:
        """系统资源监控。"""
        import psutil
        return {'cpu_percent': psutil.cpu_percent(interval=0.5), 'memory_percent': psutil.virtual_memory().percent, 'disk_percent': psutil.disk_usage('/').percent, 'timestamp': policy.time.time()}
