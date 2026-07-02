该项目采用**多语言混合构建**与**脚本化启动**的策略，未使用统一的自动化构建工具（如 Makefile、CMake 或 CI/CD 流水线）。核心逻辑分为 Python 后端服务与 Tauri 桌面客户端两部分。

### 1. 后端服务 (Python)
- **依赖管理**：通过 `requirements.txt` 声明 FastAPI、Uvicorn 等核心库。项目推荐使用虚拟环境 (`.venv`) 隔离依赖。
- **入口点**：`main.py` 作为统一 CLI 入口，支持 `station`（主控节点）、`worker`（工作节点）和 `secretary`（秘书节点）三种角色。
- **启动方式**：提供跨平台的一键启动脚本 (`scripts/start_workstation.sh`, `.bat`, `.ps1`)。这些脚本负责环境检查、虚拟环境创建、依赖安装、配置文件初始化以及服务启动。
- **配置初始化**：启动脚本会自动将 `lan_mesh/model_pool.example.yaml` 复制为 `lan_mesh/model_pool.yaml`，引导用户填入 API Key。

### 2. 桌面客户端 (QuickLAN - Tauri)
- **技术栈**：基于 Tauri v2，前端使用 React + Vite，后端使用 Rust。
- **构建命令**：
  - 开发：`npm run app:dev` (调用 `tauri dev`)
  - 生产构建：`npm run app:build` (调用 `tauri build`)
- **打包目标**：在 Windows 平台上通过 NSIS 生成安装包 (`quicklan-main/src-tauri/tauri.conf.json`)。
- **版本同步**：前端 `package.json` 与 Rust `Cargo.toml` 均维护版本号（当前为 `0.1.1`）。

### 3. 开发者规范
- **本地开发**：后端直接运行 `python main.py station`；前端进入 `quicklan-main` 目录执行 `npm run app:dev`。
- **环境要求**：需预装 Python 3.10+、Node.js 及 Rust 工具链。
- **无 CI/CD**：目前仓库中未发现 GitHub Actions 或其他持续集成配置，构建与发布主要依赖本地手动执行。