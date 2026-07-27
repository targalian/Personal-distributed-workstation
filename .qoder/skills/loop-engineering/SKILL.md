---
name: loop-engineering
description: LAN Mesh 工作站自主开发循环工作手册。定义框架全貌、开发进度、未来路线图，以及可被 Agent 定时执行的迭代任务流程。配合 /loop 命令或 Cloud Agents API 实现自动化开发闭环。
category: engineering
tags: [loop, automation, roadmap, dev-ops, self-driving]
default_access: ["station", "secretary"]
version: "1.0"
---

# LAN Mesh Loop Engineering 工作手册

## 一、框架全貌

### 1.1 系统定位

分布式个人 AI 工作站：将局域网异构主机组成统一调度网格，由 PM Agent 驱动任务拆解、团队组建、分布式执行与结果聚合。Boss 通过 Web UI / Telegram Bot / 聊天窗口下达指令，秘书 AI 解析意图并自动调度。

### 1.2 核心架构

```
┌─────────────────────────────────────────────────────────┐
│                    Station Director                       │
│  (基础设施管理: UDP发现/主机评级/Web UI/共享文件夹)        │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Secretary (同进程激活)                │   │
│  │  ChatHandler │ ModelRouter │ MCPGateway │ BotGW   │   │
│  │  ProjectMgr  │ TaskMemory  │ PeriodicReport      │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │         内嵌 Worker (本机 PM Agent)               │   │
│  │  PM Agent │ SubAgents │ AgentRuntime             │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
         │ HTTP API                    │ UDP 发现
         ▼                             ▼
┌─────────────────┐          ┌─────────────────┐
│   Worker Node   │          │   Worker Node   │
│  (远程计算节点)  │          │  (远程计算节点)  │
│  PM Agent       │          │  SubAgents      │
│  SubAgents      │          │                 │
└─────────────────┘          └─────────────────┘
```

### 1.3 模块职责

| 文件 | 职责 |
|------|------|
| `station_controller.py` | Station Director 主控: 生命周期、Secretary 激活、内嵌 PM、负载选站 |
| `station_api.py` | Secretary HTTP API: 任务/PM/团队/项目/Bot/Graph 端点 |
| `chat_handler.py` | 秘书对话: LLM 回复 + 关键词意图检测 + 操作执行 |
| `pm_agent.py` | PM Agent: 任务规划、团队组建、子任务分发、结果聚合、交付闭环 |
| `orchestrator.py` | Graph Engine: DAG 状态机、Checkpoint、断点恢复 |
| `worker.py` | Worker 节点: 接收 PM 指令、运行子 Agent |
| `database.py` | SQLite 持久化: 主机/任务/PM/团队/进度/记忆/Checkpoint |
| `bot_gateway.py` | Bot 网关: Telegram/企微推送 + 自然语言统一入口 |
| `model_router.py` | 模型路由器: 多模型池、技能路由、fallback 链 |
| `agent_runtime.py` | Agent 运行时: LLM 调用、工具执行、Prompt 管理 |
| `task.py` | TaskDAG: 有向无环图数据结构 |
| `discovery.py` | UDP 广播发现: 局域网主机自动注册 |
| `station_director.py` | 主机评级: S/A/B/C/D 五档、事件记录 |
| `skill_registry.py` | 技能库: SKILL.md 扫描、注册、分配 |
| `cloud_sync.py` | 云存储同步: S3 兼容、跨主机共享文件夹 |

### 1.4 技术栈

- **后端**: Python 3.11+, FastAPI, Uvicorn, SQLite, Pydantic v2
- **前端**: 单文件 HTML 仪表盘 (7 Tab, 深色主题, WebSocket 实时推送)
- **桌面**: Tauri + React + TypeScript (quicklan-main/, 文件共享)
- **通信**: HTTP REST + WebSocket + UDP 广播
- **AI**: 多模型路由 (OpenAI 兼容 API), MCP 工具协议
- **部署**: 跨平台 (Windows/Linux/macOS), 一键启动脚本

### 1.5 关键数据流

```
Boss 消息 → ChatHandler (意图检测)
  → submit_task → StationController.submit_task_from_chat()
    → 负载感知选站 → HTTP POST /role/start-pm → Worker
      → PM Agent.start_task()
        → _analyze_and_plan() (LLM 规划)
        → _dispatch_subtasks() (分发到子 Agent)
        → _aggregate_results() (聚合)
        → _deliver_result() → Secretary /deliver
          → WS 广播 + Bot 推送 → Boss 验收
```

---

## 二、开发进度 (已完成)

### 2.1 核心基础设施 ✅

