"""Optional task browser capability with isolated contexts and serialized control transfer."""

import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit
import uuid

from .component_service import ComponentService, call_component
from .service_runtime import ServiceError


def origin(url: str) -> str:
    value = urlsplit(url)
    if value.scheme not in {'http', 'https'} or not value.hostname or value.username or value.password:
        raise ServiceError(422, '浏览器仅支持不带凭据的HTTP(S)网址')
    port = value.port or (443 if value.scheme == 'https' else 80)
    return f'{value.scheme}://{value.hostname.lower()}:{port}'


class BrowserSessionService(ComponentService):
    """Each host owns one serial browser driver; tasks never share browser contexts."""
    OPERATIONS = ('create', 'command', 'assigned')

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='task-browser')
        self._sessions: dict[str, dict] = {}
        self._driver = None
        self._browser = None

    def create(self, task_id: str, project_id: str, allowed_origins: list[str]) -> dict:
        return self._executor.submit(self._create, task_id, project_id, allowed_origins).result()

    def assigned(self, task_id: str, project_id: str) -> str:
        def lookup() -> str:
            return next((key for key, row in self._sessions.items()
                         if (row['task_id'], row['project_id']) == (task_id, project_id)), '')
        return self._executor.submit(lookup).result()

    def _create(self, task_id: str, project_id: str, allowed_origins: list[str]) -> dict:
        for key, row in self._sessions.items():
            if (row['task_id'], row['project_id']) == (task_id, project_id):
                return dict(session_id=key, task_id=task_id, project_id=project_id,
                            host_id=self.owner.state.device_id, mode=row['mode'])
        if len(self._sessions) >= 8:
            raise ServiceError(409, '当前主机浏览器会话已达上限，请关闭不用的会话')
        approved = {origin(url) for url in allowed_origins}
        if self._driver is None:
            try:
                from playwright.sync_api import sync_playwright
                self._driver = sync_playwright().start()
                options = dict(getattr(self.owner, '_browser_launch_options', {}))
                self._browser = self._driver.chromium.launch(headless=True, **options)
            except Exception as exc:
                if self._driver:
                    self._driver.stop()
                self._driver = None
                raise ServiceError(503, '浏览器不可用；请安装lan-mesh[browser]及Playwright Chromium') from exc
        context = self._browser.new_context(accept_downloads=False, service_workers='block')
        context.set_default_timeout(10000)
        record = dict(task_id=task_id, project_id=project_id, mode='automatic',
                      context=context, approved=approved)
        context.route('**/*', lambda route: self._route(record, route))
        record['page'] = context.new_page()
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = record
        print(f'[AgentRuntime] 浏览器会话已创建: {task_id}: {session_id}')
        return dict(session_id=session_id, task_id=task_id, project_id=project_id,
                    host_id=self.owner.state.device_id, mode=record['mode'])

    @staticmethod
    def _route(record: dict, route: Any) -> None:
        try:
            allowed = origin(route.request.url) in record['approved']
        except (ServiceError, ValueError):
            allowed = False
        route.continue_() if allowed else route.abort()

    def command(self, session_id: str, operation: str, params: dict,
                task_id: str, project_id: str, actor: str) -> dict:
        return self._executor.submit(self._command, session_id, operation, params,
                                     task_id, project_id, actor).result()

    def _command(self, session_id: str, operation: str, params: dict,
                 task_id: str, project_id: str, actor: str) -> dict:
        record = self._sessions.get(session_id)
        if not record or (record['task_id'], record['project_id']) != (task_id, project_id):
            raise ServiceError(404, '当前主机或任务中没有该浏览器会话')
        if actor not in {'human', 'automation'}:
            raise ServiceError(403, '浏览器控制者无效')
        if operation in {'takeover', 'release', 'close'}:
            if actor != 'human':
                raise ServiceError(403, '仅用户可移交或关闭浏览器')
            if operation == 'close':
                record['context'].close()
                del self._sessions[session_id]
                return {'mode': 'closed', 'session_id': session_id}
            record['mode'] = 'human' if operation == 'takeover' else 'automatic'
            return {'mode': record['mode'], 'session_id': session_id}
        expected = 'human' if actor == 'human' else 'automatic'
        if record['mode'] != expected:
            raise ServiceError(409, '浏览器控制权不匹配，请先接管或交回控制')
        self._perform(record, operation, params, actor)
        page = record['page']
        return dict(session_id=session_id, mode=record['mode'], url=page.url,
                    text=page.locator('body').inner_text(timeout=10000)[:16000],
                    image=base64.b64encode(page.screenshot(full_page=False)).decode('ascii'))

    @staticmethod
    def _perform(record: dict, operation: str, params: dict, actor: str) -> None:
        page = record['page']
        if operation == 'navigate':
            url = str(params.get('url', ''))
            target = origin(url)
            if actor == 'human':
                record['approved'].add(target)
            if target not in record['approved']:
                raise ServiceError(403, '该网址来源尚未由用户授权给此会话')
            page.goto(url, wait_until='domcontentloaded', timeout=10000)
        elif operation in {'click', 'fill'}:
            selector = str(params.get('selector', ''))
            if not selector or len(selector) > 1000:
                raise ServiceError(422, '请选择页面元素')
            element = page.locator(selector)
            if operation == 'click':
                element.click(timeout=10000)
            else:
                element.fill(str(params.get('text', ''))[:8000], timeout=10000)
        elif actor == 'human' and operation == 'click_point':
            x, y = float(params.get('x', -1)), float(params.get('y', -1))
            if not (0 <= x <= 1280 and 0 <= y <= 720):
                raise ServiceError(422, '点击位置超出画面')
            page.mouse.click(x, y)
        elif actor == 'human' and operation == 'type_text':
            page.keyboard.insert_text(str(params.get('text', ''))[:8000])
        elif actor == 'human' and operation == 'press_enter':
            page.keyboard.press('Enter')
        elif operation != 'snapshot':
            raise ServiceError(422, '不支持的浏览器动作')

    def deinit(self) -> None:
        self._executor.submit(self._close).result()
        self._executor.shutdown(wait=True)

    def _close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._driver:
            self._driver.stop()
        self._sessions.clear()


def register_browser_tool(runtime: object, data: dict, registry: Any) -> None:
    """Explicit session assignment grants only the matching task's automatic actions."""
    owner, session_id = getattr(runtime, '_browser_owner', None), data.get('_browser_session_id')
    if owner is None:
        return
    session_id = session_id or call_component(owner, BrowserSessionService, 'assigned',
        task_id=data.get('_task_id', ''), project_id=data.get('_project_id', ''))
    if not session_id:
        return
    from .protocol import ToolDef
    def execute(params: dict) -> dict:
        return call_component(owner, BrowserSessionService, 'command', session_id=session_id,
            operation=params.get('operation', 'snapshot'), params=params, actor='automation',
            task_id=data.get('_task_id', ''), project_id=data.get('_project_id', ''))
    registry.register_tool(ToolDef(name='browser_action', description=(
        '操作明确分配给本任务的独立浏览器。人工接管期间所有动作都会被拒绝。仅允许会话已授权的网站来源。'),
        input_schema={'type': 'object', 'properties': {
            'operation': {'type': 'string', 'enum': ['snapshot', 'navigate', 'click', 'fill']},
            'url': {'type': 'string'}, 'selector': {'type': 'string'}, 'text': {'type': 'string'}},
            'required': ['operation']}), execute)
