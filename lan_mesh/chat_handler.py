"""
秘书聊天处理器 — Web 端对话接口

职责:
1. 接收用户 (Boss) 通过 Web 聊天窗口发送的消息
2. 构建工作站状态摘要作为 system prompt 上下文
3. 调用 LLM 生成回复
4. 解析回复中的操作意图并执行
5. 返回回复 + 操作结果

聊天历史持久化到 SQLite DB (chat_history 表), 重启不丢失。
"""
import time
from typing import Optional

from .logger import get_logger

logger = get_logger("chat_handler")


# ── 操作意图关键词映射 ──────────────────────────────────────────

_ACTION_KEYWORDS = {
    "提交任务": "submit_task",
    "提交一个": "submit_task",
    "创建任务": "submit_task",
    "新任务": "submit_task",
    "下发任务": "submit_task",
    "激活下发": "submit_task",
    "立即下发": "submit_task",
    "分配任务": "submit_task",
    # 自然语言表达 (用户日常说法)
    "做一个": "submit_task",
    "写一个": "submit_task",
    "开发一个": "submit_task",
    "创建一个": "submit_task",
    "搭建一个": "submit_task",
    "设计一个": "submit_task",
    "生成一个": "submit_task",
    "帮我做": "submit_task",
    "帮我写": "submit_task",
    "帮我开发": "submit_task",
    "帮我创建": "submit_task",
    "实现一个": "submit_task",
    "制作一个": "submit_task",
    "搞一个": "submit_task",
    "弄一个": "submit_task",
    "做个": "submit_task",
    "写个": "submit_task",
    "整一个": "submit_task",
    "启动秘书": "activate_secretary",
    "激活秘书": "activate_secretary",
    "停止秘书": "deactivate_secretary",
    "停用秘书": "deactivate_secretary",
    "状态": "query_status",
    "进度": "query_progress",
    "查看主机": "query_hosts",
    "主机列表": "query_hosts",
    "查看任务": "query_tasks",
    "任务列表": "query_tasks",
    # 优化8: 取消/暂停任务
    "取消任务": "cancel_task",
    "终止任务": "cancel_task",
    "停止任务": "cancel_task",
    "暂停任务": "pause_task",
    # 优化7: 回复 PM 决策请求
    "回复PM": "respond_to_pm",
    "告诉PM": "respond_to_pm",
    "告知PM": "respond_to_pm",
    # 优化9: 验收/退回交付物
    "验收": "accept_delivery",
    "验收通过": "accept_delivery",
    "确认通过": "accept_delivery",
    "退回": "reject_delivery",
    "重新做": "reject_delivery",
    # 项目管理
    "创建项目": "create_project",
    "新建项目": "create_project",
    "建立项目": "create_project",
}


