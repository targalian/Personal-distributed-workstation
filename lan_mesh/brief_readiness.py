"""Explain missing brief content and unresolved decisions before versioned handoff."""
from .project_brief import ProjectBrief, SECTIONS


def missing_text(value: str) -> bool:
    """Recognize blank fields and standalone placeholders, not semantic quality."""
    text = value.strip().strip("。.!！?？… ").casefold()
    return not text or text in {"待讨论", "待确认", "待补充", "待填写", "未填写", "tbd", "todo", "待命名项目"}


def brief_readiness(current: dict) -> dict:
    """Return deterministic, navigable advisory issues for this exact revision."""
    document = ProjectBrief.model_validate(current["document"])
    issues = []

    def add(key, message, *, section=None, node=None, field=None, priority="P1"):
        issues.append({"id": key, "message": message, "section": section, "node_id": node,
                       "field": field, "priority": priority})

    if missing_text(document.title):
        add("title", "项目名称尚未确定", field="title")
    for key in ("goals", "scope", "requirements", "acceptance"):
        if missing_text(document.sections[key]):
            add("section:" + key, SECTIONS[key] + "尚未填写", section=key, priority="P0")
    for node in document.nodes:
        if node.status != "confirmed":
            label = "仍待讨论" if node.status == "discussion" else "仍是未确认建议"
            add(f"node:{node.id}:status", f"{node.title}：{label}", node=node.id, field="status", priority=node.priority)
        if missing_text(node.owner):
            add(f"node:{node.id}:owner", f"{node.title}：缺少负责人", node=node.id, field="owner", priority=node.priority)
        field = "decision" if node.kind == "discussion" else "acceptance"
        if missing_text(getattr(node, field)):
            label = "决策结论" if field == "decision" else "验收标准"
            add(f"node:{node.id}:{field}", f"{node.title}：缺少{label}", node=node.id, field=field, priority=node.priority)
    if current.get("proposal"):
        add("proposal", "有未采纳的 AI 建议；本次下发仅包含已保存文档")
    return {"revision": current["revision"], "title": document.title, "issues": issues,
            "unconfirmed_nodes": sum(node.status != "confirmed" for node in document.nodes),
            "unresolved_discussions": sum(node.kind == "discussion" and
                                          (node.status != "confirmed" or missing_text(node.decision))
                                          for node in document.nodes),
            "has_freeform_questions": bool(document.sections["questions"].strip()),
            "notice": "仅检查缺项和确认状态；有文字不代表质量达标。自由文本讨论记录需人工复核。"}
