"""Build reviewable AI changes and merge an explicit selection without breaking graphs."""
from copy import deepcopy

from .project_brief import SECTIONS, ProjectBrief

NODE_FIELDS = {
    "title": "名称", "kind": "类型", "description": "说明", "status": "确认状态",
    "priority": "优先级", "acceptance": "验收标准", "owner": "负责人",
    "x": "横坐标", "y": "纵坐标", "collapsed": "折叠状态",
}


def brief_changes(before: dict, after: dict) -> list[dict]:
    """Return independent text changes; structural graph edits form one atomic group."""
    changes = []

    def add(key: str, label: str, old: object, new: object) -> None:
        if old != new:
            changes.append({"id": key, "label": label, "before": old, "after": new})

    add("title", "项目名称", before["title"], after["title"])
    for key, label in SECTIONS.items():
        add("section:" + key, label, before["sections"][key], after["sections"][key])
    def structure(value: dict) -> tuple:
        return ([(n["id"], n["parent_id"]) for n in value["nodes"]], value["edges"])
    if structure(before) != structure(after):
        add("graph", "模块与依赖结构（整体采纳）",
            {key: before[key] for key in ("nodes", "edges")},
            {key: after[key] for key in ("nodes", "edges")})
    else:
        for old, new in zip(before["nodes"], after["nodes"]):
            for field, label in NODE_FIELDS.items():
                add(f"node:{old['id']}:{field}", f"{old['title']} · {label}", old[field], new[field])
    return changes


def apply_brief_changes(before: dict, candidate: dict, selected: list[str]) -> dict:
    """Apply only known selections, then validate the complete merged document."""
    changes = {item["id"]: item for item in brief_changes(before, candidate)}
    if len(selected) != len(set(selected)) or set(selected) - changes.keys():
        raise ValueError("包含重复或未知的变更项，请重新加载建议")
    result = deepcopy(before)
    nodes = {node["id"]: node for node in result["nodes"]}
    for key in selected:
        value = deepcopy(changes[key]["after"])
        if key == "title":
            result["title"] = value
        elif key == "graph":
            result.update(value)
        elif key.startswith("section:"):
            result["sections"][key.split(":", 1)[1]] = value
        else:
            _, node_id, field = key.split(":")
            nodes[node_id][field] = value
    return ProjectBrief.model_validate(result).model_dump()
