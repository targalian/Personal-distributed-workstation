"""Conversation-scoped project specification; Markdown and DAG share one source.

Containment (parent_id) and dependency (edges) are deliberately distinct graphs.
Only the active Secretary writes these files; revision checks protect browser edits.
"""
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SECTIONS = {
    "background": "项目背景与问题",
    "goals": "目标与成功指标",
    "users": "用户角色与使用场景",
    "scope": "范围与非目标",
    "requirements": "项目需求与优先级",
    "quality": "非功能需求（性能／安全／可用性）",
    "solution": "解决方案与备选方案",
    "data": "数据、接口与外部依赖",
    "milestones": "里程碑与交付物",
    "resources": "预算、资源与约束",
    "risks": "风险与假设",
    "questions": "功能待讨论项与决策记录",
    "acceptance": "验收标准与验证方式",
}
STATUS_LABELS = {"proposed": "建议", "confirmed": "已确认", "discussion": "待讨论"}


class BriefNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    kind: Literal["module", "feature", "discussion"] = "module"
    parent_id: str | None = None
    description: str = Field(default="", max_length=6000)
    status: Literal["proposed", "confirmed", "discussion"] = "discussion"
    priority: Literal["P0", "P1", "P2"] = "P1"
    acceptance: str = Field(default="", max_length=3000)
    owner: str = Field(default="", max_length=160)
    x: float = Field(default=24, ge=0, le=20000, allow_inf_nan=False)
    y: float = Field(default=60, ge=0, le=20000, allow_inf_nan=False)
    collapsed: bool = False


class BriefEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str


class ProjectBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="待命名项目", min_length=1, max_length=160)
    sections: dict[str, str] = Field(default_factory=dict)
    nodes: list[BriefNode] = Field(default_factory=list, max_length=150)
    edges: list[BriefEdge] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_graph(self):
        if set(self.sections) - set(SECTIONS):
            raise ValueError("包含不支持的文档章节")
        if any(len(text) > 12000 for text in self.sections.values()):
            raise ValueError("单章节最多 12000 字")
        self.sections = {key: self.sections.get(key, "") for key in SECTIONS}
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("节点 ID 不可重复")
        ancestors = {}
        for node in self.nodes:
            chain = {node.id}
            parent = node.parent_id
            while parent is not None:
                if parent not in nodes:
                    raise ValueError("父节点不存在")
                if parent in chain:
                    raise ValueError("包含关系不能成环")
                chain.add(parent)
                if len(chain) > 3:
                    raise ValueError("框图最多支持三层嵌套")
                parent = nodes[parent].parent_id
            ancestors[node.id] = chain - {node.id}
        adjacency = {key: [] for key in nodes}
        indegree = dict.fromkeys(nodes, 0)
        seen = set()
        for edge in self.edges:
            a, b = edge.source, edge.target
            if a not in nodes or b not in nodes:
                raise ValueError("依赖边引用了不存在的节点")
            if a == b or (a, b) in seen:
                raise ValueError("不允许自连或重复依赖")
            if a in ancestors[b] or b in ancestors[a]:
                raise ValueError("父子包含关系不应再作为执行依赖")
            seen.add((a, b))
            adjacency[a].append(b)
            indegree[b] += 1
        queue = [key for key, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            key = queue.pop()
            visited += 1
            for target in adjacency[key]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(nodes):
            raise ValueError("依赖关系必须是 DAG，不能形成循环")
        return self


def render_markdown(brief: ProjectBrief, revision: int = 0) -> str:
    def inline(text):
        return text.replace("\n", " ").replace("\r", " ")

    lines = [f"# {inline(brief.title)}", "", f"> 项目描述 · 版本 {revision} · 讨论草稿（不代表任务已下发）", ""]
    for key, label in SECTIONS.items():
        lines += [f"## {label}", "", brief.sections[key].strip() or "待讨论", ""]
    lines += ["## 模块与功能分解", ""]

    def emit_nodes(parent=None, depth=0):
        for node in brief.nodes:
            if node.parent_id != parent:
                continue
            pad = "  " * depth
            lines.append(f"{pad}- **{inline(node.title)}** (`{node.id}`) · {node.priority} · {STATUS_LABELS[node.status]}")
            for label, value in [("说明", node.description), ("验收", node.acceptance), ("负责人", node.owner)]:
                if value:
                    lines.append(f"{pad}  - {label}：{inline(value)}")
            emit_nodes(node.id, depth + 1)

    emit_nodes()
    if not brief.nodes:
        lines.append("待讨论")
    lines += ["", "## 依赖 DAG", "", "箭头 A → B 表示 B 依赖 A；框内嵌套表示包含关系，最多三层。", "", "```mermaid", "flowchart LR"]
    # Mermaid IDs are generated, labels encoded; user content cannot terminate the fence.
    ids = {node.id: f"n{i}" for i, node in enumerate(brief.nodes)}

    def label(text):
        return "".join(ch if ch.isalnum() or ch == " " else f"#{ord(ch)};" for ch in inline(text))

    def emit_graph(parent=None, depth=1):
        for node in brief.nodes:
            if node.parent_id != parent:
                continue
            prefix = "  " * depth
            if any(child.parent_id == node.id for child in brief.nodes):
                lines.append(f'{prefix}subgraph {ids[node.id]}["{label(node.title)}"]')
                emit_graph(node.id, depth + 1)
                lines.append(prefix + "end")
            else:
                lines.append(f'{prefix}{ids[node.id]}["{label(node.title)}"]')

    emit_graph()
    if not brief.nodes:
        lines.append('  empty["待讨论"]')
    for edge in brief.edges:
        lines.append(f"  {ids[edge.source]} --> {ids[edge.target]}")
    lines += ["```", "", "## 追踪说明", "", "本文件由项目描述工作区导出。章节、节点与依赖共用一份数据；在工作区保存后重新导出即可同步。", ""]
    return "\n".join(lines)


class RevisionConflict(ValueError):
    pass


class BriefStore:
    def __init__(self, root):
        self.root = Path(root) / "project_briefs"
        self.lock = threading.RLock()

    def _path(self, conv_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", conv_id):
            raise ValueError("非法对话 ID")
        return self.root / conv_id / "brief.json"

    def get(self, conv_id):
        with self.lock:
            path = self._path(conv_id)
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                brief = ProjectBrief.model_validate(value["document"])
            else:
                brief = ProjectBrief()
                value = {"revision": 0, "updated_at": None, "document": brief.model_dump()}
            value.pop("_history", None)
            return {**value, "proposal": value.get("proposal"),
                    "markdown": render_markdown(brief, value["revision"]), "section_labels": SECTIONS}

    def save(self, conv_id, document, expected_revision):
        brief = ProjectBrief.model_validate(document)
        with self.lock:
            previous = self.get(conv_id)
            if expected_revision != previous["revision"]:
                raise RevisionConflict("项目描述已更新，请保留当前修改并重新加载后合并")
            value = {"revision": previous["revision"] + 1, "updated_at": time.time(), "document": brief.model_dump()}
            value["proposal"] = previous.get("proposal")
            self._write(conv_id, value, source="manual")
            return self.get(conv_id)

    def propose(self, conv_id: str, document: dict, expected_revision: int) -> dict:
        """Persist a candidate without changing the saved document or its revision."""
        from .brief_review import brief_changes
        candidate = ProjectBrief.model_validate(document).model_dump()
        with self.lock:
            current = self.get(conv_id)
            if current["revision"] != expected_revision:
                raise RevisionConflict("项目描述已更新，请重新加载后整理")
            if current["proposal"]:
                raise RevisionConflict("已有待审阅建议，请先采纳或忽略，原建议已保留")
            changes = brief_changes(current["document"], candidate)
            if not changes:
                return current
            current["proposal"] = {
                "id": uuid.uuid4().hex, "base_revision": expected_revision,
                "created_at": time.time(), "document": candidate, "changes": changes,
            }
            self._write(conv_id, current)
            return self.get(conv_id)

    def review(self, conv_id: str, proposal_id: str, expected_revision: int,
               selected: list[str]) -> dict:
        """Atomically resolve the proposal; an empty selection discards it, even if stale."""
        from .brief_review import apply_brief_changes
        with self.lock:
            current = self.get(conv_id)
            proposal = current["proposal"]
            if current["revision"] != expected_revision or not proposal or proposal["id"] != proposal_id:
                raise RevisionConflict("版本或待审阅建议已变化，请重新加载后审阅")
            if selected:
                if proposal["base_revision"] != current["revision"]:
                    raise RevisionConflict("建议基于旧版本，请忽略后重新整理；当前文档已保留")
                current["document"] = apply_brief_changes(current["document"], proposal["document"], selected)
                current["revision"] += 1
                current["updated_at"] = time.time()
            current["proposal"] = None
            self._write(conv_id, current, source="ai_review" if selected else "")
            return self.get(conv_id)

    def history(self, conv_id: str, before_revision: int | None = None, limit: int = 50) -> dict:
        """List committed snapshots newest first, without loading document bodies."""
        with self.lock:
            path = self._path(conv_id)
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            entries = self._history_entries(raw)
            if before_revision is not None:
                entries = [entry for entry in entries if entry["revision"] < before_revision]
            entries = sorted(entries, key=lambda entry: entry["revision"], reverse=True)
            page = entries[:limit]
            return {"revisions": [{key: value for key, value in entry.items() if key != "file"} for entry in page],
                    "next_before": page[-1]["revision"] if len(entries) > limit else None}

    def revision(self, conv_id: str, revision: int) -> dict:
        """Read a committed snapshot; unpublished files are never exposed as history."""
        with self.lock:
            path = self._path(conv_id)
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            entry = next((item for item in self._history_entries(raw) if item["revision"] == revision), None)
            if entry is None:
                raise KeyError("历史版本不存在")
            snapshot = json.loads((path.parent / "revisions" / entry["file"]).read_text(encoding="utf-8")) if entry.get("file") else raw
            brief = ProjectBrief.model_validate(snapshot["document"])
            return {"revision": revision, "document": brief.model_dump(), "updated_at": entry["updated_at"],
                    "source": entry["source"], "markdown": render_markdown(brief, revision)}

    def restore(self, conv_id: str, revision: int, expected_revision: int) -> dict:
        """Restore old content as a new revision, preserving all committed snapshots."""
        with self.lock:
            current = self.get(conv_id)
            if current["revision"] != expected_revision:
                raise RevisionConflict("当前文档已更新，请重新比较后恢复")
            snapshot = self.revision(conv_id, revision)
            current.update(document=snapshot["document"], revision=current["revision"] + 1, updated_at=time.time())
            self._write(conv_id, current, source="restore", restored_from=revision)
            return self.get(conv_id)

    @staticmethod
    def _history_entries(raw: dict) -> list[dict]:
        entries = list(raw.get("_history", []))
        if not entries and raw.get("revision", 0) > 0:
            entries.append({"revision": raw["revision"], "updated_at": raw.get("updated_at"),
                            "source": "legacy", "title": raw["document"]["title"]})
        return entries

    def _write(self, conv_id: str, value: dict, source: str = "", restored_from: int | None = None) -> None:
        """Prepare snapshots, then atomically publish document, proposal and history index."""
        stored = {key: value[key] for key in ("revision", "updated_at", "document", "proposal")}
        path = self._path(conv_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entries = self._history_entries(raw)
        if value["revision"] != raw.get("revision", 0):
            if entries and not entries[-1].get("file"):
                entries[-1] = self._snapshot(path, raw, "legacy")
            entries.append(self._snapshot(path, stored, source or "manual", restored_from))
        stored["_history"] = entries
        self._atomic_write(path, json.dumps(stored, ensure_ascii=False, indent=2))

    def _snapshot(self, path: Path, value: dict, source: str, restored_from: int | None = None) -> dict:
        folder = path.parent / "revisions"
        folder.mkdir(exist_ok=True)
        filename = f"{value['revision']}-{uuid.uuid4().hex}.json"
        snapshot = {key: value[key] for key in ("revision", "updated_at", "document")}
        snapshot.update(source=source, restored_from=restored_from)
        self._atomic_write(folder / filename, json.dumps(snapshot, ensure_ascii=False, indent=2))
        return {"revision": value["revision"], "updated_at": value["updated_at"], "source": source,
                "restored_from": restored_from, "title": value["document"]["title"], "file": filename}

    @staticmethod
    def _atomic_write(path, text):
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".brief-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def brief_instructions(current: dict, *, generate=False) -> str:
    """One LLM call yields both a concise conversation reply and a validated artifact."""
    return (
        "\n# 项目描述产物协议\n"
        "Boss 与秘书讨论项目时，应逐步沉淀项目描述。只输出 JSON 对象，不要代码围栏："
        '{"reply":"简短说明本轮变更和最多三个关键待讨论问题", "project_brief":{'
        '"title":"项目名称", "sections":{}, "nodes":[], "edges":[]}}。\n'
        "project_brief 是完整的新文档，保留已有有效内容、节点 ID、布局与 Boss 手工修改。"
        "返回的修改仅作为待审阅建议；未经 Boss 采纳，不得声称已更新或保存项目描述。"
        "仅查询状态等非项目讨论时 project_brief 设为 null。"
        + ("本次明确要求整理项目描述，必须返回完整 project_brief。" if generate else "")
        + "sections 按下列键填写中文内容，未知信息写待讨论，不可臆造预算、期限或已确认决定："
        + json.dumps(SECTIONS, ensure_ascii=False)
        + '\n节点格式：{"id":"module_1","title":"模块名","kind":"module",'
        '"parent_id":null,"description":"说明","status":"discussion","priority":"P1",'
        '"acceptance":"验收标准","owner":"待讨论","x":24,"y":60,"collapsed":false}。'
        "kind 可为 module/feature/discussion，status 可为 proposed/confirmed/discussion。"
        "仅 Boss 明确确认的内容可标 confirmed；建议标 proposed，未知标 discussion。"
        "父子最多三层（模块→功能→子功能），x/y 相对父框，新建同级节点要错开放置。"
        '依赖格式：{"source":"前置节点ID","target":"后续节点ID"}，不可成环、重复、自连或连接祖先与后代。'
        "模块和功能写入 nodes，功能待讨论项也应在 questions 中注明待决策事项、影响、负责人和结论。"
        "整理文档不是执行任务，不能声称已经创建项目、下发 PM 或保存成功。\n当前文档：\n"
        + json.dumps(current["document"], ensure_ascii=False)
    )


def parse_brief_reply(content):
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return content, None
    if not isinstance(value, dict) or not isinstance(value.get("reply"), str):
        return content, None
    return value["reply"], value.get("project_brief")
