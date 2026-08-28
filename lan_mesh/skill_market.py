"""
技能市场 — 第三方 Skill 插件浏览/安装/卸载 (F5.3 插件系统, iter-61)

架构角色:
  市场源 (skills_market/ 目录, 可配置 skill_market_dir)
    └── {skill_id}/           ← 第三方插件包 (人工放入或外部同步)
        ├── SKILL.md          ← 主内容 (含 YAML front matter, 必填)
        └── reference.md      ← 扩展参考 (可选)
    ↓ SkillMarket.list_market()   浏览可安装插件 (含 installed 标记)
    ↓ SkillMarket.install()       校验 → 白名单复制到 skills/ → 注册 DB
    ↓ SkillMarket.uninstall()     仅 origin=market 可卸载 → 删文件 + 删 DB

安全设计 (第三方内容最终会注入 Agent system prompt):
  - 包体积上限 (skill_max_size_kb, 默认 200KB): 防内容注水撑爆上下文
  - skill_id 白名单字符 (^[a-z0-9][a-z0-9-_]{0,63}$): 防路径穿越
  - front matter 必填 name: 无元数据包拒绝安装
  - 内置技能 (origin=builtin) 拒绝卸载与覆盖安装
  - 白名单复制: 仅拷贝 SKILL.md/reference.md, 忽略包内其他文件
  - 安全默认: 市场包未声明 default_access 时默认仅 ["station"],
    需显式 assign_skill 授权才会分发到 Worker/Agent prompt
"""
import re
import shutil
from pathlib import Path

import yaml

from .database import Database
from .logger import get_logger

logger = get_logger("skill_market")

# 技能 ID 白名单: 小写字母/数字/连字符/下划线, 最长 64, 防路径穿越
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-_]{0,63}$")