class ChatHandler:
    """秘书聊天处理器 — 支持多项目对话隔离。

    在 Secretary 激活后由 StationController 创建,
    通过 station_api 的 /api/conversations 端点对外服务。

    对话持久化到共享文件夹 (conversations/), 跨主机可访问。
    """

    def __init__(self, runtime, controller, db=None, shared_folder=None):
        """初始化聊天处理器。

        Args:
            runtime: AgentRuntime 实例, 用于 LLM 调用
            controller: StationController 实例
            db: Database 实例 (本地缓存)
            shared_folder: SharedFolderManager 实例 (权威源)
        """
        self.runtime = runtime
        self.controller = controller
        self._db = db
        self._shared_folder = shared_folder
        self._max_history = 50

        # ── 多对话状态 ──
        self._conv_index: list[dict] = []       # [{id, title, project_id, pm_threads, created_at, updated_at}]
        self._conversations: dict[str, list] = {}  # conv_id → messages
        self._active_conv_id: str = ""

        # ── 方案C: PM 线程 (L2 层) ──
        # pm_id → [{role, content, timestamp, ...}]  线程消息独立存储
        self._pm_thread_messages: dict[str, list] = {}

        # 加载对话索引
        self._load_conv_index()

        # 向后兼容: 若无任何对话, 创建默认对话
        if not self._conv_index:
            self._create_conversation("默认对话")

        # 确保活跃对话有效
        if not self._active_conv_id and self._conv_index:
            self._active_conv_id = self._conv_index[0]["id"]

        # 向后兼容: 为旧对话补充 pm_threads 字段
        for c in self._conv_index:
            if "pm_threads" not in c:
                c["pm_threads"] = []

        # ── 优化7: 反向沟通跟踪 ──
        self._last_awaiting_pm: str = ""

    # ── 公开接口 ──────────────────────────────────────────────────

    def chat(self, message: str, conv_id: str = "", history: Optional[list] = None,
             pm_thread_id: str = "") -> dict:
        """处理用户消息, 返回回复。

        Args:
            message: 用户输入文本
            conv_id: 对话 ID (留空则用当前活跃对话)
            history: 可选外部历史 (向后兼容)
            pm_thread_id: 方案C — 若指定, 消息直接路由到 PM 线程 (L2), 跳过秘书 LLM

        Returns:
            {"reply": str, "action_taken": str, "timestamp": float, "conv_id": str}
        """
        # 确定对话
        cid = conv_id or self._active_conv_id
        if not cid or cid not in self._conversations:
            cid = self._conv_index[0]["id"] if self._conv_index else self._create_conversation("默认对话")
        self._active_conv_id = cid

        # ── 方案C: L2 路由 — PM 线程内直接对话 ──
        if pm_thread_id:
            result = self.send_to_pm_thread(cid, pm_thread_id, message)
            return {
                "reply": result.get("reply", ""),
                "action_taken": "pm_thread_direct",
                "timestamp": result.get("timestamp", time.time()),
                "conv_id": cid,
                "pm_id": pm_thread_id,
                "layer": "L2",
            }

        # 获取对话历史
        chat_history = history if history is not None else self._conversations.get(cid, [])

        # 1. 构建状态上下文
        status_context = self._build_status_context()

        # 2. 构建 system prompt
        system_prompt = self._build_system_prompt(status_context)

        # 3. 拼接对话历史 + 用户消息
        prompt = self._build_prompt(message, chat_history)

        # 4. 调用 LLM (优先用全局默认模型, 否则走自动路由)
        model_pref = ""
        fallback_models = []

        # 全局默认模型 (model_pool.yaml 中配置)
        default_model = getattr(self.controller, '_default_model', '')
        if default_model:
            model_pref = default_model
            # 降级链: 从模型池中获取 fallback
            if self.controller.model_router:
                entry = self.controller.model_router._entries.get(default_model)
                if entry and hasattr(entry, 'fallback'):
                    fallback_models = entry.fallback or []
        elif self.controller.model_router:
            try:
                routing = self.controller.model_router.route(message, skill="document_summary")
                model_pref = routing.selected_model
                fallback_models = routing.fallback_chain
            except Exception:
                pass

        resp = self.runtime._call_llm_with_routing(
            prompt,
            {
                "_model_preference": model_pref,
                "_fallback_models": fallback_models,
                "_system_prompt": system_prompt,  # 注入秘书专用 system prompt
            },
        )
        reply_text = resp.get("content", "[LLM 调用失败]")

        # 5. 检测操作意图
        action_taken = ""
        action_result = ""
        action = self._detect_action(message)
        if action:
            action_result = self._execute_action(action, message)
            if action_result:
                reply_text += f"\n\n📋 **操作结果**: {action_result}"
                action_taken = action

        # 6. 保存到对话 + 持久化
        now = time.time()
        user_msg = {"role": "user", "content": message, "timestamp": now}
        asst_msg = {"role": "assistant", "content": reply_text, "timestamp": now, "action_taken": action_taken}

        if cid not in self._conversations:
            self._conversations[cid] = []
        self._conversations[cid].append(user_msg)
        self._conversations[cid].append(asst_msg)
        # 裁剪过长对话
        if len(self._conversations[cid]) > self._max_history * 2:
            self._conversations[cid] = self._conversations[cid][-(self._max_history * 2):]

        # 持久化: 共享文件夹 (权威源) + DB (本地缓存)
        self._save_message_to_file(cid, user_msg)
        self._save_message_to_file(cid, asst_msg)
        self._save_to_db("user", message, timestamp=now)
        self._save_to_db("assistant", reply_text, action_taken=action_taken, timestamp=now)
        # 更新索引时间
        self._touch_conv(cid)

        # O1: 自动标题生成 (首次对话后)
        if len(self._conversations.get(cid, [])) == 2:
            meta = self._get_conv_meta(cid)
            if meta and meta.get("title", "") in ("新对话", "默认对话", ""):
                self._auto_title(cid, message)

        return {
            "reply": reply_text,
            "action_taken": action_taken,
            "timestamp": now,
            "conv_id": cid,
        }

    def get_history(self, limit: int = 50, conv_id: str = "") -> list[dict]:
        """返回指定对话的最近消息。"""
        cid = conv_id or self._active_conv_id
        msgs = self._conversations.get(cid, [])
        return msgs[-limit:]

    def clear_history(self, conv_id: str = ""):
        """清空指定对话历史。"""
        cid = conv_id or self._active_conv_id
        self._conversations[cid] = []
        # 清空文件
        self._clear_conv_file(cid)

    # ── 多对话管理 API ─────────────────────────────────────────

    def list_conversations(self) -> list[dict]:
        """返回对话列表 (按 updated_at 降序)。"""
        return sorted(self._conv_index, key=lambda c: c.get("updated_at", 0), reverse=True)

    def create_conversation(self, title: str, project_id: str = "") -> dict:
        """新建对话, 返回对话元数据。"""
        cid = self._create_conversation(title, project_id)
        return self._get_conv_meta(cid)

    def delete_conversation(self, conv_id: str) -> bool:
        """删除对话。"""
        self._conv_index = [c for c in self._conv_index if c["id"] != conv_id]
        self._conversations.pop(conv_id, None)
        self._save_conv_index()
        self._delete_conv_file(conv_id)
        # 若删除的是活跃对话, 切换到第一个
        if self._active_conv_id == conv_id:
            self._active_conv_id = self._conv_index[0]["id"] if self._conv_index else ""
        return True

    def rename_conversation(self, conv_id: str, title: str) -> bool:
        """重命名对话。"""
        for c in self._conv_index:
            if c["id"] == conv_id:
                c["title"] = title
                self._save_conv_index()
                return True
        return False

    def get_messages(self, conv_id: str, limit: int = 100) -> list[dict]:
        """获取指定对话的消息 (懒加载)。"""
        if conv_id not in self._conversations:
            self._conversations[conv_id] = self._load_conversation_from_file(conv_id)
        return self._conversations.get(conv_id, [])[-limit:]

    def switch_conversation(self, conv_id: str) -> bool:
        """切换活跃对话。"""
        if any(c["id"] == conv_id for c in self._conv_index):
            self._active_conv_id = conv_id
            # 懒加载消息
            if conv_id not in self._conversations:
                self._conversations[conv_id] = self._load_conversation_from_file(conv_id)
            return True
        return False

    # ── 方案C: PM 线程管理 (L2 层) ───────────────────────

    def attach_pm_thread(self, conv_id: str, pm_id: str, task_name: str = "",
                         agent_name: str = "") -> dict:
        """将 PM Agent 绑定到对话的线程列表。

        当任务创建并分配 PM 后调用, 在对应项目对话中注册线程卡片。
        """
        meta = self._get_conv_meta(conv_id)
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
        self._pm_thread_messages.setdefault(pm_id, [])
        self._save_conv_index()
        self._save_thread_index(conv_id)
        logger.info("[方案C] PM %s 已绑定到对话 %s", pm_id[:8], conv_id)
        return {"ok": True, "pm_id": pm_id, "thread": thread_info}

    def detach_pm_thread(self, conv_id: str, pm_id: str) -> bool:
        """从对话中移除 PM 线程。"""
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return False
        threads = meta.get("pm_threads", [])
        meta["pm_threads"] = [t for t in threads if t["pm_id"] != pm_id]
        self._save_conv_index()
        self._save_thread_index(conv_id)
        return True

    def update_pm_thread_status(self, pm_id: str, status: str, **kwargs):
        """更新 PM 线程状态 (在所有关联对话中查找)。"""
        for meta in self._conv_index:
            for t in meta.get("pm_threads", []):
                if t["pm_id"] == pm_id:
                    t["status"] = status
                    t["updated_at"] = time.time()
                    if kwargs.get("task_name"):
                        t["task_name"] = kwargs["task_name"]
                    self._save_conv_index()
                    self._save_thread_index(meta["id"])
                    return

    def get_pm_threads(self, conv_id: str) -> list[dict]:
        """获取对话关联的所有 PM 线程。"""
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return []
        return meta.get("pm_threads", [])

    def find_conv_by_pm(self, pm_id: str) -> str:
        """根据 pm_id 反查所属对话 ID。"""
        for meta in self._conv_index:
            if any(t["pm_id"] == pm_id for t in meta.get("pm_threads", [])):
                return meta["id"]
        return ""

    # ── 方案C: PM 线程消息 (L2 层存储) ─────────────────────

    def send_to_pm_thread(self, conv_id: str, pm_id: str, message: str) -> dict:
        """L2 路由: 用户在 PM 线程内直接发消息给 PM Agent。

        跳过秘书 LLM, 直接注入到 PM 的 receive_input。
        """
        now = time.time()
        # 记录用户消息到线程
        user_msg = {"role": "user", "content": message, "timestamp": now, "layer": "L2"}
        self._pm_thread_messages.setdefault(pm_id, []).append(user_msg)
        self._save_thread_message_to_file(pm_id, user_msg)

        # 注入到 PM Agent
        result = self.controller.inject_input_to_pm(pm_id, {
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
            self._pm_thread_messages[pm_id].append(ack_msg)
            self._save_thread_message_to_file(pm_id, ack_msg)
            self.update_pm_thread_status(pm_id, "executing")
            return {"ok": True, "reply": ack_msg["content"], "timestamp": now, "pm_id": pm_id}

        return {"ok": False, "reply": f"❌ 发送失败: {result.get('message', '未知错误')}",
                "timestamp": now, "pm_id": pm_id}

    def get_pm_thread_messages(self, pm_id: str, limit: int = 100) -> list[dict]:
        """获取 PM 线程的历史消息 (懒加载)。"""
        if pm_id not in self._pm_thread_messages:
            self._pm_thread_messages[pm_id] = self._load_thread_messages_from_file(pm_id)
        return self._pm_thread_messages.get(pm_id, [])[-limit:]

    def append_pm_thread_message(self, pm_id: str, role: str, content: str, **kwargs):
        """向 PM 线程追加消息 (PM 回复/状态变更等)。"""
        msg = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "layer": "L2",
            **kwargs,
        }
        self._pm_thread_messages.setdefault(pm_id, []).append(msg)
        self._save_thread_message_to_file(pm_id, msg)
        # 裁剪过长线程
        if len(self._pm_thread_messages[pm_id]) > self._max_history * 2:
            self._pm_thread_messages[pm_id] = self._pm_thread_messages[pm_id][-(self._max_history * 2):]

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
        self._pm_thread_messages.setdefault(pm_id, []).append(clarification_msg)
        self._save_thread_message_to_file(pm_id, clarification_msg)

        # ── L1: 写入主对话流 (作为系统通知卡片) ──
        conv_id = self.find_conv_by_pm(pm_id)
        if conv_id:
            # 获取 PM 名称
            agent_name = f"PM-{pm_id[:8]}"
            meta = self._get_conv_meta(conv_id)
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
            self._conversations.setdefault(conv_id, []).append(notify_msg)
            self._save_message_to_file(conv_id, notify_msg)
            self._touch_conv(conv_id)

        # 更新线程状态
        self.update_pm_thread_status(pm_id, "awaiting_input")
        logger.info("[方案C] PM %s 决策请求已双写 L1+L2", pm_id[:8])

    # ── 对话持久化 (共享文件夹) ─────────────────────────────────

    def _get_conv_dir(self):
        """获取对话存储目录。"""
        import os
        if self._shared_folder:
            base = self._shared_folder.path
        else:
            from pathlib import Path
            base = Path.home() / ".lan_mesh"
        conv_dir = os.path.join(str(base), "conversations")
        os.makedirs(conv_dir, exist_ok=True)
        return conv_dir

    def _load_conv_index(self):
        """从共享文件夹加载对话索引。"""
        import json, os
        idx_path = os.path.join(self._get_conv_dir(), "index.json")
        if os.path.isfile(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    self._conv_index = json.load(f)
            except Exception:
                self._conv_index = []
        # 预加载所有对话消息
        for c in self._conv_index:
            cid = c["id"]
            if cid not in self._conversations:
                self._conversations[cid] = self._load_conversation_from_file(cid)

    def _save_conv_index(self):
        """原子写入对话索引到共享文件夹。"""
        import json, os
        idx_path = os.path.join(self._get_conv_dir(), "index.json")
        tmp_path = idx_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._conv_index, f, ensure_ascii=False, indent=2)
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
        self._conv_index.append(meta)
        self._conversations[cid] = []
        self._active_conv_id = cid
        self._save_conv_index()
        return cid

    def _get_conv_meta(self, conv_id: str) -> dict:
        for c in self._conv_index:
            if c["id"] == conv_id:
                return c
        return {}

    def _touch_conv(self, conv_id: str):
        """更新对话的 updated_at。"""
        for c in self._conv_index:
            if c["id"] == conv_id:
                c["updated_at"] = time.time()
                break
        self._save_conv_index()

    def _save_message_to_file(self, conv_id: str, msg: dict):
        """追加写入消息到 JSONL 文件。"""
        import json, os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_conversation_from_file(self, conv_id: str) -> list[dict]:
        """从 JSONL 文件加载对话消息。"""
        import json, os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
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
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                open(filepath, "w").close()
        except Exception:
            pass

    def _delete_conv_file(self, conv_id: str):
        """删除对话文件。"""
        import os
        filepath = os.path.join(self._get_conv_dir(), f"{conv_id}.jsonl")
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception:
            pass

    # ── 方案C: PM 线程持久化 ───────────────────────────────

    def _get_thread_dir(self):
        """获取 PM 线程存储目录。"""
        import os
        thread_dir = os.path.join(self._get_conv_dir(), "pm_threads")
        os.makedirs(thread_dir, exist_ok=True)
        return thread_dir

    def _save_thread_index(self, conv_id: str):
        """保存对话的 PM 线程索引到独立文件 (pm_threads/{conv_id}.json)。"""
        import json, os
        meta = self._get_conv_meta(conv_id)
        if not meta:
            return
        filepath = os.path.join(self._get_thread_dir(), f"{conv_id}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(meta.get("pm_threads", []), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_thread_message_to_file(self, pm_id: str, msg: dict):
        """追加写入 PM 线程消息到 JSONL 文件。"""
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self._get_thread_dir(), f"{safe_id}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_thread_messages_from_file(self, pm_id: str) -> list[dict]:
        """从 JSONL 文件加载 PM 线程消息。"""
        import json, os
        safe_id = pm_id.replace("/", "_").replace("\\", "_")
        filepath = os.path.join(self._get_thread_dir(), f"{safe_id}.jsonl")
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
            resp = self.runtime._call_llm_with_routing(
                prompt,
                {"_model_preference": getattr(self.controller, '_default_model', ''),
                 "_fallback_models": [], "_system_prompt": "你是标题生成器,只输出标题文本。"}
            )
            title = resp.get("content", "").strip().strip('"').strip("'")[:20]
            if title:
                self.rename_conversation(conv_id, title)
        except Exception:
            pass  # 标题生成失败不影响主流程

    # ── 持久化辅助方法 ────────────────────────────────────────────

    def _load_history_from_db(self) -> list[dict]:
        """从 DB 加载聊天历史到内存。"""
        if not self._db:
            return []
        try:
            rows = self._db.get_chat_history(limit=self._max_history * 2)
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
        if not self._db:
            return
        try:
            self._db.save_chat_message(role, content, action_taken, timestamp)
        except Exception as e:
            logger.error("保存聊天记录异常: %s", e)

    # ── 内部方法 ──────────────────────────────────────────────────

    def _build_status_context(self) -> str:
        """从 controller.db 获取工作站状态, 拼接为文本摘要。"""
        lines = []

        try:
            # 主机状态
            hosts = self.controller.db.list_hosts()
            online_hosts = [h for h in hosts if h.online]
            lines.append(f"## 工作站状态")
            lines.append(f"- 在线主机: {len(online_hosts)}/{len(hosts)} 台")
            if online_hosts:
                host_names = [h.device_name or h.hostname or h.device_id[:8] for h in online_hosts[:5]]
                lines.append(f"- 在线主机名: {', '.join(host_names)}")

            # Secretary 状态
            secretary_status = "已激活" if self.controller.secretary_active else "未激活"
            lines.append(f"- Secretary: {secretary_status}")

            # PM Agent 状态
            try:
                pm_agents = self.controller.db.list_pm_agents()
                active_pms = [p for p in pm_agents if p.status in ("planning", "executing", "monitoring")]
                lines.append(f"- 活跃 PM Agent: {len(active_pms)} 个")
                if active_pms:
                    for pm in active_pms[:3]:
                        lines.append(f"  - {pm.agent_name} [{pm.status}] 任务: {pm.task_id}")
                # 优化7: 高亮等待决策的 PM
                awaiting_pms = [p for p in pm_agents if p.status == "awaiting_input"]
                if awaiting_pms:
                    # 记录最近的 awaiting PM (用于后续回复匹配)
                    self._last_awaiting_pm = awaiting_pms[-1].pm_id
                    lines.append(f"- ⚠️ 等待决策 PM: {len(awaiting_pms)} 个")
                    for pm in awaiting_pms[:2]:
                        lines.append(f"  - {pm.agent_name} 正在等待您的决策! (任务: {pm.task_id})")
                        lines.append(f"    请回复「告诉PM {pm.agent_name} <您的决策>」来回复")
                # 优化10: 高亮升级的 PM
                escalated_pms = [p for p in pm_agents if p.status == "escalated"]
                if escalated_pms:
                    self._last_awaiting_pm = escalated_pms[-1].pm_id
                    lines.append(f"- 🚨 升级告警 PM: {len(escalated_pms)} 个")
                    for pm in escalated_pms[:2]:
                        lines.append(f"  - {pm.agent_name} 遇到严重失败, 需要您的决策! (任务: {pm.task_id})")
                        lines.append(f"    可选: 忽略继续 / 简化重试 / 指定主机 / 放弃任务")
                        lines.append(f"    请回复「告诉PM {pm.agent_name} <您的选择>」")
            except Exception:
                lines.append("- PM Agent: 数据不可用")

            # 任务状态
            try:
                tasks = self.controller.db.list_tasks(limit=10)
                running_tasks = [t for t in tasks if t.status in ("running", "pending")]
                lines.append(f"- 进行中任务: {len(running_tasks)} 个")
                if running_tasks:
                    for t in running_tasks[:3]:
                        lines.append(f"  - {t.name} [{t.status}]")
            except Exception:
                lines.append("- 任务: 数据不可用")

            # 团队状态
            try:
                teams = self.controller.db.list_teams()
                active_teams = [t for t in teams if t.status in ("active", "pending")]
                lines.append(f"- Agent 团队: {len(active_teams)} 个")
            except Exception:
                pass

        except Exception as e:
            lines.append(f"[状态获取异常: {e}]")

        return "\n".join(lines)

    def _build_system_prompt(self, status_context: str) -> str:
        """构建 LLM system prompt。

        包含秘书身份、能力边界、行为约束和实时工作站状态。
        此 prompt 通过 input_data['_system_prompt'] 注入 LLM 调用。
        """
        return (
            "# 身份\n"
            "你是 LAN Mesh 分布式 AI 工作站的秘书 AI 助手。"
            "你的职责是接收 Boss 的指令, 回答关于工作站状态的问题, 并协助管理任务和 Agent 团队。\n\n"
            "# 能力范围\n"
            "你可以帮助 Boss:\n"
            "1. 查看工作站状态 — 在线主机数量、主机评级、PM Agent 状态、任务进度\n"
            "2. 直接提交任务 — Boss 描述任务后, 系统会自动创建并分配 PM Agent 接管\n"
            "3. 创建项目 — Boss 描述项目后, 系统会自动创建项目\n"
            "4. 激活/停用 Secretary 模式\n"
            "5. 查询任务进度和 PM Agent 团队状态\n"
            "6. 解释工作站的功能和架构 (Station Director/Worker/Secretary/PM Agent)\n\n"
            "# 行为约束 (重要)\n"
            "- 只回答与 LAN Mesh 工作站相关的问题。\n"
            "- 不要编造不存在的功能、文件、数据库或代码。\n"
            "- 如果用户询问工作站能力范围外的问题 (如股票交易、编程开发等), "
            "礼貌地说明你的职责是管理分布式 AI 工作站, 无法处理该类问题。\n"
            "- 回复必须简洁明了, 使用中文, 基于下方实时数据回答, 不要臆测。\n"
            "- 如果不确定某个信息, 如实告知「该信息暂不可用」而非编造。\n\n"
            "# 操作执行规则 (极其重要, 必须严格遵守)\n"
            "- 你只是语言模型, 你本身没有任何执行能力, 不能创建任务、不能创建项目、不能激活任何服务。\n"
            "- 所有实际操作由系统在后台通过关键词检测自动执行, 执行结果会以「📋 操作结果」的形式追加在你的回复之后。\n"
            "- 绝对禁止在回复中声称操作已执行、已完成、已创建。例如不能说「已创建PM Agent」「任务已下发」。\n"
            "- 当 Boss 要求执行操作时, 你只需回复确认和理解, 例如「收到, 系统正在处理您的指令」。\n"
            "- 如果 Boss 的指令不够明确, 引导 Boss 补充信息, 但不要假装已经执行。\n\n"
            "# 工作站架构概要\n"
            "- Station Director: 基础设施管理入口, 提供 Web UI 和 UDP 发现\n"
            "- Worker: 计算节点, 执行 PM Agent 分配的子任务\n"
            "- Secretary: 项目管理层, 同进程激活后加载聊天/模型路由/MCP工具\n"
            "- PM Agent: 项目经理, 在 Worker 上运行, 管理团队和子 Agent\n"
            "- 技能库: skills/ 目录下的 SKILL.md 文件, 定义 Agent 能力\n"
            "- 主机通讯: 支持 P2P 聊天和文件传输\n\n"
            f"# 当前工作站实时状态\n{status_context}"
        )

    def _build_prompt(self, message: str, history: list) -> str:
        """拼接对话历史 + 用户消息为 prompt。"""
        if not history:
            return message

        # 将历史对话格式化为文本
        lines = []
        for msg in history[-20:]:  # 最近 20 条
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                lines.append(f"Boss: {content}")
            else:
                lines.append(f"秘书: {content}")

        lines.append(f"Boss: {message}")
        return "\n".join(lines)

    def _detect_action(self, message: str) -> str:
        """检测用户消息中的操作意图。

        Returns:
            操作类型字符串, 无意图则返回空字符串
        """
        msg_lower = message.lower()
        for keyword, action in _ACTION_KEYWORDS.items():
            if keyword in message or keyword in msg_lower:
                return action
        return ""

    def _execute_action(self, action: str, message: str) -> str:
        """执行检测到的操作。

        Args:
            action: 操作类型
            message: 原始用户消息

        Returns:
            操作结果描述 (字符串)
        """
        try:
            if action == "query_status":
                return self._action_query_status()
            elif action == "query_hosts":
                return self._action_query_hosts()
            elif action == "query_tasks":
                return self._action_query_tasks()
            elif action == "query_progress":
                return self._action_query_progress()
            elif action == "activate_secretary":
                return self._action_activate_secretary()
            elif action == "deactivate_secretary":
                return self._action_deactivate_secretary()
            elif action == "submit_task":
                return self._action_submit_task(message)
            elif action == "cancel_task":
                return self._action_cancel_task(message)
            elif action == "pause_task":
                return self._action_pause_task(message)
            elif action == "respond_to_pm":
                return self._action_respond_to_pm(message)
            elif action == "accept_delivery":
                return self._action_accept_delivery(message)
            elif action == "reject_delivery":
                return self._action_reject_delivery(message)
            elif action == "create_project":
                return self._action_create_project(message)
            return ""
        except Exception as e:
            return f"操作执行失败: {e}"

    # ── 操作实现 ──────────────────────────────────────────────────

    def _parse_task_from_message(self, message: str) -> dict:
        """从用户消息中提取任务名称和描述 (LLM 解析)。"""
        extract_prompt = (
            "从以下用户消息中提取任务信息。返回严格 JSON 格式: "
            "{\"name\": \"简短任务名称(10字以内)\", \"description\": \"详细任务描述\"}。\n"
            "如果描述太模糊无法提取有效信息, 返回 {\"name\": \"\", \"description\": \"\"}。\n\n"
            f"消息: {message}"
        )
        resp = self.runtime._call_llm_with_routing(
            extract_prompt,
            {"_model_preference": "", "_fallback_models": []},
        )
        content = resp.get("content", "")
        try:
            # 提取 JSON 部分
            import json
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except Exception:
            pass
        return {"name": message[:20], "description": message}

    def _action_submit_task(self, message: str) -> str:
        """从对话直接提交任务并分配 PM Agent (优化13: 支持优先级)。"""
        task_info = self._parse_task_from_message(message)
        name = task_info.get("name", "")
        description = task_info.get("description", "")

        if not name or len(description) < 5:
            return (
                "请提供更详细的任务信息, 例如:\n"
                "「提交一个代码审查任务: 检查 xxx 项目的安全漏洞」\n"
                "可选: 加「优先」或「紧急」提高优先级"
            )

        # 优化13: 从消息中推断优先级
        priority = "normal"
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in ["紧急", "urgent", "立刻", "马上"]):
            priority = "urgent"
        elif any(kw in msg_lower for kw in ["优先", "重要", "high", "尽快"]):
            priority = "high"
        elif any(kw in msg_lower for kw in ["不急", "低优先", "low", "有空"]):
            priority = "low"

        result = self.controller.submit_task_from_chat(
            name=name,
            description=description,
            created_by="secretary",
            priority=priority,
        )

        # 优化14: 查询任务记忆, 提供历史参考
        memory_hint = self._get_task_memory_hint(name, description)

        status = result.get("status", "unknown")
        if status == "failed":
            error = result.get("output_data", {}).get("error", "未知错误")
            return f"任务创建失败: {error}"
        elif status == "running":
            pm_id = result.get("pm_agent_id", "")[:8]
            priority_label = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}.get(priority, priority)
            msg = (
                f"✅ 任务已创建并分配 PM Agent!\n"
                f"- 任务ID: {result.get('task_id', '')}\n"
                f"- 名称: {name}\n"
                f"- 优先级: {priority_label}\n"
                f"- PM Agent: {pm_id}\n"
                f"- 状态: 运行中"
            )
            if memory_hint:
                msg += f"\n\n💡 历史参考: {memory_hint}"
            return msg
        return f"任务已创建: {name} (状态: {status})"

    def _action_create_project(self, message: str) -> str:
        """从对话创建项目 (BUG-026: 秘书幻觉修复配套)。"""
        pm = getattr(self.controller, "project_manager", None)
        if pm is None:
            return "项目创建失败: Secretary 未激活或项目管理器不可用"

        # 复用 LLM 解析提取项目名称和描述
        info = self._parse_task_from_message(message)
        name = info.get("name", "")
        description = info.get("description", "")

        if not name or len(description) < 5:
            return (
                "请提供更详细的项目信息, 例如:\n"
                "「创建一个项目: 股票自动交易系统, 预算10美元」"
            )

        try:
            project = pm.create_project(
                name=name,
                description=description,
                budget_limit_usd=10.0,
                allowed_models=[],
                routing_strategy="balanced",
                workspace_base="",
            )
            return (
                f"✅ 项目已创建!\n"
                f"- 项目ID: {project.project_id}\n"
                f"- 名称: {name}\n"
                f"- 描述: {description[:80]}"
            )
        except Exception as e:
            return f"项目创建失败: {e}"

    def _action_query_status(self) -> str:
        """查询综合状态。"""
        context = self._build_status_context()
        return context

    def _action_query_hosts(self) -> str:
        """查询主机列表。"""
        hosts = self.controller.db.list_hosts()
        if not hosts:
            return "暂无注册主机"
        lines = []
        for h in hosts:
            status = "在线" if h.online else "离线"
            name = h.device_name or h.hostname or h.device_id[:8]
            lines.append(f"  {status} | {name} | {h.ip or '-'} | 评级: {h.rating or '-'}")
        return f"主机列表 ({len(hosts)} 台):\n" + "\n".join(lines)

    def _action_query_tasks(self) -> str:
        """查询任务列表。"""
        if not self.controller.secretary_active:
            return "Secretary 未激活, 无法查询任务"
        tasks = self.controller.db.list_tasks(limit=10)
        if not tasks:
            return "暂无任务"
        lines = []
        for t in tasks:
            pm_info = f" PM: {t.pm_agent_id[:8]}" if t.pm_agent_id else ""
            lines.append(f"  [{t.status}] {t.name}{pm_info}")
        return f"最近任务 ({len(tasks)} 个):\n" + "\n".join(lines)

    def _action_query_progress(self) -> str:
        """查询 PM Agent 进度 (优化11: LLM 智能摘要)。"""
        if not self.controller.secretary_active:
            return "Secretary 未激活"
        try:
            pms = self.controller.db.list_pm_agents()
            if not pms:
                return "暂无 PM Agent"

            # 收集原始数据
            raw_lines = []
            for pm in pms:
                reports = self.controller.db.get_progress_reports(pm.pm_id, limit=3)
                latest_progress = reports[0]["progress"] if reports else 0.0
                latest_msg = reports[0]["message"] if reports else ""
                raw_lines.append(
                    f"- {pm.agent_name} | 状态: {pm.status} | "
                    f"进度: {latest_progress*100:.0f}% | "
                    f"模式: {pm.collaboration_mode or '-'} | "
                    f"最新消息: {latest_msg[:100]}"
                )

            raw_text = "\n".join(raw_lines)

            # 优化11: 用 LLM 生成自然语言摘要
            summary_prompt = (
                "你是 LAN Mesh 工作站的秘书。请用简洁的中文 (2-3句话) 向 Boss 汇报以下 PM Agent 进度。\n"
                "要求: 突出整体进展、异常情况和预计剩余时间。不要逐条列出, 用自然段落。\n\n"
                f"PM Agent 数据:\n{raw_text}"
            )
            try:
                resp = self.runtime._call_llm_with_routing(
                    summary_prompt,
                    {"_model_preference": "", "_fallback_models": []},
                )
                summary = resp.get("content", "").strip()
                if summary and len(summary) > 10:
                    return f"📊 进度汇报:\n{summary}\n\n--- 原始数据 ---\n{raw_text}"
            except Exception:
                pass

            # LLM 失败时回退到原始格式
            return f"PM Agent 进度 ({len(pms)} 个):\n{raw_text}"
        except Exception as e:
            return f"进度查询失败: {e}"

    def _action_activate_secretary(self) -> str:
        """激活 Secretary。"""
        if self.controller.secretary_active:
            return "Secretary 已处于激活状态"
        result = self.controller.activate_secretary()
        return result.get("message", "Secretary 激活结果未知")

    def _action_deactivate_secretary(self) -> str:
        """停用 Secretary。"""
        if not self.controller.secretary_active:
            return "Secretary 当前未激活"
        result = self.controller.deactivate_secretary()
        return result.get("message", "Secretary 停用结果未知")

    # ── 优化8: 取消/暂停任务操作 ──

    def _action_cancel_task(self, message: str) -> str:
        """取消指定任务。"""
        task_id = self._extract_task_id_from_message(message)
        if not task_id:
            # 尝试从消息中找任务名称
            tasks = self.controller.db.list_tasks(status="running", limit=10)
            if not tasks:
                return "当前没有运行中的任务可以取消"
            # 模糊匹配任务名
            for t in tasks:
                if t.name and t.name in message:
                    task_id = t.task_id
                    break
            if not task_id:
                task_list = "\n".join([f"  - [{t.status}] {t.name} (ID: {t.task_id[:12]})" for t in tasks[:5]])
                return f"请指定要取消的任务。当前运行中的任务:\n{task_list}\n\n示例: 取消任务 {tasks[0].name if tasks else 'XXX'}"

        result = self.controller.cancel_task(task_id)
        if result.get("ok"):
            return f"✅ {result.get('message', '任务已取消')}"
        return f"❌ 取消失败: {result.get('message', '未知错误')}"

    def _action_pause_task(self, message: str) -> str:
        """暂停指定任务。"""
        task_id = self._extract_task_id_from_message(message)
        if not task_id:
            tasks = self.controller.db.list_tasks(status="running", limit=10)
            if not tasks:
                return "当前没有运行中的任务可以暂停"
            for t in tasks:
                if t.name and t.name in message:
                    task_id = t.task_id
                    break
            if not task_id:
                task_list = "\n".join([f"  - [{t.status}] {t.name} (ID: {t.task_id[:12]})" for t in tasks[:5]])
                return f"请指定要暂停的任务。当前运行中的任务:\n{task_list}\n\n示例: 暂停任务 {tasks[0].name if tasks else 'XXX'}"

        result = self.controller.pause_task(task_id)
        if result.get("ok"):
            return f"⏸️ {result.get('message', '任务已暂停')}"
        return f"❌ 暂停失败: {result.get('message', '未知错误')}"

    def _extract_task_id_from_message(self, message: str) -> str:
        """从消息中提取 task_id (格式: task-xxxx)。"""
        import re
        match = re.search(r'task-[a-f0-9]{12}', message, re.IGNORECASE)
        return match.group(0) if match else ""

    # ── 优化7: 回复 PM 决策 ──

    def _action_respond_to_pm(self, message: str) -> str:
        """向等待决策的 PM Agent 回复 Boss 的决策。

        支持两种方式:
        1. 「告诉PM PM-abc 用方案A」→ 精确指定 PM
        2. 如果只有一个 PM 在等待决策, 可直接用「用方案A」
        """
        pm_id = self._extract_pm_id_from_message(message)

        # 如果没有明确指定 PM, 使用最近的 awaiting PM
        if not pm_id:
            # 刷新 _last_awaiting_pm
            try:
                pm_agents = self.controller.db.list_pm_agents()
                awaiting = [p for p in pm_agents if p.status == "awaiting_input"]
                if awaiting:
                    pm_id = awaiting[-1].pm_id
                    self._last_awaiting_pm = pm_id
            except Exception:
                pass

        if not pm_id:
            pm_id = self._last_awaiting_pm

        if not pm_id:
            return "当前没有 PM Agent 在等待您的决策。"

        # 提取回复内容 (去掉"告诉PM xxx"前缀)
        response_text = message
        for prefix in ["告诉PM", "告知PM", "回复PM"]:
            if prefix in message:
                # 找到前缀后的内容
                idx = message.index(prefix) + len(prefix)
                # 跳过 PM ID 部分
                after_prefix = message[idx:].strip()
                # 如果以 PM ID 开头, 去掉它
                if after_prefix.startswith("PM-") or after_prefix.startswith("pm-"):
                    parts = after_prefix.split(None, 1)
                    response_text = parts[1] if len(parts) > 1 else ""
                else:
                    response_text = after_prefix
                break

        if not response_text.strip():
            return "请提供您的决策内容。示例: 告诉PM PM-abc 用方案A"

        # 获取 PM 信息
        pm = self.controller.db.get_pm_agent(pm_id)
        if not pm:
            return f"PM Agent {pm_id[:12]} 不存在"

        result = self.controller.inject_input_to_pm(pm_id, {
            "response": response_text,
            "choice": response_text,
        })

        if result.get("ok"):
            return f"✅ 已将您的决策发送给 {pm.agent_name}: {response_text[:100]}"
        return f"❌ 发送失败: {result.get('message', '未知错误')}"

    def _extract_pm_id_from_message(self, message: str) -> str:
        """从消息中提取 pm_id (格式: pm-xxxx 或 PM-xxxx)。"""
        import re
        match = re.search(r'[Pp][Mm]-[a-f0-9]{12}', message)
        return match.group(0) if match else ""

    # ── 优化9: 验收/退回交付物 ──

    def _action_accept_delivery(self, message: str) -> str:
        """验收最近交付的任务。"""
        task = self._find_latest_delivered_task()
        if not task:
            return "当前没有待验收的任务交付物。"

        task_id = task.task_id
        delivery = task.output_data.get("_delivery", {})
        delivery["accepted"] = True
        delivery["accepted_at"] = time.time()
        task.output_data["_delivery"] = delivery
        task.status = "completed"
        self.controller.db.save_task(task)

        return (
            f"✅ 已验收任务「{task.name}」的交付物。\n"
            f"任务状态已更新为 completed。"
        )

    def _action_reject_delivery(self, message: str) -> str:
        """退回最近交付的任务, 附带修改意见。"""
        task = self._find_latest_delivered_task()
        if not task:
            return "当前没有待退回的任务交付物。"

        task_id = task.task_id
        delivery = task.output_data.get("_delivery", {})
        delivery["accepted"] = False
        delivery["rejected_at"] = time.time()

        # 提取退回原因
        reason = message
        for kw in ["退回", "重新做"]:
            if kw in message:
                idx = message.index(kw) + len(kw)
                reason = message[idx:].strip()
                break
        if not reason:
            reason = "Boss 未提供具体修改意见"

        delivery["reject_reason"] = reason
        task.output_data["_delivery"] = delivery
        task.status = "running"  # 重新运行
        self.controller.db.save_task(task)

        # 通知 PM Agent (如果还在运行)
        pm_id = task.pm_agent_id
        if pm_id:
            self.controller.inject_input_to_pm(pm_id, {
                "response": f"[退回] {reason}",
                "choice": "reject",
                "task_name": task.name,
            })

        return (
            f"↩️ 已退回任务「{task.name}」的交付物。\n"
            f"退回原因: {reason}\n"
            f"PM Agent 将收到修改意见并重新执行。"
        )

    def _find_latest_delivered_task(self):
        """查找最近的已交付但未验收的任务。"""
        try:
            tasks = self.controller.db.list_tasks(limit=20)
            for t in tasks:
                if t.output_data and "_delivery" in t.output_data:
                    delivery = t.output_data["_delivery"]
                    if delivery.get("accepted") is None:  # 未验收
                        return t
        except Exception:
            pass
        return None

    # ── 优化14: 任务记忆辅助 ──

    def _get_task_memory_hint(self, task_name: str, task_desc: str) -> str:
        """优化14: 查询任务记忆, 生成历史参考提示。

        当 Boss 提交新任务时, 查询同类型任务的历史记录:
        - 成功率和平均耗时
        - 推荐的协作模式
        - 常见错误预警
        """
        try:
            db = self.controller.db
            # 推断任务类型 (复用 PM Agent 的逻辑)
            from .pm_agent import ProjectManagerAgent
            task_type = ProjectManagerAgent._infer_task_type(task_name, task_desc)

            stats = db.get_task_memory_stats(task_type=task_type)
            if stats.get("total", 0) == 0:
                return ""

            hints = []
            hints.append(f"同类任务历史 {stats['total']} 次, 成功率 {stats['success_rate']*100:.0f}%")
            if stats.get("avg_duration", 0) > 0:
                mins = stats["avg_duration"] / 60
                hints.append(f"平均耗时 {mins:.1f} 分钟")
            if stats.get("recommended_mode"):
                hints.append(f"推荐模式: {stats['recommended_mode']}")
            if stats.get("common_errors"):
                top_error = stats["common_errors"][0][0]
                hints.append(f"注意: 历史常见错误「{top_error[:50]}」")

            return "; ".join(hints)
        except Exception:
            return ""
