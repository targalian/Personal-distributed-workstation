"""Native Telegram adapter with configured-room admission and durable reply receipts."""

import time

from . import bot_gateway as policy
from .component_service import call_component
from .im_service import ImService
from .service_runtime import ServiceError


def receive_native(gateway: object, channel: object, payload: dict) -> bool:
    """Return true when the durable adapter handled or refused a native event."""
    room = str(payload.get('room_id', ''))
    if not channel.chat_id or room != str(channel.chat_id):
        print('[Bot] 已拒绝未配置聊天来源的Telegram消息')
        return True
    handler = getattr(gateway, '_chat_handler', None)
    controller = getattr(handler, 'controller', None)
    if controller is None or getattr(controller, 'db', None) is None:
        return False
    try:
        with gateway._lock:
            binding = controller.db._get_conn().execute(
                'SELECT 1 FROM im_bindings WHERE channel=? AND room_id=?', ('telegram', room)).fetchone()
            if not binding:
                conv = handler.create_conversation('Telegram ' + room)
                call_component(controller, ImService, 'bind', conv_id=conv['id'],
                               payload={'channel': 'telegram', 'room_id': room})
        result = call_component(controller, ImService, 'receive', channel='telegram', payload=payload)
        reply = result.get('reply', '')
        if reply:
            bot_id = str(channel.bot_token).split(':', 1)[0]
            with controller.db._get_conn() as conn:
                for index, part in enumerate(policy._split_message(reply, max_len=4000)):
                    conn.execute('INSERT OR IGNORE INTO im_reply_outbox(bot_id,event_id,part,room_id,message,state) VALUES (?,?,?,?,?,?)',
                                 (bot_id, payload['event_id'], index, room, part, 'pending'))
            flush_replies(gateway, channel)
    except ServiceError as exc:
        print(f'[Bot] Telegram持久入口拒绝或失败: {type(exc).__name__}')
        if exc.status_code not in {403, 404, 409, 422}:
            raise
    except Exception:
        # Do not advance the provider cursor until admission has been persisted.
        raise
    return True


def flush_replies(gateway: object, channel: object) -> None:
    handler = getattr(gateway, '_chat_handler', None)
    controller = getattr(handler, 'controller', None)
    if controller is None or getattr(controller, 'db', None) is None:
        return
    bot_id = str(channel.bot_token).split(':', 1)[0]
    conn = controller.db._get_conn()
    rows = conn.execute("SELECT * FROM im_reply_outbox WHERE bot_id=? AND room_id=? AND state='pending' AND retry_at<=? ORDER BY event_id,part LIMIT 10",
                        (bot_id, str(channel.chat_id), time.time())).fetchall()
    for row in rows:
        key = (bot_id, row['event_id'], row['part'])
        with conn:
            claimed = conn.execute("UPDATE im_reply_outbox SET state='sending' WHERE bot_id=? AND event_id=? AND part=? AND state='pending'", key).rowcount
        if not claimed:
            continue
        state = 'review_required'
        try:
            response = policy.requests.post(f'https://api.telegram.org/bot{channel.bot_token}/sendMessage',
                json={'chat_id': row['room_id'], 'text': row['message']}, timeout=10)
            if response.status_code == 200:
                state = 'sent' if response.json().get('ok') is True else 'pending'
            elif response.status_code == 429:
                state = 'pending'
        except Exception:
            pass  # The provider may have sent the message before acknowledgement was lost.
        with conn:
            conn.execute('UPDATE im_reply_outbox SET state=?,retry_at=? WHERE bot_id=? AND event_id=? AND part=?',
                         (state, time.time() + 30, *key))
