"""Authenticated IM bridge: configured room ownership, shared ChatRequest and durable dedup."""

import hashlib
import json
import re
import threading
import time
from typing import Any

from .component_service import ComponentService
from .conversation_service import ChatRequest, respond_to_chat
from .periodic_service import conversation_scope
from .service_runtime import ServiceError


class ImService(ComponentService):
    OPERATIONS = ('bind', 'bindings', 'receive', 'events')

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._lock = threading.RLock()
        self._active: set[tuple[str, str]] = set()

    def bind(self, conv_id: str, payload: dict) -> dict:
        project = conversation_scope(self.owner, conv_id)
        channel, room = str(payload.get('channel', '')), str(payload.get('room_id', ''))
        enabled = payload.get('enabled', True)
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', channel) or not room or len(room) > 200 or not isinstance(enabled, bool):
            raise ServiceError(422, '请提供通道名、房间标识和布尔启用状态')
        with self.owner.db._get_conn() as conn:
            conn.execute('''INSERT INTO im_bindings VALUES (?,?,?,?,?) ON CONFLICT(channel,room_id)
                DO UPDATE SET conv_id=excluded.conv_id,project_id=excluded.project_id,enabled=excluded.enabled''',
                (channel, room, conv_id, project, int(enabled)))
        return dict(channel=channel, room_id=room, conv_id=conv_id, project_id=project, enabled=enabled)

    def bindings(self, conv_id: str) -> dict:
        conversation_scope(self.owner, conv_id)
        return {'items': [dict(row) for row in self.owner.db._get_conn().execute(
            'SELECT * FROM im_bindings WHERE conv_id=?', (conv_id,))]}

    def events(self, conv_id: str) -> dict:
        project = conversation_scope(self.owner, conv_id)
        conn = self.owner.db._get_conn()
        items = [dict(row) for row in conn.execute("""SELECT channel,event_id,room_id,state,created_at
            FROM im_inbox WHERE conv_id=? AND project_id=? ORDER BY created_at DESC LIMIT 100""", (conv_id, project))]
        for item in items:
            item['replies'] = [dict(row) for row in conn.execute('SELECT part,state FROM im_reply_outbox WHERE event_id=? AND room_id=?',
                                                               (item['event_id'], item['room_id']))] if item['channel'] == 'telegram' else []
        return {'items': items}

    def _binding(self, channel: str, room: str) -> dict:
        row = self.owner.db._get_conn().execute('SELECT * FROM im_bindings WHERE channel=? AND room_id=? AND enabled=1',
                                              (channel, room)).fetchone()
        if not row:
            raise ServiceError(403, '该IM房间未获授权或已停用')
        row = dict(row)
        if conversation_scope(self.owner, row['conv_id']) != row['project_id']:
            raise ServiceError(409, '对话项目已变更，请管理员重新绑定IM房间')
        return row

    def receive(self, channel: str, payload: dict) -> dict:
        event_id, room = str(payload.get('event_id', '')), str(payload.get('room_id', ''))
        message = payload.get('message', '')
        if not event_id or len(event_id) > 300 or not isinstance(message, str) or not 1 <= len(message) <= 32000:
            raise ServiceError(422, '消息需包含稳定event_id、room_id及1至32000字符message')
        binding = self._binding(channel, room)
        canonical = dict(room_id=room, message=message, kind=payload.get('kind', 'chat'),
                         pm_id=str(payload.get('pm_id', '')), conv_id=binding['conv_id'], project_id=binding['project_id'])
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (channel, event_id)
        with self._lock:
            row, claimed = self._claim(channel, event_id, room, digest, binding)
            if not claimed:
                if row['fingerprint'] != digest:
                    raise ServiceError(409, '同一IM事件标识对应不同内容或对话')
                if row['state'] == 'processing' and key not in self._active and time.time() - row['created_at'] > 120:
                    return {'state': 'review_required', 'reply': '上次处理未能确认，请核对任务记录；不会自动重复操作', 'duplicate': True}
                return {'state': row['state'], **json.loads(row['response_json']), 'duplicate': True}
            self._active.add(key)
        try:
            result = self._respond(binding, canonical)
            state = 'completed'
        except Exception as exc:
            result = {'reply': '消息处理未能确认，请核对任务记录后再决定是否重试', 'error': str(exc)}
            state = 'review_required'
        finally:
            with self._lock:
                self._active.discard(key)
        with self.owner.db._get_conn() as conn:
            conn.execute('UPDATE im_inbox SET state=?,response_json=? WHERE channel=? AND event_id=?',
                         (state, json.dumps(result, ensure_ascii=False), channel, event_id))
        print(f'[Bot] IM事件已记账: {channel}: {event_id}: {state}')
        return {'state': state, **result, 'duplicate': False}

    def _claim(self, channel: str, event_id: str, room: str, digest: str, binding: dict) -> tuple[dict, bool]:
        with self.owner.db._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO im_inbox(channel,event_id,room_id,conv_id,project_id,fingerprint,state,created_at) VALUES (?,?,?,?,?,?,?,?)',
                         (channel, event_id, room, binding['conv_id'], binding['project_id'], digest, 'pending', time.time()))
            row = dict(conn.execute('SELECT * FROM im_inbox WHERE channel=? AND event_id=?', (channel, event_id)).fetchone())
            if row['fingerprint'] != digest:
                return row, False
            claimed = conn.execute("UPDATE im_inbox SET state='processing' WHERE channel=? AND event_id=? AND state='pending'",
                                   (channel, event_id)).rowcount
            return row, bool(claimed)

    def _respond(self, binding: dict, event: dict) -> dict:
        if event['kind'] == 'decision':
            pm_id = event['pm_id']
            if self.owner.chat_handler.find_conv_by_pm(pm_id) != binding['conv_id']:
                raise ServiceError(403, '该PM不属于此IM对话')
            result = self.owner.inject_input_to_pm(pm_id, {'response': event['message'], 'choice': event['message']})
            return {'reply': '回复已送达PM' if result.get('ok') else result.get('message', '回复未送达'), **result}
        if event['kind'] != 'chat':
            raise ServiceError(422, '不支持的消息类型')
        text = event['message']
        command = text.split(maxsplit=1)[0]
        gateway = getattr(self.owner, 'bot_gateway', None)
        handler = getattr(gateway, '_command_handler', None)
        if command in {'/status', '/hosts', '/tasks', '/help', '/ping'} and callable(handler):
            return {'reply': str(handler(command[1:], '', binding['room_id']))}
        return respond_to_chat(self.owner, ChatRequest(text, conv_id=binding['conv_id']))