- [x] UDP 广播发现 + 自动注册
- [x] 主机评级 (S/A/B/C/D)
- [x] 共享文件夹 + 云存储同步 (S3)
- [x] Web UI 7 Tab 仪表盘
- [x] WebSocket 实时推送
- [x] P2P 主机间聊天

### 2.2 PM Agent 架构 ✅

- [x] multi-agent-architect skill 驱动规划
- [x] 五种协作模式选型 (单体/编排者/Teams/Bus/SharedState)
- [x] 依赖感知的子任务分发
- [x] 动态 Prompt 定制 (build_subagent_prompt)
- [x] 结果聚合 + LLM 总结
- [x] 三级失败接管 (同站重试→换站→PM本地)
- [x] 自主执行原则 (禁止反问, 立即规划执行)

### 2.3 秘书系统 9 项优化 ✅

| # | 优化 | 状态 |
|---|------|------|
| P0#2 | 反向沟通通道 (PM→Secretary→Boss→PM) | ✅ |
| P0#3 | 任务取消/暂停 (跨进程) | ✅ |
| P1#5 | 交付闭环 (deliver→验收/退回) | ✅ |
| P1#7 | 失败升级 (escalated→Boss决策) | ✅ |
| P2#4 | 智能摘要 (LLM 进度汇报) | ✅ |
| P2#10 | 定期汇报 (5分钟周期 Bot 推送) | ✅ |
| P2#6 | 优先级协商 + 负载感知选站 | ✅ |
| P3#8 | 任务上下文记忆 (task_memory 表) | ✅ |
| P3#9 | Bot 统一入口 (Telegram→ChatHandler) | ✅ |

### 2.4 Graph Engine ✅

- [x] TaskDAG 有向无环图
- [x] 显式状态机 (decompose→route→dispatch→monitor→aggregate→deliver)
- [x] 自动 Checkpoint (每次状态转换持久化)
- [x] 断点恢复 API (/api/tasks/{id}/resume)
- [x] DAG 图编辑 API (GET/PUT /api/tasks/{id}/graph)
- [x] 环检测

### 2.5 内嵌 Worker ✅

- [x] Station 进程内直接运行 PM Agent (无需单独 Worker)
- [x] 本机子 Agent 创建/管理
- [x] 本机 PM 进度上报转发
- [x] /role/start-pm, /pm/create-subagent 等端点

### 2.6 秘书幻觉修复 ✅

- [x] system prompt 操作执行规则 (禁止声称已执行)
- [x] 关键词扩展 (下发/分配/创建项目)
- [x] create_project 操作实现
- [x] PM 完成/失败时同步更新任务状态

---

## 三、未来开发路线图

### Phase 1: 稳定性与可观测性 (当前优先)

| ID | 任务 | 优先级 | 预估 |
|----|------|--------|------|
| F1.1 | 结构化日志系统 (替换 print, 支持级别/文件输出) | P0 | 2h |
| F1.2 | 健康检查端点 (/health) + 自愈重启 | P0 | 1h |
| F1.3 | PM Agent 超时保护 (全局任务超时 + 子任务超时) | P0 | 2h |
| F1.4 | 错误追踪 (Sentry 或本地错误聚合) | P1 | 3h |
| F1.5 | API 请求限流 + 认证 (API Key) | P1 | 2h |

### Phase 2: 任务执行能力增强

| ID | 任务 | 优先级 | 预估 |
|----|------|--------|------|
| F2.1 | 子 Agent 工具执行 (Shell/文件读写/HTTP) | P0 | 4h |
| F2.2 | 代码执行沙箱 (Docker/venv 隔离) | P1 | 6h |
| F2.3 | 多轮对话式任务细化 (PM↔Boss 迭代需求) | P1 | 3h |
| F2.4 | 任务模板库 (预置常见任务 DAG) | P2 | 2h |
| F2.5 | 子任务结果质量验证 (生成-验证器模式) | P2 | 4h |

### Phase 3: 多机协作深化

| ID | 任务 | 优先级 | 预估 |
|----|------|--------|------|
| F3.1 | Worker 自动扩缩容 (基于任务队列深度) | P1 | 4h |
| F3.2 | 跨站文件同步 (任务产物自动分发) | P1 | 3h |
| F3.3 | PM Agent 迁移 (主机故障时自动转移) | P2 | 5h |
| F3.4 | 多 Secretary 联邦 (跨网段协作) | P3 | 8h |

### Phase 4: 智能化与自治

| ID | 任务 | 优先级 | 预估 |
|----|------|--------|------|
| F4.1 | 任务记忆 → 自动优化 (历史模式驱动规划) | P1 | 3h |
| F4.2 | 异常自愈 (检测→诊断→修复循环) | P2 | 5h |
| F4.3 | 自然语言 DAG 编辑 (Boss 口述修改图结构) | P2 | 4h |
| F4.4 | 成本感知调度 (Token 预算约束下最优分配) | P3 | 4h |

