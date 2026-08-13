# Work Station

A distributed personal AI workstation — organizes multiple heterogeneous hosts on a LAN into a unified scheduling grid. A Project Manager Agent (PM Agent) drives automatic task decomposition, team formation, distributed execution, and result aggregation, all managed through a chat with the Secretary in the Web UI.

> 中文文档请见 Gitee 仓库的 master 分支。

## Project Structure

```
work_station/
├── lan_mesh/                  # Python/FastAPI distributed AI Agent mesh
│   ├── web/                   # Web UI dashboard (dark theme, 7 tab panels)
│   │   ├── templates/dashboard.html
│   │   └── static/
│   ├── station_controller.py  # Station Director main controller (unified Secretary/Worker entry)
│   ├── station_api.py         # Secretary-side API (tasks/PM/teams/chat)
│   ├── station_director.py    # Host management and rating
│   ├── secretary.py           # Secretary controller (backward compatible)
│   ├── worker.py              # Worker daemon (with embedded PM Agent support)
│   ├── pm_agent.py            # Project Manager Agent (task decomposition / team formation / progress management / result aggregation)
│   ├── chat_handler.py        # Secretary chat handler (Web chat + state injection + intent detection)
│   ├── agent_prompt.py        # Shared prompt templates and customized builders for sub-Agents
│   ├── agent_runtime.py       # Agent runtime (multi-provider LLM + skill routing + custom prompts)
│   ├── agent_card.py          # Agent capability declaration card
│   ├── model_router.py        # Model router (L1-L4 difficulty grading + weighted scoring)
│   ├── model_pool.yaml        # Model pool configuration
│   ├── project.py             # Project management and budget control
│   ├── mcp_gateway.py         # MCP tool gateway
│   ├── mcp_client.py          # MCP client
│   ├── orchestrator.py        # (deprecated, replaced by PM Agent)
│   ├── host_info.py           # Host hardware info collection
│   ├── host_rating.py         # Host rating (S/A/B/C/D)
│   ├── shared_folder.py       # Shared folder management
│   ├── discovery.py           # UDP broadcast discovery
│   ├── database.py            # SQLite persistence (hosts/tasks/PMs/teams/progress)
│   ├── protocol.py            # Data models and protocol definitions
│   ├── config.py              # Pydantic configuration loading
│   ├── tool_registry.py       # Tool registry
│   ├── preflight.py           # Pre-launch self check
│   └── api.py                 # FastAPI routing layer (Worker API + Secretary API)
├── quicklan-main/             # Tauri/React LAN file sharing desktop app
├── scripts/                   # Cross-platform one-click startup scripts
│   ├── start_workstation.bat  # Windows double-click startup
│   ├── start_workstation.ps1  # PowerShell startup (with parameters)
│   └── start_workstation.sh   # Linux/Mac startup
├── main.py                    # Unified entry point
├── config.yaml                # Runtime configuration
└── requirements.txt           # Python dependencies
```

## Core Capabilities

### PM Agent Driven Task Orchestration
- Boss submits a task via the Web UI → Secretary registers a PM Agent on a suitable work_station
- The PM uses the multi-agent-architect skill to analyze task complexity and decide the team architecture
- Autonomously decomposes the task into a subtask list and organizes dependencies (DAG topological sort)
- Creates sub-Agents or teams on suitable work_stations, each with a customized system prompt
- The PM handles simple tasks itself; for complex tasks it forms a team and dispatches work

### Sub-Agent Prompt Customization System
- **Common template**: all sub-Agents share base guidelines (identity, working norms, progress reporting protocol, self-check requirements)
- **Role templates**: 7 skill types, each with a role name, responsibilities, and quality standards
- **Dynamic generation**: the PM generates a custom prompt for each sub-Agent based on task type, team structure, and dependencies
- **Runtime updates**: the PM can adjust a sub-Agent's prompt mid-execution via `/pm/update-prompt`

