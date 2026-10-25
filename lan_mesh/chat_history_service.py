"""ChatHistoryService: extracted domain behavior with explicit component ownership."""

import time
from .chat_policy import (logger)

from .component_service import ComponentService

class ChatHistoryService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('get_history', 'clear_history', 'list_conversations', 'create_conversation', 'delete_conversation', 'rename_conversation', 'get_messages', 'switch_conversation', 'attach_pm_thread', 'detach_pm_thread', 'update_pm_thread_status', 'get_pm_threads', 'find_conv_by_pm', 'delete_pm_thread', 'send_to_pm_thread', 'get_pm_thread_messages', 'append_pm_thread_message', '_trim_pm_thread', 'notify_pm_clarification', '_get_conv_dir', '_load_conv_index', '_save_conv_index', '_create_conversation', '_get_conv_meta', '_touch_conv', '_save_message_to_file', '_load_conversation_from_file', '_clear_conv_file', '_delete_conv_file', '_get_thread_dir', '_save_thread_index', '_save_thread_message_to_file', '_load_thread_messages_from_file', '_auto_title', '_load_history_from_db', '_save_to_db')

    def get_history(self, limit: int = 50, conv_id: str = "") -> list[dict]:
        """返回指定对话的最近消息。"""
        cid = conv_id or self.owner._active_conv_id
        msgs = self.owner._conversations.get(cid, [])
        return msgs[-limit:]


    def clear_history(self, conv_id: str = ""):
        """清空指定对话历史。"""
        cid = conv_id or self.owner._active_conv_id
        self.owner._conversations[cid] = []
        # 清空文件
        self.owner._clear_conv_file(cid)


    def list_conversations(self) -> list[dict]:
        """返回对话列表 (按 updated_at 降序)。"""
        return sorted(self.owner._conv_index, key=lambda c: c.get("updated_at", 0), reverse=True)


    def create_conversation(self, title: str, project_id: str = "") -> dict:
        """新建对话, 返回对话元数据。"""
        cid = self.owner._create_conversation(title, project_id)
        return self.owner._get_conv_meta(cid)


    def delete_conversation(self, conv_id: str) -> bool:
        """删除对话。"""
        self.owner._conv_index = [c for c in self.owner._conv_index if c["id"] != conv_id]
        self.owner._conversations.pop(conv_id, None)
        self.owner._save_conv_index()
        self.owner._delete_conv_file(conv_id)
        # 若删除的是活跃对话, 切换到第一个
        if self.owner._active_conv_id == conv_id:
            self.owner._active_conv_id = self.owner._conv_index[0]["id"] if self.owner._conv_index else ""
        return True


    def rename_conversation(self, conv_id: str, title: str) -> bool:
        """重命名对话。"""
        for c in self.owner._conv_index:
            if c["id"] == conv_id:
                c["title"] = title
                self.owner._save_conv_index()
                return True
        return False


    def get_messages(self, conv_id: str, limit: int = 100) -> list[dict]:
        """获取指定对话的消息 (懒加载)。"""
        if conv_id not in self.owner._conversations:
            self.owner._conversations[conv_id] = self.owner._load_conversation_from_file(conv_id)
        return self.owner._conversations.get(conv_id, [])[-limit:]


    def switch_conversation(self, conv_id: str) -> bool:
        """切换活跃对话。"""
        if any(c["id"] == conv_id for c in self.owner._conv_index):
            self.owner._active_conv_id = conv_id
            # 懒加载消息
            if conv_id not in self.owner._conversations:
                self.owner._conversations[conv_id] = self.owner._load_conversation_from_file(conv_id)
            return True
        return False


    def attach_pm_thread(self, conv_id: str, pm_id: str, task_name: str = "",
                         agent_name: str = "") -> dict:
        """将 PM Agent 绑定到对话的线程列表。

        当任务创建并分配 PM 后调用, 在对应项目对话中注册线程卡片。
        """
        meta = self.owner._get_conv_meta(conv_id)
        if not meta:
            return {"ok": False, "message": "对话不存在"}

        threads = meta.setdefault("pm_threads", [])
        # 避免重复绑定
        if any(t["pm_id"] == pm_id for t in threads):
            return {"ok": True, "message": "PM 线程已存在", "pm_id": pm_id}

        thread_info = {
            "pm_id": pm_id,
            "task_name": task_name,
            "agent_name": agent_name or f"PM-{pm_id[:8]}",
            "status": "starting",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        threads.append(thread_info)
        self.owner._pm_thread_messages.setdefault(pm_id, [])
        self.owner._save_conv_index()
        self.owner._save_thread_index(conv_id)
        logger.info("[方案C] PM %s 已绑定到对话 %s", pm_id[:8], conv_id)
        return {"ok": True, "pm_id": pm_id, "thread": thread_info}


    def detach_pm_thread(self, conv_id: str, pm_id: str) -> bool:
        """从对话中移除 PM 线程。"""
        meta = self.owner._get_conv_meta(conv_id)
        if not meta:
            return False
        threads = meta.get("pm_threads", [])
        meta["pm_threads"] = [t for t in threads if t["pm_id"] != pm_id]
        self.owner._save_conv_index()
        self.owner._save_thread_index(conv_id)
        return True


    def update_pm_thread_status(self, pm_id: str, status: str, **kwargs):
        """更新 PM 线程状态 (在所有关联对话中查找)。"""
        for meta in self.owner._conv_index:
            for t in meta.get("pm_threads", []):
                if t["pm_id"] == pm_id:
                    t["status"] = status
                    t["updated_at"] = time.time()
                    if kwargs.get("task_name"):
                        t["task_name"] = kwargs["task_name"]
                    self.owner._save_conv_index()
                    self.owner._save_thread_index(meta["id"])
                    return


    def get_pm_threads(self, conv_id: str) -> list[dict]:
        """获取对话关联的所有 PM 线程。"""
        meta = self.owner._get_conv_meta(conv_id)
        if not meta:
            return []
        return meta.get("pm_threads", [])


    def find_conv_by_pm(self, pm_id: str) -> str:
        """根据 pm_id 反查所属对话 ID。"""
        for meta in self.owner._conv_index:
            if any(t["pm_id"] == pm_id for t in meta.get("pm_threads", [])):
                return meta["id"]
        return ""


    def delete_pm_thread(self, pm_id: str):
        """彻底删除 PM 线程 (内存消息 + JSONL 文件), 任务删除时调用。"""
        self.owner._pm_thread_messages.pop(pm_id, None)
        import os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self.owner._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception:
            pass


    def send_to_pm_thread(self, conv_id: str, pm_id: str, message: str) -> dict:
        """L2 路由: 用户在 PM 线程内直接发消息给 PM Agent。

        跳过秘书 LLM, 直接注入到 PM 的 receive_input。
        """
        now = time.time()
        # 记录用户消息到线程
        user_msg = {"role": "user", "content": message, "timestamp": now, "layer": "L2"}
        self.owner._pm_thread_messages.setdefault(pm_id, []).append(user_msg)
        self.owner._save_thread_message_to_file(pm_id, user_msg)
        self.owner._trim_pm_thread(pm_id)

        # 注入到 PM Agent
        result = self.owner.controller.inject_input_to_pm(pm_id, {
            "response": message,
            "choice": message,
            "source": "pm_thread",  # 标记来源为线程直发
        })

        if result.get("ok"):
            # 记录系统确认消息
            ack_msg = {
                "role": "system",
                "content": f"✅ 已发送给 PM Agent",
                "timestamp": time.time(),
                "layer": "L2",
            }
            self.owner._pm_thread_messages[pm_id].append(ack_msg)
            self.owner._save_thread_message_to_file(pm_id, ack_msg)
            self.owner._trim_pm_thread(pm_id)
            self.owner.update_pm_thread_status(pm_id, "executing")
            return {"ok": True, "reply": ack_msg["content"], "timestamp": now, "pm_id": pm_id}

        return {"ok": False, "reply": f"❌ 发送失败: {result.get('message', '未知错误')}",
                "timestamp": now, "pm_id": pm_id}


    def get_pm_thread_messages(self, pm_id: str, limit: int = 100) -> list[dict]:
        """获取 PM 线程的历史消息 (懒加载)。"""
        if pm_id not in self.owner._pm_thread_messages:
            self.owner._pm_thread_messages[pm_id] = self.owner._load_thread_messages_from_file(pm_id)
            self.owner._trim_pm_thread(pm_id)
        return self.owner._pm_thread_messages.get(pm_id, [])[-limit:]


    def append_pm_thread_message(self, pm_id: str, role: str, content: str, **kwargs):
        """向 PM 线程追加消息 (PM 回复/状态变更等)。"""
        msg = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "layer": "L2",
            **kwargs,
        }
        self.owner._pm_thread_messages.setdefault(pm_id, []).append(msg)
        self.owner._save_thread_message_to_file(pm_id, msg)
        self.owner._trim_pm_thread(pm_id)


    def _trim_pm_thread(self, pm_id: str):
        """裁剪 PM 线程消息 (内存 + 文件原子重写), 防止无限增长。"""
        msgs = self.owner._pm_thread_messages.get(pm_id, [])
        if len(msgs) <= self.owner._max_history * 2:
            return
        trimmed = msgs[-(self.owner._max_history * 2):]
        self.owner._pm_thread_messages[pm_id] = trimmed
        # 同步重写 JSONL 文件, 避免文件无限增长 (原子写防损坏)
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self.owner._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for m in trimmed:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, filepath)
        except Exception:
            pass


    def notify_pm_clarification(self, pm_id: str, question: str, options: list = None):
        """方案C 双写: PM 请求决策时同时写入 L1 通知 + L2 线程消息。

        由 station_api 的 /api/pm/{pm_id}/status 端点在检测到 awaiting_input 时调用。
        """
        now = time.time()
        options = options or []

        # ── L2: 写入 PM 线程 ──
        clarification_msg = {
            "role": "pm",
            "content": question,
            "timestamp": now,
            "layer": "L2",
            "type": "clarification",
            "options": options,
        }
        self.owner._pm_thread_messages.setdefault(pm_id, []).append(clarification_msg)
        self.owner._save_thread_message_to_file(pm_id, clarification_msg)
        self.owner._trim_pm_thread(pm_id)

        # ── L1: 写入主对话流 (作为系统通知卡片) ──
        conv_id = self.owner.find_conv_by_pm(pm_id)
        if conv_id:
            # 获取 PM 名称
            agent_name = f"PM-{pm_id[:8]}"
            meta = self.owner._get_conv_meta(conv_id)
            if meta:
                for t in meta.get("pm_threads", []):
                    if t["pm_id"] == pm_id:
                        agent_name = t.get("agent_name", agent_name)
                        break

            options_hint = ""
            if options:
                options_hint = "\n可选项: " + " | ".join(options)

            notify_content = (
                f"⚠️ **{agent_name} 等待您的决策**\n"
                f"问题: {question}{options_hint}\n"
                f"→ 展开 PM 线程直接回复, 或在此输入「告诉PM {pm_id[:12]} <您的决策>」"
            )
            notify_msg = {
                "role": "system",
                "content": notify_content,
                "timestamp": now,
                "layer": "L1",
                "type": "pm_clarification",
                "pm_id": pm_id,
                "options": options,
            }
            self.owner._conversations.setdefault(conv_id, []).append(notify_msg)
            self.owner._save_message_to_file(conv_id, notify_msg)
            self.owner._touch_conv(conv_id)

        # 更新线程状态
        self.owner.update_pm_thread_status(pm_id, "awaiting_input")
        logger.info("[方案C] PM %s 决策请求已双写 L1+L2", pm_id[:8])


    def _get_conv_dir(self):
        """获取对话存储目录。"""
        import os
        if self.owner._shared_folder:
            base = self.owner._shared_folder.path
        else:
            from pathlib import Path
            base = Path.home() / ".lan_mesh"
        conv_dir = os.path.join(str(base), "conversations")
        os.makedirs(conv_dir, exist_ok=True)
        return conv_dir


    def _load_conv_index(self):
        """从共享文件夹加载对话索引。"""
        import json, os
        idx_path = os.path.join(self.owner._get_conv_dir(), "index.json")
        if os.path.isfile(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    self.owner._conv_index = json.load(f)
            except Exception:
                self.owner._conv_index = []
        # 预加载所有对话消息
        for c in self.owner._conv_index:
            cid = c["id"]
            if cid not in self.owner._conversations:
                self.owner._conversations[cid] = self.owner._load_conversation_from_file(cid)


    def _save_conv_index(self):
        """原子写入对话索引到共享文件夹。"""
        import json, os
        idx_path = os.path.join(self.owner._get_conv_dir(), "index.json")
        tmp_path = idx_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.owner._conv_index, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, idx_path)  # 原子替换
        except Exception:
            pass


    def _create_conversation(self, title: str, project_id: str = "") -> str:
        """内部创建对话, 返回 conv_id。"""
        import uuid
        cid = f"conv-{uuid.uuid4().hex[:10]}"
        now = time.time()
        meta = {
            "id": cid,
            "title": title,
            "project_id": project_id,
            "pm_threads": [],  # 方案C: 关联的 PM 线程列表
            "created_at": now,
            "updated_at": now,
        }
        self.owner._conv_index.append(meta)
        self.owner._conversations[cid] = []
        self.owner._active_conv_id = cid
        self.owner._save_conv_index()
        return cid


    def _get_conv_meta(self, conv_id: str) -> dict:
        for c in self.owner._conv_index:
            if c["id"] == conv_id:
                return c
        return {}


    def _touch_conv(self, conv_id: str):
        """更新对话的 updated_at。"""
        for c in self.owner._conv_index:
            if c["id"] == conv_id:
                c["updated_at"] = time.time()
                break
        self.owner._save_conv_index()


    def _save_message_to_file(self, conv_id: str, msg: dict):
        """追加写入消息到 JSONL 文件。"""
        import json, os
        filepath = os.path.join(self.owner._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass


    def _load_conversation_from_file(self, conv_id: str) -> list[dict]:
        """从 JSONL 文件加载对话消息。"""
        import json, os
        filepath = os.path.join(self.owner._get_conv_dir(), f"{conv_id}.jsonl")
        messages = []
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
            except Exception:
                pass
        return messages


    def _clear_conv_file(self, conv_id: str):
        """清空对话文件。"""
        import os
        filepath = os.path.join(self.owner._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                open(filepath, "w").close()
        except Exception:
            pass


    def _delete_conv_file(self, conv_id: str):
        """删除对话文件。"""
        import os
        filepath = os.path.join(self.owner._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception:
            pass


    def _get_thread_dir(self):
        """获取 PM 线程存储目录。"""
        import os
        thread_dir = os.path.join(self.owner._get_conv_dir(), "pm_threads")
        os.makedirs(thread_dir, exist_ok=True)
        return thread_dir


    def _save_thread_index(self, conv_id: str):
        """保存对话的 PM 线程索引到独立文件 (pm_threads/{conv_id}.json)。"""
        import json, os
        meta = self.owner._get_conv_meta(conv_id)
        if not meta:
            return
        filepath = os.path.join(self.owner._get_thread_dir(), f"{conv_id}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(meta.get("pm_threads", []), f, ensure_ascii=False, indent=2)
        except Exception:
            pass


    def _save_thread_message_to_file(self, pm_id: str, msg: dict):
        """追加写入 PM 线程消息到 JSONL 文件。"""
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self.owner._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass


    def _load_thread_messages_from_file(self, pm_id: str) -> list[dict]:
        """从 JSONL 文件加载 PM 线程消息。"""
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self.owner._get_thread_dir(), f"{safe_id}.jsonl")
        messages = []
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
            except Exception:
                pass
        return messages


    def _auto_title(self, conv_id: str, first_message: str):
        """O1: 根据首条消息自动生成对话标题 (5字内)。"""
        try:
            prompt = f"请用不超过8个字概括以下对话主题,直接输出标题,不要任何解释:\n{first_message[:100]}"
            resp = self.owner.runtime._call_llm_with_routing(
                prompt,
                {"_model_preference": getattr(self.owner.controller, '_default_model', ''),
                 "_fallback_models": [], "_system_prompt": "你是标题生成器,只输出标题文本。"}
            )
            title = resp.get("content", "").strip().strip('"').strip("'")[:20]
            if title:
                self.owner.rename_conversation(conv_id, title)
        except Exception:
            pass  # 标题生成失败不影响主流程


    def _load_history_from_db(self) -> list[dict]:
        """从 DB 加载聊天历史到内存。"""
        if not self.owner._db:
            return []
        try:
            rows = self.owner._db.get_chat_history(limit=self.owner._max_history * 2)
            history = []
            for r in rows:
                entry = {
                    "role": r.get("role", "user"),
                    "content": r.get("content", ""),
                    "timestamp": r.get("timestamp", 0),
                }
                if r.get("action_taken"):
                    entry["action_taken"] = r["action_taken"]
                history.append(entry)
            logger.info("从 DB 加载 %d 条历史对话", len(history))
            return history
        except Exception as e:
            logger.error("加载 DB 聊天历史异常: %s", e)
            return []


    def _save_to_db(self, role: str, content: str, action_taken: str = "", timestamp: float = 0):
        """将单条聊天记录写入 DB。"""
        if not self.owner._db:
            return
        try:
            self.owner._db.save_chat_message(role, content, action_taken, timestamp)
        except Exception as e:
            logger.error("保存聊天记录异常: %s", e)

