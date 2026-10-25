# Yomk 技能内化（iter-112）

工作站自带三个技能，资产在 `lan_mesh/builtin_skills/`，随 wheel 分发，不依赖编码 Agent
家目录中的全局技能，也不要求运行机器能访问 GitHub。

| 技能 | 用途 | 激活条件 |
|---|---|---|
| yomkserverpy-modular | Yomk Python 服务、消息和独立工程 | Yomk 任务，排除明确 C++ 任务 |
| yomkserver-modular | Yomk C++17 服务与工程骨架 | 同时包含 Yomk 与 C++/cpp 标记 |
| lan-mesh-yomk | 工作站实例级服务、兼容门面和生命周期 | lan_mesh、工作站等工程标记 |

三个技能默认供 station、secretary、pm、worker 使用，属于知识参考，不增加工具或部署权限。
入口使用标准 name/description/metadata front matter；工作站从 metadata 读取分类、标签、
默认角色、版本与 activation，同时继续兼容旧技能的顶层字段。

## 发现与分发

SkillRegistry 合并包内技能与既有外部 skills 目录，外部同目录技能在注册内容查询中优先。
扫描不存在的外部目录时仍能注册内置技能，无需创建或修改 site-packages。源码部署继续使用
根 skills 目录；安装部署的外部技能目录回落到 `~/.lan_mesh/skills`。

原有下载包字段保留，新增 resources 字典，携带 reference.md、examples.md、upstream-skill.md、
UPSTREAM.md 和 LICENSE.txt。Worker 只写入这五个固定资源文件名，旧 Worker 仍可读正文和 reference。
上游的完整指引及示例保留在资源文件中，简明入口区分独立工程模板与工作站实例封装。

## 任务提示词

`skill_knowledge.append_builtin_guidance` 根据任务描述按 any/all/unless 规则选择内置技能。
不匹配或空任务描述不激活；只注入简明 SKILL.md 正文与资料目录，参考长文按需读取。
同一提示词通过标题标记去重；普通天气、摘要或 Python 任务不附加 Yomk 指令。

运行时没有缓存也能使用包内技能；PM 自定义角色提示词保留，相关任务追加知识。
普通 LLM 与模型路由两条路径的外部 system prompt 都经过相同的增补逻辑。
秘书调用补传当前消息作为选择上下文，避免仅有身份提示词时遗漏技能。
缓存中的内置技能不参与旧有“无匹配时全量装载”逻辑，防止把 C++ 工程模板用于无关任务。
本地自动注入使用随包维护的入口版本；分发的完整资源仍可供工具按需读取。

技能不改变执行器枚举：code_generation、code_review、react_agent 等仍负责执行，
知识技能 ID 不替代 required_skill，不自动切换后端或模型，不自动派发多 Agent。

## 验证与生效

`tests/test_builtin_skills.py` 验证离线注册、各角色可见性、下载资源、任务语言匹配、
无关任务隔离、定制提示词、模型路由与默认路由、秘书上下文和 Worker 资源落盘。
构建 wheel 后须从安装目录验证内置资产，不能只验证源码目录。

已运行的 Station 需更新代码或安装新包后重启，启动扫描即注册三个技能；Worker 沿既有发现/
注册流程拉取资源。此迭代不重启现有节点，不调用真实在线模型；真实多机验收仍待测试环境。

本轮实测：14 项新增技能行为、全量 635 passed / 137.91s；三个技能格式检查通过，
文档清单 204 项、159 文件名称检查通过；wheel 中有 13 项技能资产，
参考文档与上游对应提交一致，隔离安装后 `python -I` 验证和 `pip check` 均通过。