### Six Production-Grade Optimizations
1. **Dependency-aware topological dispatch** — when a predecessor task completes, its result is automatically injected into the successor's input_data
2. **PM dynamic prompt adjustment** — new Worker endpoint lets the PM correct course / add context / adjust strategy mid-flight
3. **Selective skill injection** — AgentRuntime loads only skills matching required_skill, reducing token usage
4. **PM result aggregation** — after all subtasks finish, an LLM aggregates them into the final deliverable in dependency order
5. **Failure takeover strategy** — three-tier strategy when a sub-Agent fails: retry on same station → retry on another station → PM local takeover
6. **Sub-Agent self-check** — completion includes a self_check field, verified by the PM before confirmation

### Secretary Chat Interface
- Chat window in the Web UI; the Boss talks directly to the Secretary
- The Secretary injects workstation state context (online host count, active PMs, in-progress tasks)
- Intent detection: task submission, start/stop Secretary, query status/progress/hosts/tasks

### Distributed Host Management
- Station Director unified management, UDP broadcast auto-discovery, HTTP heartbeat registration
- Host hardware rating system (S/A/B/C/D); task dispatch picks the best node by rating
- Workers automatically collect local specs (CPU/memory/disk/OS/network)

### Model Router
- **L1-L4 difficulty grading**: rule-driven, based on text length, keywords, and skill type
- **Weighted scoring**: `Score = capability coverage × 0.4 + cost inverse × 0.3 + speed × 0.2 − load × 0.1`
- **Fallback chain resilience**: if the preferred model fails, automatically retries along the fallback chain
- **Multi-provider support**: DeepSeek / OpenAI / Anthropic / Qwen / Aliyun Token Plan

### Project Isolation and Budget Control
- Each project has an isolated workspace, budget quota, and model whitelist
- Automatic token metering with real-time cost tracking
- Auto-pause or switch to economy models when the budget is exceeded

### Web UI Dashboard
- Dark theme, 7 tab panels:
  - **Work Station Monitor** — host list, ratings, resource usage
  - **Task Management** — task submission, status tracking, PM Agent assignment info
  - **Secretary Chat** — chat window with the Secretary (state-aware + intent detection)
  - **Team Management** — tree view of PMs and teams (station, status, progress reports)
  - **Agent Status** — Agent cards, skills, tools
  - **MCP Tools** — tool list and configuration
  - **Project Management** — project isolation, budgets, spending records
- Real-time WebSocket push (heartbeats, task changes, PM registration, progress reports, chat replies)

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend framework | FastAPI + Uvicorn |
| Data validation | Pydantic v2 |
| Configuration | PyYAML + Pydantic |
| Persistence | SQLite |
| LLM calls | requests (OpenAI-compatible protocol) |
| Desktop app | Tauri + React + TypeScript |
| Discovery protocol | UDP broadcast |
| Communication | HTTP REST + WebSocket |
| Multi-agent decisions | multi-agent-architect skill (10-step decision framework) |

## Quick Start

### One-Click Startup (Recommended)

**Windows double-click:**

Just double-click `scripts/start_workstation.bat`. The script automatically: checks Python → creates a virtual environment → installs dependencies → copies configuration → starts the Station Director.

**PowerShell startup (with parameters):**

```powershell
# Basic startup
.\scripts\start_workstation.ps1

# Specify port and name
.\scripts\start_workstation.ps1 -Port 8080 -Name "Control Center"

# Also start a local Worker (background)
.\scripts\start_workstation.ps1 -WithWorker
```

**Linux/Mac startup:**

```bash
bash scripts/start_workstation.sh

# Specify port and name
bash scripts/start_workstation.sh --port 8080 --name "Control Center"

# Also start a local Worker
bash scripts/start_workstation.sh --with-worker
```

After startup, open `http://localhost:45470` to enter the Web UI and click "Set as Master" to activate the Secretary.

### Manual Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate
pip install -r requirements.txt
```

### Configure the Model Pool

Copy the template and fill in the API key environment variable names:

```powershell
Copy-Item lan_mesh\model_pool.example.yaml lan_mesh\model_pool.yaml
```

Set environment variables as needed:

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:OPENAI_API_KEY = "sk-xxx"
$env:ALIYUN_TOKENPLAN_API_KEY = "your-dedicated-TokenPlan-key"
```

### Start a Node

