LAN Mesh 项目采用**多语言混合构建体系**，后端基于 Python，前端桌面应用基于 Tauri (Rust + React)。项目未使用统一的自动化构建工具（如 Makefile 或 CI/CD 流水线），而是依赖脚本化启动和手动环境配置。

### 1. 后端构建与运行 (Python)
- **依赖管理**：使用标准的 `requirements.txt` 管理 Python 依赖（FastAPI, Uvicorn, Pydantic 等）。
- **环境初始化**：通过 `scripts/setup_env.ps1` (Windows) 自动创建 `.venv` 虚拟环境并安装依赖。同时会复制 `model_pool.example.yaml` 为 `model_pool.yaml` 作为配置模板。
- **启动方式**：
  - 统一入口为 `main.py`，支持 `master` 和 `worker` 两种角色。
  - 提供跨平台启动脚本：`scripts/start_master.sh/.ps1` 和 `scripts/start_worker.sh/.ps1`。这些脚本负责激活虚拟环境并传递命令行参数（如端口、设备名、共享目录）。

### 2. 前端桌面应用构建 (Tauri + React)
- **技术栈**：React (Vite) + TypeScript 用于 UI，Rust (Tauri) 用于系统底层交互（文件传输、网络发现）。
- **构建工具**：
  - **前端**：使用 `vite` 进行开发和打包 (`npm run build`)。
  - **桌面端**：使用 `@tauri-apps/cli` 进行原生应用打包 (`npm run app:build`)。
- **配置**：`quicklan-main/src-tauri/tauri.conf.json` 定义了应用元数据、窗口行为及打包目标（目前仅配置了 Windows NSIS 安装包）。
- **版本同步**：`package.json` 和 `Cargo.toml` 中均硬编码了版本号 `0.1.1`，需手动保持同步。

### 3. 部署与发布
- **后端部署**：目前主要为本地局域网运行，无 Dockerfile 或 Kubernetes  manifests。生产环境部署依赖手动执行启动脚本。
- **前端发布**：通过 Tauri 构建生成 Windows `.exe` 安装程序。构建产物位于 `quicklan-main/src-tauri/target/release/bundle/nsis/`。

### 4. 开发者规范
- **环境隔离**：后端开发必须在 `.venv` 虚拟环境中进行。
- **配置管理**：敏感配置（如 API Key）应通过环境变量或本地 `model_pool.yaml` 管理，严禁提交至版本控制。
- **跨平台兼容**：后端脚本同时提供了 `.sh` (Linux/macOS) 和 `.ps1` (Windows) 版本，确保在不同宿主系统上的一致性体验。