class SkillMarket:
    """第三方技能市场 — 浏览/安装/卸载第三方 Skill 插件。

    市场源为本地目录 (默认项目根 skills_market/), 每个子目录是一个
    插件包; 包目录可由用户从任意第三方渠道 (下载/拷贝/Git 同步) 放入,
    install 后纳入 SkillRegistry 的统一注册/权限/分发体系。
    """

    def __init__(self, db: Database, skills_dir: Path, market_dir: Path,
                 max_size_kb: int = 200):
        self.db = db
        self.skills_dir = skills_dir
        self.market_dir = market_dir
        self.max_size_kb = max(1, int(max_size_kb or 200))

    # ── 市场浏览 ──────────────────────────────────────────────────

    def list_market(self) -> list[dict]:
        """扫描市场目录, 列出可安装的第三方插件包。

        Returns:
            [{skill_id, name, description, category, tags, version,
              size_kb, installed(origin 或空), valid, error}]
        """
        results = []
        if not self.market_dir.is_dir():
            return results
        installed = {s["skill_id"]: s.get("origin", "builtin")
                     for s in self.db.list_skills()}
        for entry in sorted(self.market_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            meta = self._parse_front_matter(skill_md)
            skill_id = str(meta.get("name") or entry.name).strip()
            err = self._validate_package(entry, skill_id, meta,
                                         check_conflict=False)
            results.append({
                "skill_id": skill_id,
                "name": meta.get("name", skill_id),
                "description": meta.get("description", ""),
                "category": meta.get("category", "general"),
                "tags": meta.get("tags", []),
                "version": str(meta.get("version", "1.0")),
                "size_kb": round(self._dir_size_kb(entry), 1),
                "installed": installed.get(skill_id, ""),
                "valid": err is None,
                "error": err or "",
            })
        return results

    # ── 安装/卸载 ──────────────────────────────────────────────────

    def install(self, skill_id: str) -> dict:
        """安装第三方插件: 校验 → 白名单复制到 skills/ → 注册 DB。

        Returns:
            {ok, action: "installed"|"updated", message}
        """
        if not _SKILL_ID_RE.match(skill_id or ""):
            return {"ok": False,
                    "message": f"非法技能 ID: {skill_id!r} (仅允许小写字母/数字/连字符/下划线)"}
        pkg_dir = self.market_dir / skill_id
        skill_md = pkg_dir / "SKILL.md"
        if not skill_md.is_file():
            return {"ok": False, "message": f"市场中不存在插件包: {skill_id}"}

        meta = self._parse_front_matter(skill_md)
        err = self._validate_package(pkg_dir, skill_id, meta, check_conflict=True)
        if err:
            return {"ok": False, "message": err}

        # 白名单复制: 仅拷贝 SKILL.md / reference.md, 忽略包内其他文件
        dest_dir = self.skills_dir / skill_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_md, dest_dir / "SKILL.md")
        ref_md = pkg_dir / "reference.md"
        if ref_md.is_file():
            shutil.copy2(ref_md, dest_dir / "reference.md")

        # 注册 DB (origin=market); 安全默认: 未声明 default_access → 仅 station
        action = "updated" if self.db.get_skill(skill_id) else "installed"
        default_access = meta.get("default_access") or ["station"]
        self.db.upsert_skill(
            skill_id=skill_id,
            name=meta.get("name", skill_id),
            description=meta.get("description", ""),
            category=meta.get("category", "general"),
            tags=meta.get("tags", []),
            default_access=default_access,
            content_path=skill_id,
            version=str(meta.get("version", "1.0")),
            origin="market",
        )
        logger.info("技能市场%s: %s (origin=market)", action, skill_id)
        return {"ok": True, "action": action,
                "message": f"插件 {skill_id} 已{action} "
                           f"(默认仅 station 可用, 需显式分配角色后分发)"}

    def uninstall(self, skill_id: str) -> dict:
        """卸载第三方插件 (仅 origin=market; 内置技能受保护)。

        Returns:
            {ok, action: "uninstalled", message}
        """
        existing = self.db.get_skill(skill_id)
        if not existing:
            return {"ok": False, "message": f"技能不存在: {skill_id}"}
        if existing.get("origin", "builtin") != "market":
            return {"ok": False,
                    "message": f"内置技能 {skill_id} 受保护, 仅市场安装的技能可卸载"}

        dest_dir = self.skills_dir / skill_id
        if dest_dir.is_dir():
            shutil.rmtree(dest_dir)
        self.db.delete_skill(skill_id)
        logger.info("技能已卸载: %s", skill_id)
        return {"ok": True, "action": "uninstalled", "message": f"插件 {skill_id} 已卸载"}

    # ── 内部工具 ──────────────────────────────────────────────────

    def _validate_package(self, pkg_dir: Path, skill_id: str, meta: dict,
                          check_conflict: bool = True) -> str | None:
        """校验插件包安全性, 返回错误信息 (None = 通过)。"""
        if not _SKILL_ID_RE.match(skill_id or ""):
            return f"非法技能 ID: {skill_id!r}"
        if not meta.get("name"):
            return "SKILL.md 缺少 front matter 或 name 字段"
        size_kb = self._dir_size_kb(pkg_dir)
        if size_kb > self.max_size_kb:
            return f"包体积 {size_kb:.0f}KB 超过上限 {self.max_size_kb}KB"
        if check_conflict:
            existing = self.db.get_skill(skill_id)
            if existing and existing.get("origin", "builtin") != "market":
                return f"与内置技能冲突: {skill_id} (内置技能不可覆盖安装)"
        return None

    @staticmethod
    def _parse_front_matter(filepath: Path) -> dict:
        """解析 SKILL.md 的 YAML front matter (无/失败返回空字典)。"""
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("读取市场包失败 %s: %s", filepath, e)
            return {}
        if not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            return yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            logger.error("市场包 YAML 解析失败 %s: %s", filepath, e)
            return {}

    @staticmethod
    def _dir_size_kb(pkg_dir: Path) -> float:
        """统计包目录总大小 (KB)。"""
        total = 0
        for f in pkg_dir.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
        return total / 1024.0
