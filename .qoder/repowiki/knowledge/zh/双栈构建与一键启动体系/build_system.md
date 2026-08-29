## 1. 系统概述
本项目采用**双技术栈并行**的构建与部署模式：
- **LAN Mesh (Python)**: 基于 `FastAPI` 的后端服务，通过 `main.py` 统一入口管理多角色节点（Station, Worker, Resources）。
- **QuickLAN (Tauri + React)**: 基于 `Tauri 2` 的跨平台桌面应用，前端使用 `Vite + React`，后端使用 `Rust`。

项目未引入复杂的 CI/CD 流水线或容器化方案（如 Docker），而是侧重于**本地环境的一键初始化与启动**，通过脚本自动化处理依赖安装、虚拟环境配置及配置文件生成。

## 2. 核心构建流程

### 2.1 LAN Mesh (Python)
- **依赖管理**: 使用 `requirements.txt` 声明核心依赖（`fastapi`, `uvicorn`, `pydantic` 等）。
- **启动逻辑**: 
  - 统一入口为 `main.py`，通过 `argparse` 解析角色参数。
  - 支持命令行参数覆盖 `config.yaml` 中的默认配置。
- **环境初始化**: 
  - 提供 `scripts/start_workstation.bat` (Windows CMD) 和 `scripts/start_workstation.ps1` (PowerShell)。
  - **自动化步骤**: 检查 Python 版本 -> 创建/校验 `.venv` -> 安装依赖 -> 复制示例配置 (`model_pool.example.yaml` -> `model_pool.yaml`) -> 启动服务。
  - **编码处理**: 脚本中强制设置 `PYTHONUTF8=1` 以解决 Windows 下的中文编码问题。

### 2.2 QuickLAN (Tauri)
- **前端构建**: 使用 `Vite` 进行开发服务器启动 (`npm run dev`) 和生产环境打包 (`npm run build`)。
- **桌面端构建**: 使用 `Tauri CLI`。
  - 开发模式: `npm run app:dev` (自动调用 `beforeDevCommand`)。
  - 生产打包: `npm run app:build` (自动调用 `beforeBuildCommand` 并生成 NSIS 安装包)。
- **配置协同**: `tauri.conf.json` 定义了前后端通信端口（前端 1420）及打包产物（NSIS 格式，支持简体中文安装向导）。

## 3. 关键文件与约定

| 模块 | 关键文件 | 说明 |
| :--- | :--- | :--- |
| **Python 依赖** | `requirements.txt` | 定义后端运行时依赖。 |
| **统一入口** | `main.py` | 所有 Python 节点的启动器，支持 `station/worker/resources` 角色切换。 |
| **启动脚本** | `scripts/start_workstation.*` | 跨平台的一键启动脚本，封装了环境准备逻辑。 |
| **前端配置** | `quicklan-main/package.json` | 定义 Vite 脚本及 Tauri CLI 集成。 |
| **Rust 配置** | `quicklan-main/src-tauri/Cargo.toml` | 定义 Rust 依赖及 Tauri 插件。 |
| **Tauri 配置** | `quicklan-main/src-tauri/tauri.conf.json` | 定义应用元数据、窗口行为及打包策略。 |

## 4. 开发者规范

1. **环境隔离**: 严禁在全局 Python 环境中运行项目，必须通过脚本或手动创建 `.venv`。
2. **配置管理**: 
   - 禁止直接修改 `model_pool.example.yaml`，应确保 `model_pool.yaml` 存在于 `lan_mesh/` 目录下。
   - 敏感信息（如 API Key）建议通过环境变量（`DEEPSEEK_API_KEY` 等）注入，而非硬编码在配置文件中。
3. **启动方式**: 
   - Python 端推荐直接使用 `scripts/` 下的脚本启动，以确保环境一致性。
   - Tauri 端开发需同时安装 Node.js 和 Rust 工具链，并通过 `npm run app:dev` 启动联调环境。
4. **版本同步**: `package.json` 与 `Cargo.toml` 中的版本号应保持同步（当前均为 `0.1.1`）。