```powershell
# Station Director (master node, includes Web UI)
python main.py station

# Worker (worker node)
python main.py worker

# Backward compatible: start the Secretary directly
python main.py secretary
```

### Start Workers on Other Hosts

Repeat the installation steps above on another LAN host, then run:

```powershell
python main.py worker --name "Compute-Node-01"
```

The Worker automatically discovers the Station Director and registers.

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 45454 | UDP | Device discovery broadcast |
| 45460 | TCP | Worker HTTP API |
| 45470 | TCP | Station Director HTTP API + Web UI |

## API Overview

### Task Management
- `POST /api/tasks` — submit a task (auto-selects a work_station and registers a PM Agent)
- `GET /api/tasks` — task list
- `GET /api/tasks/{id}` — task details

### PM Agent Management
- `GET /api/pm` — list all PM Agents
- `GET /api/pm/{pm_id}` — PM details (including team structure + progress)
- `GET /api/pm/{pm_id}/teams` — teams under a PM
- `GET /api/pm/{pm_id}/progress` — PM progress report
- `POST /api/pm/{pm_id}/status` — PM status reporting (called by Workers)
- `POST /api/pm/{pm_id}/progress` — PM progress reporting (called by Workers)

### Team Management
- `GET /api/teams` — all teams
- `GET /api/teams/{team_id}` — team details

### Secretary Chat
- `POST /api/secretary/chat` — send a message ({message} → {reply, action_taken})
- `GET /api/secretary/chat/history` — chat history
- `DELETE /api/secretary/chat/history` — clear chat history (memory + DB)

### Worker PM Endpoints (called by the PM Agent)
- `POST /role/start-pm` — start a PM Agent on a Worker
- `POST /role/stop-pm` — stop a PM Agent
- `GET /role/pm-status` — PM Agent runtime status
- `POST /pm/create-subagent` — create a sub-Agent (with a custom system_prompt)
- `POST /pm/update-prompt` — dynamically update a sub-Agent's prompt
- `POST /pm/progress-report` — a sub-Agent reports progress to the PM
- `GET /pm/subagents` — sub-Agent list

### Project Management
- `POST /api/projects` — create a project (budget, model whitelist, routing policy)
- `GET /api/projects` — list all projects
- `GET /api/projects/{id}/usage` — view spending records

### Model Routing
- `POST /api/route/dry-run` — routing decision preview
- `GET /api/models` — model pool list

### Real-Time Communication
- `WS /ws` — WebSocket push (host status, task changes, PM registration, progress reports, chat replies, team updates)

## Execution Flow

```
Boss submits a task (Web UI)
  → Secretary picks the best work_station (sorted by rating)
  → POST /role/start-pm → Worker creates a PM Agent
  → PM analyzes the task (multi-agent-architect skill + LLM)
  → PM decides: execute simple tasks itself / form a team for complex tasks
  → PM creates sub-Agents (with custom prompts) + dispatches by dependency topology
  → Sub-Agents execute + report staged progress
  → PM collects progress + auto-dispatches follow-up tasks when dependencies complete
  → All done → PM aggregates results via LLM → reports to the Secretary
  → Web UI shows PM/teams/progress in real time
```

## Roadmap

| Phase | Status | Content |
|-------|--------|---------|
| Phase 1 Infrastructure | ✅ | FastAPI Worker, UDP discovery, heartbeat registration |
| Phase 2 Router | ✅ | Difficulty classifier, weighted scoring routing, fallback chain |
| Phase 3 Project Isolation | ✅ | Project directory isolation, budget counters, spending tracking |
| Phase 4 Workflow Orchestration | ✅ | Preset templates (code/document/system tasks) |
| Phase 5 Dashboard | ✅ | 7-tab multi-panel dashboard, real-time WebSocket push |
| Phase 6 PM Agent | ✅ | Project Manager Agent, team formation, sub-Agent prompt customization, chat interface |
| Phase 7 Production Optimizations | ✅ | Dependency-aware dispatch, dynamic prompts, selective injection, result aggregation, failure takeover, self-check |
| Phase 8 Enhancements | 🔄 | Direct sub-Agent communication, semantic cache, skill marketplace |

## License

MIT