### Phase 5: 产品化

| ID | 任务 | 优先级 | 预估 |
|----|------|--------|------|
| F5.1 | Web UI 重构 (React SPA, DAG 可视化编辑器) | P1 | 8h |
| F5.2 | 多用户 + 权限 (Boss/Operator/Viewer) | P2 | 4h |
| F5.3 | 插件系统 (第三方 Skill 市场) | P3 | 6h |
| F5.4 | 移动端 App (React Native / Flutter) | P3 | 12h |

---

## 四、Loop 执行计划

### 4.1 循环结构

每次 Loop 迭代执行以下 5 个阶段：

```
┌─────────────────────────────────────────────────┐
│  Phase A: 状态检查 (Status Check)               │
│  → 读取状态文件, 确认上次进度, 检测异常          │
├─────────────────────────────────────────────────┤
│  Phase B: 任务选取 (Task Selection)             │
│  → 从路线图中选取下一个待执行任务                 │
├─────────────────────────────────────────────────┤
│  Phase C: 实施 (Implementation)                 │
│  → 编码、修改文件、创建新模块                    │
├─────────────────────────────────────────────────┤
│  Phase D: 验证 (Verification)                   │
│  → 编译检查、单元测试、集成验证                  │
├─────────────────────────────────────────────────┤
│  Phase E: 记录 (Recording)                      │
│  → 更新状态文件、提交变更、输出报告              │
└─────────────────────────────────────────────────┘
```

### 4.2 状态文件规范

状态文件路径: `项目根目录/loop_status.json`

```json
{
  "version": 1,
  "last_run": "2026-07-27T22:00:00Z",
  "current_phase": "F1.1",
  "phase_status": "in_progress",
  "completed": ["F1.2", "F1.3"],
  "failed": [],
  "blocked": [],
  "next_tasks": ["F1.4", "F1.5"],
  "iteration_count": 3,
  "total_files_modified": 12,
  "last_error": "",
  "notes": "结构化日志已完成基础框架, 下一步替换 pm_agent.py 中的 print"
}
```

### 4.3 每次迭代的具体指令

**Phase A: 状态检查**
```
1. 读取 项目根目录/loop_status.json
2. 如果文件不存在 → 初始化 (首次运行)
3. 检查 last_error → 如果有未解决错误, 优先修复
4. 检查 blocked 列表 → 尝试解除阻塞
5. 运行 `python -c "import py_compile; ..."` 确认代码库健康
```

**Phase B: 任务选取**
```
1. 从 §三 路线图中按优先级选取:
   - P0 > P1 > P2 > P3
   - 同优先级按 ID 顺序
2. 跳过 completed 和 blocked 中的任务
3. 如果所有 P0-P1 完成, 进入 P2
4. 记录选取的任务到 current_phase
```

**Phase C: 实施**
```
1. 阅读任务描述, 理解目标
2. 定位相关文件 (参考 §1.3 模块职责表)
3. 实施修改:
   - 遵循项目命名规范 (小写_下划线.py, PascalCase 类)
   - 添加类型标注
   - 关键路径添加 print(f"[模块名] ...") 日志
   - 新增 API 端点需同时更新 station_api.py
4. 如涉及 DB 变更, 使用 ALTER TABLE + try/except 兼容模式
```

**Phase D: 验证**
```
1. 编译检查:
   python -c "import py_compile; files=[...]; [py_compile.compile(f, doraise=True) for f in files]"
2. 导入检查:
   python -c "from lan_mesh.station_controller import StationController; print('OK')"
3. 如果修改了 API:
   检查路由注册、参数校验、错误处理
4. 如果修改了 DB:
   检查表创建、兼容性、索引
```

**Phase E: 记录**
```
1. 更新 loop_status.json:
   - current_phase → completed
   - 更新 next_tasks
   - 记录 iteration_count++
2. 输出本次迭代报告:
   - 修改了哪些文件
   - 实现了什么功能
   - 遗留问题
3. 如果发现新 Bug → 加入 blocked 或创建新任务
```

### 4.4 调度配置

**通过 /loop 命令 (推荐):**
```
/loop --interval 30m --skill loop-engineering --max-iterations 50
```

**通过 Cloud Agents API:**
```yaml
schedule:
  cron: "*/30 * * * *"  # 每30分钟
  max_concurrent: 1
  timeout: 600s

task:
  skill: loop-engineering
  context:
    - read: loop_status.json
    - read: lan_mesh/  # 源码目录
  output:
    - write: loop_status.json
    - write: loop_reports/  # 迭代报告
```

---

## 五、监控与迭代

### 5.1 健康指标

