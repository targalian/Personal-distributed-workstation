"""RuntimeLlmService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
import time
from typing import Any, Optional
from . import agent_runtime as policy
from .component_service import ComponentService
from .skill_knowledge import append_builtin_guidance
from .runtime_model_budget import ModelWindowBudget
from .runtime_react_archive import ReactArchive
from .runtime_usage import metered_call, observe_usage, record_external_usage

class RuntimeLlmService(ComponentService):
    """RuntimeLlmService business operations with explicit state ownership."""
    OPERATIONS = ('_call_llm_messages_with_tools', '_call_openai_with_tools', '_call_llm_with_routing', '_resolve_provider', '_call_openai_compatible', '_call_llm_full', '_get_provider_models', '_get_default_model', '_call_llm')

    def _configured_providers(self) -> dict:
        """Honor configured pool boundaries, including custom/free API providers."""
        pool = policy._load_model_pool_entries()
        if not pool:
            return policy.PROVIDER_CONFIG
        providers = {}
        for entry in pool.values():
            if entry.usage_mode == 'chatgpt_plan':
                continue
            providers.setdefault(entry.provider, {'base_url': entry.base_url,
                                                  'api_key_env': entry.api_key_env})
        return providers

    def _call_plan(self, entry: Any, messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
        """Keep subscription accounting owned by this runtime, including failed attempts."""
        from .codex_plan import call_plan
        call_type = 'tools' if tools is not None else 'chat'
        started = time.monotonic()
        try:
            result = call_plan(entry, messages, tools)
        except Exception:
            record_external_usage(self.owner, entry.id, 0, 0, call_type, outcome='failed',
                                  usage_known=False, started=started)
            raise
        result['usage_id'] = record_external_usage(
            self.owner, entry.id, result.get('input_tokens', 0),
            result.get('output_tokens', 0), call_type, outcome='completed',
            usage_known=bool(result.get('usage_known')), started=started)
        return result

    def _thinking_options(self, model_id: str) -> dict:
        """Translate the optional thinking setting for supported providers."""
        entry = policy._load_model_pool_entries().get(model_id)
        enabled = getattr(entry, 'thinking_enabled', None)
        if enabled is None:
            return {}
        if entry.provider == 'siliconflow':
            return {'enable_thinking': enabled}
        if entry.provider == 'zhipu':
            return {'thinking': {'type': 'enabled' if enabled else 'disabled'}}
        return {}

    def _call_llm_messages_with_tools(self, messages: list[dict], tools: list[dict], input_data: dict) -> dict:
        """F2.1: 支持多轮 messages + tools 的 LLM 调用。

        与 _call_llm_with_routing 类似, 但支持完整 messages 数组和 tools 参数。
        """
        model_pref = input_data.get('_model_preference', '')
        fallbacks = input_data.get('_fallback_models', [])
        chain = []
        if model_pref:
            chain = [model_pref] + [m for m in fallbacks if m != model_pref]
        else:
            for provider, cfg in self._configured_providers().items():
                api_key = policy.os.environ.get(cfg['api_key_env'], '')
                if api_key:
                    default_model = self.owner._get_default_model(provider)
                    if default_model:
                        chain.append(default_model)
            # 订阅模型没有 API Key，必须单独加入无偏好候选链，否则纯订阅池会得到空链
            from .codex_plan import model_available
            for entry in policy._load_model_pool_entries().values():
                if entry.usage_mode == 'chatgpt_plan' and model_available(entry):
                    chain.append(entry.id)
        last_error = None
        candidates = []
        pool = policy._load_model_pool_entries()
        archive = input_data.get('_react_archive')
        for model_id in dict.fromkeys(chain):
            if self.owner._cancel_event.is_set():
                return {'status': 'cancelled', 'error': '任务已取消'}
            provider_cfg = self.owner._resolve_provider(model_id)
            if not provider_cfg:
                continue
            if provider_cfg.get('usage_mode') == 'chatgpt_plan':
                try:
                    response = self._call_plan(provider_cfg['model_entry'], messages, tools)
                    return {**response, 'context_candidates': candidates}
                except Exception as e:
                    last_error = e
                    policy.logger.warning('[ReAct] 模型 %s 订阅调用失败: %s, 降级...', model_id, e)
                    continue
            window = ModelWindowBudget.resolve(pool.get(model_id), input_data)
            checkpoint = archive.store.latest if isinstance(archive, ReactArchive) else ''
            fitted, report = window.prepare(messages, tools, input_data, checkpoint)
            report['model'] = model_id
            candidates.append(report)
            if fitted is None:
                last_error = ValueError(f'{model_id} 上下文预算不足，未发送请求')
                continue
            if isinstance(archive, ReactArchive):
                archive.store.save('model_request', dict(messages=fitted, tools=tools, budget=report))
            if self.owner._cancel_event.is_set():
                return {'status': 'cancelled', 'error': '任务已取消', 'context_candidates': candidates}
            try:
                response = self.owner._call_openai_with_tools(
                    fitted, tools, model_id, provider_cfg['base_url'], provider_cfg['api_key'], window.reply_tokens)
                return {**response, 'context_candidates': candidates}
            except Exception as e:
                last_error = e
                policy.logger.warning('[ReAct] 模型 %s 调用失败: %s, 降级...', model_id, e)
                continue
        error = f'[模型调用失败] {last_error or "没有配置可用模型或 API Key"}'
        return {'message': {'content': error, 'tool_calls': []}, 'status': 'failed',
                'error': error, 'model': 'none', '_error': True, 'input_tokens': 0, 'output_tokens': 0,
                'context_candidates': candidates}

    @metered_call('tools')
    def _call_openai_with_tools(self, messages: list[dict], tools: list[dict], model_id: str, base_url: str, api_key: str, max_tokens: int = 4096) -> dict:
        """F2.1: OpenAI 兼容 API 调用 (支持 tools 参数)。"""
        import time as _time
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {'model': model_id, 'messages': messages, 'max_tokens': max_tokens}
        payload.update(self._thinking_options(model_id))
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        call_start = _time.time()
        resp = policy.requests.post(url, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        call_ms = (_time.time() - call_start) * 1000
        usage = data.get('usage', {})
        observe_usage(usage)
        choice_msg = data['choices'][0]['message']
        result = {'message': choice_msg, 'model': model_id, 'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': usage.get('completion_tokens', 0)}
        result['finish_reason'] = data['choices'][0].get('finish_reason', '')
        return result

    def _call_llm_with_routing(self, prompt: str, input_data: dict) -> dict:
        """带路由决策的 LLM 调用, 支持降级链重试。

        优先使用 orchestrator 注入的 model_preference,
        失败时沿 fallback_models 链路重试。
        如果无路由器信息, 回退到旧逻辑 (deepseek → openai)。
        """
        model_pref = input_data.get('_model_preference', '')
        fallbacks = input_data.get('_fallback_models', [])
        external_system_prompt = input_data.get('_system_prompt', '')
        if external_system_prompt:
            external_system_prompt = append_builtin_guidance(
                external_system_prompt, input_data.get('description', input_data.get('requirement', '')))
        blueprint_suffix = policy._build_blueprint_prompt(input_data.get('_project_blueprint', ''))
        from .pm_memory import runtime_memory
        blueprint_suffix += runtime_memory(self.owner, input_data)
        if model_pref:
            chain = [model_pref] + [m for m in fallbacks if m != model_pref]
            last_error = None
            unresolved = []
            if external_system_prompt:
                system_prompt = external_system_prompt
            else:
                system_prompt = self.owner._build_system_prompt(input_data.get('description', input_data.get('requirement', '')))
            system_prompt = f'{system_prompt}{blueprint_suffix}'
            for model_id in dict.fromkeys(chain):
                if self.owner._cancel_event.is_set():
                    return self._llm_failure('任务已取消', status='cancelled')
                provider_cfg = self.owner._resolve_provider(model_id)
                if not provider_cfg:
                    unresolved.append(model_id)
                    continue
                try:
                    if provider_cfg.get('usage_mode') == 'chatgpt_plan':
                        messages = ([{'role': 'system', 'content': system_prompt}]
                                    if system_prompt else []) + [{'role': 'user', 'content': prompt}]
                        result = self._call_plan(provider_cfg['model_entry'], messages)
                        message = result.get('message') or {}
                        return {'content': message.get('content', ''),
                                'model': result.get('model', model_id),
                                'input_tokens': result.get('input_tokens', 0),
                                'output_tokens': result.get('output_tokens', 0)}
                    return self.owner._call_openai_compatible(prompt, model_id, provider_cfg['base_url'], provider_cfg['api_key'], system_prompt=system_prompt)
                except Exception as e:
                    last_error = e
                    policy.logger.warning('模型 %s 调用失败: %s, 尝试降级...', model_id, e)
                    continue
            if unresolved and (not last_error):
                return self._llm_failure(f"[模型调用失败] 以下模型的 API Key 未配置: {', '.join(unresolved)}。请在 .env 文件或环境变量中设置对应的 API Key。")
            try:
                from .error_tracker import error_tracker
                error_tracker.capture('llm', last_error, context={'point': 'fallback_exhausted', 'chain': list(chain)})
            except Exception:
                pass
            return self._llm_failure(f'[模型调用失败] 降级链均不可用: {last_error}')
        if external_system_prompt:
            system_prompt = external_system_prompt
        else:
            system_prompt = self.owner._build_system_prompt(input_data.get('description', input_data.get('requirement', '')))
        system_prompt = f'{system_prompt}{blueprint_suffix}'
        return self.owner._call_llm_full(prompt, system_prompt=system_prompt)

    @staticmethod
    def _llm_failure(error: str, status: str = 'failed') -> dict:
        return {'content': error, 'model': 'none', 'input_tokens': 0,
                'output_tokens': 0, '_error': True, 'status': status, 'error': error}

    def _resolve_provider(self, model_id: str) -> Optional[dict]:
        """根据模型 ID 解析 provider 配置, 返回 {base_url, api_key} 或 None。

        查找顺序:
        1. model_pool.yaml 精确匹配 (支持所有 provider, 包括 aliyun-tokenplan)
        2. PROVIDER_CONFIG 前缀匹配 (兼容旧逻辑)
        """
        pool = policy._load_model_pool_entries()
        entry = pool.get(model_id)
        if entry:
            if entry.usage_mode == 'chatgpt_plan':
                return {'base_url': entry.base_url, 'api_key': '', 'usage_mode': 'chatgpt_plan',
                        'model_entry': entry}
            api_key = policy.os.environ.get(entry.api_key_env, '')
            if api_key:
                return {'base_url': entry.base_url, 'api_key': api_key}
        for provider, cfg in policy.PROVIDER_CONFIG.items():
            api_key = policy.os.environ.get(cfg['api_key_env'], '')
            if not api_key:
                continue
            if provider == 'deepseek' and model_id.startswith('deepseek'):
                return {'base_url': cfg['base_url'], 'api_key': api_key}
            if provider == 'openai' and model_id.startswith('gpt'):
                return {'base_url': cfg['base_url'], 'api_key': api_key}
            if provider == 'anthropic' and model_id.startswith('claude'):
                return {'base_url': cfg['base_url'], 'api_key': api_key}
            if provider == 'qwen' and model_id.startswith('qwen'):
                return {'base_url': cfg['base_url'], 'api_key': api_key}
        return None

    @metered_call('chat')
    def _call_openai_compatible(self, prompt: str, model_id: str, base_url: str, api_key: str, system_prompt: str='') -> dict:
        """通用 OpenAI 兼容 API 调用 — 流式输出 + 活性检测。

        超时策略 (替代盲等):
        - 首 token 超时 60s: 模型无反应 → 立即降级
        - 流间超时 90s: 输出中断 → 判定卡死
        - 总时长上限 600s: 兆底保护
        """
        import time as _time
        FIRST_TOKEN_TIMEOUT = 60
        INTER_CHUNK_TIMEOUT = 90
        TOTAL_TIMEOUT = 600
        resp = self._open_stream(prompt, model_id, base_url, api_key, system_prompt, FIRST_TOKEN_TIMEOUT)
        collected = []
        interrupted = False
        input_tokens = 0
        output_tokens = 0
        start_time = _time.time()
        last_chunk_time = start_time
        first_token_received = False
        try:
            for line in resp.iter_lines(decode_unicode=True):
                now = _time.time()
                if now - start_time > TOTAL_TIMEOUT:
                    policy.logger.warning('%s 输出超过总时长上限 %ds, 截断', model_id, TOTAL_TIMEOUT)
                    interrupted = True
                    break
                if self.owner._cancel_event.is_set():
                    interrupted = True
                    break
                if not line:
                    if first_token_received and now - last_chunk_time > INTER_CHUNK_TIMEOUT:
                        policy.logger.warning('%s 流间超时 %ds, 截断已收集内容', model_id, INTER_CHUNK_TIMEOUT)
                        interrupted = True
                        break
                    continue
                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str.strip() == '[DONE]':
                        break
                    try:
                        chunk = policy._json.loads(data_str)
                    except (ValueError, TypeError):
                        continue
                    choices = chunk.get('choices', [])
                    if choices:
                        delta = choices[0].get('delta', {})
                        content = delta.get('content', '')
                        if content:
                            if not first_token_received:
                                first_token_received = True
                                ttft = now - start_time
                                policy.logger.debug('%s 首 token 延迟: %.1fs', model_id, ttft)
                            collected.append(content)
                            last_chunk_time = now
                    u = chunk.get('usage')
                    if u:
                        observe_usage(u)
                        input_tokens = u.get('prompt_tokens', 0)
                        output_tokens = u.get('completion_tokens', 0)
        except policy.requests.exceptions.ChunkedEncodingError:
            interrupted = True
            if collected:
                policy.logger.warning('%s 连接中断, 返回已收集内容 (%d 字符)', model_id, sum((len(c) for c in collected)))
            else:
                raise
        finally:
            resp.close()
        full_content = ''.join(collected)
        if not full_content:
            raise TimeoutError(f'{model_id} 未返回任何内容 (首 token 超时 {FIRST_TOKEN_TIMEOUT}s)')
        result = {'content': full_content, 'model': model_id, 'input_tokens': input_tokens, 'output_tokens': output_tokens}
        if interrupted:
            result.update(status='failed', _error=True, error='模型流中断，输出未完成')
        return result

    def _open_stream(self, prompt: str, model_id: str, base_url: str, api_key: str, system_prompt: str, first_token_timeout: int) -> Any:
        """Close rejected responses before returning a live stream to its reader."""
        url = f"{base_url.rstrip('/')}/chat/completions"
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': prompt})
        payload = {'model': model_id, 'messages': messages, 'max_tokens': 4096, 'stream': True}
        payload.update(self._thinking_options(model_id))
        resp = policy.requests.post(url, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=payload, timeout=(10, first_token_timeout), stream=True)
        try:
            resp.raise_for_status()
        except Exception:
            resp.close()
            raise
        if 'charset' not in resp.headers.get('Content-Type', '').lower():
            resp.encoding = 'utf-8'
        return resp

    def _call_llm_full(self, prompt: str, system_prompt: str='') -> dict:
        """调用外部 LLM API (回退逻辑, 无路由器时使用)。

        遍历所有已配置 API Key 的 provider, 每个 provider 内按 quality_score
        从高到低尝试多个模型 (超时/失败自动降级到更快的模型)。
        """
        policy._ensure_env_loaded()
        errors = []
        from .codex_plan import model_available
        for entry in policy._load_model_pool_entries().values():
            if entry.usage_mode != 'chatgpt_plan' or not model_available(entry):
                continue
            try:
                messages = ([{'role': 'system', 'content': system_prompt}]
                            if system_prompt else []) + [{'role': 'user', 'content': prompt}]
                result = self._call_plan(entry, messages)
                message = result.get('message') or {}
                return {'content': message.get('content', ''), 'model': entry.id,
                        'input_tokens': result.get('input_tokens', 0),
                        'output_tokens': result.get('output_tokens', 0)}
            except Exception as e:
                policy.logger.warning('%s 订阅调用失败: %s, 尝试下一模型...', entry.id, e)
                errors.append(f'{entry.id}: {e}')
        for provider, cfg in self._configured_providers().items():
            api_key = policy.os.environ.get(cfg['api_key_env'], '')
            if not api_key:
                continue
            models = self.owner._get_provider_models(provider)
            if not models:
                errors.append(f'{provider}: 未找到可用模型配置')
                continue
            for model_id in models:
                try:
                    return self.owner._call_openai_compatible(prompt, model_id, cfg['base_url'], api_key, system_prompt=system_prompt)
                except Exception as e:
                    policy.logger.warning('%s(%s) 调用失败: %s, 尝试下一模型...', provider, model_id, e)
                    errors.append(f'{provider}({model_id}): {e}')
                    continue
        if errors:
            detail = '; '.join(errors[-3:])
            return {'content': f'[LLM 调用失败] 已配置 Key 的 Provider 均调用失败: {detail}', 'model': 'none', 'input_tokens': 0, 'output_tokens': 0, '_error': True}
        return {'content': '[未配置 LLM API Key] 请设置 ALIYUN_TOKENPLAN_API_KEY、DEEPSEEK_API_KEY 或 OPENAI_API_KEY 环境变量。', 'model': 'none', 'input_tokens': 0, 'output_tokens': 0, '_error': True}

    def _get_provider_models(self, provider: str) -> list:
        """获取指定 provider 下所有模型 ID, 按 quality_score 降序排列。

        同 provider 内尝试多个模型实现自动降级:
        qwen3.8-max → qwen3.7-max → qwen3.7-plus → qwen3.6-flash
        """
        pool = policy._load_model_pool_entries()
        candidates = [e for e in pool.values()
                      if e.provider == provider and e.usage_mode != 'chatgpt_plan']
        if pool:
            candidates.sort(key=lambda e: e.quality_score, reverse=True)
            return [e.id for e in candidates]
        defaults = {'volcengine-ark': ['ark-code-latest'], 'deepseek': ['deepseek-chat'], 'openai': ['gpt-4o-mini'], 'anthropic': ['claude-3-haiku'], 'qwen': ['qwen-turbo'], 'aliyun-tokenplan': []}
        return defaults.get(provider, [])

    def _get_default_model(self, provider: str) -> str:
        """获取 provider 下默认模型 (quality 最高), 无可用时返回空串。

        iter-55: _call_llm_messages_with_tools 无偏好路径曾调用本方法
        但全库未定义 (AttributeError 隐患), 此处补齐。
        """
        models = self.owner._get_provider_models(provider)
        return models[0] if models else ''

    def _call_llm(self, prompt: str) -> str:
        """调用外部 LLM API 生成回复 (仅返回文本内容)。"""
        return self.owner._call_llm_full(prompt)['content']