| 指标 | 正常范围 | 告警阈值 | 检测方式 |
|------|----------|----------|----------|
| 编译通过率 | 100% | <100% | py_compile |
| 导入成功率 | 100% | <100% | import 测试 |
| API 响应时间 | <500ms | >2s | /health 端点 |
| PM 任务成功率 | >70% | <50% | task_memory 统计 |
| 内存占用 | <500MB | >1GB | psutil |
| 磁盘使用 | <80% | >90% | shutil.disk_usage |

### 5.2 日志规范

```python
# 当前: print 输出 (待迁移到结构化日志)
print(f"[Station] 任务已创建: {task_id}")
print(f"[PM {pm_id[:8]}] 聚合完成")
print(f"[BotGateway] Telegram 轮询异常: {e}")

# 目标: 结构化日志 (F1.1 任务)
import logging
logger = logging.getLogger("lan_mesh.station")
logger.info("任务已创建", extra={"task_id": task_id})
logger.error("聚合失败", exc_info=True)
```

### 5.3 回滚策略

每次 Loop 迭代前:
1. 确认 git 工作区干净 (`git status`)
2. 如果上次迭代失败 → `git diff` 检查残留修改
3. 严重错误 → `git checkout -- .` 回滚到上次稳定状态
4. 记录回滚原因到 loop_status.json.notes

### 5.4 迭代优化规则

- **连续 2 次失败** → 降低任务优先级, 标记为 blocked, 跳过
- **编译错误** → 立即修复, 不进入下一个任务
- **新发现的 Bug** → 如果影响 P0 功能, 插入到当前迭代; 否则记录到路线图
- **单次迭代超时 (>10min)** → 保存进度, 标记为 in_progress, 下次继续

---

## 六、约束与守则

### 6.1 编码守则

1. **向后兼容**: 新增列用 `ALTER TABLE + try/except`, 新增端点不修改已有路径
2. **线程安全**: DB 操作通过 `_get_conn()` 获取线程本地连接
3. **错误隔离**: 所有 HTTP 调用包裹 `try/except`, 超时设为 10s
4. **日志必须**: 关键路径 (任务创建/完成/失败) 必须有 print 输出
5. **类型标注**: 公共方法参数和返回值必须有类型标注
6. **Pydantic v2**: 数据模型使用 `model_validator` 而非 `validator`

### 6.2 禁止事项

- ❌ 不得删除已有 API 端点
- ❌ 不得修改 DB 表的主键结构
- ❌ 不得在循环中执行 `rm`/`del` 等破坏性操作
- ❌ 不得硬编码 API Key 或密码
- ❌ 不得修改 `.git/hooks` 或 git config
- ❌ 不得跳过编译验证直接进入下一个任务

### 6.3 文件修改范围

每次迭代优先修改的文件 (按影响范围排序):
1. `database.py` — 如需新表/新列
2. `pm_agent.py` — 如需增强 PM 能力
3. `station_controller.py` — 如需新增控制逻辑
4. `station_api.py` — 如需新增 API 端点
5. `chat_handler.py` — 如需新增对话能力
6. `bot_gateway.py` — 如需新增推送事件
7. `worker.py` / `api.py` — 如需 Worker 端配合

---

## 七、快速参考

### 启动命令

```bash
# Station Director (推荐入口)
python main.py station

# Worker 节点
python main.py worker

# 指定端口
python main.py station --port 8080
```

### 关键 API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/secretary/chat` | POST | 秘书对话 |
| `/api/tasks` | GET/POST | 任务 CRUD |
| `/api/pm` | GET | PM Agent 列表 |
| `/api/pm/{id}/deliver` | POST | 交付物上报 |
| `/api/pm/{id}/task-memory` | POST | 任务记忆 |
| `/api/pm/{id}/inject-input` | POST | 注入 Boss 回复 |
| `/api/tasks/{id}/cancel` | POST | 取消任务 |
| `/api/tasks/{id}/graph` | GET/PUT | DAG 图 |
| `/api/tasks/{id}/resume` | POST | 断点恢复 |
| `/api/bot/message` | POST | Bot 消息入口 |
| `/role/start-pm` | POST | 本机启动 PM |
| `/pm/create-subagent` | POST | 创建子 Agent |

### 编译验证命令

```powershell
cd e:\ingobj\work_station
python -c "import py_compile; files=['lan_mesh/pm_agent.py','lan_mesh/station_controller.py','lan_mesh/station_api.py','lan_mesh/chat_handler.py','lan_mesh/bot_gateway.py','lan_mesh/database.py','lan_mesh/worker.py','lan_mesh/api.py','lan_mesh/orchestrator.py']; [py_compile.compile(f, doraise=True) for f in files]; print('All OK')"
```
