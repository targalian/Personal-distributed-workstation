该项目采用多语言混合构建体系，主要包含 Python 后端服务与 Tauri 桌面客户端两部分。

### 1. Python 后端 (LAN Mesh)
- **依赖管理**：使用 `requirements.txt` 声明核心依赖（FastAPI, Uvicorn, Pydantic 等）。
- **环境初始化**：通过 `scripts/setup_env.sh` (及对应的 `.bat/.ps1`) 自动化创建虚拟环境 (`.venv`)、安装依赖并初始化配置文件 (`model_pool.yaml`)。
- **启动方式**：提供统一的入口脚本 `main.py`，支持通过命令行参数指定角色（`station`, `secretary`, `worker`）。同时提供了封装好的 Shell/Batch 脚本（如 `start_station.sh`）以简化不同角色的启动流程。
- **配置覆盖**：支持通过 CLI 参数（如 `--port`, `--name`）覆盖 `config.yaml` 中的默认配置。

### 2. 前端/桌面端 (QuickLAN)
- **技术栈**：基于 Tauri v2 + React + TypeScript。
- **前端构建**：使用 Vite 进行开发 (`npm run dev`) 和打包 (`npm run build`)。
- **原生层构建**：使用 Rust (Cargo) 编译原生二进制文件。通过 `tauri.conf.json` 配置构建钩子，在打包前自动执行前端构建命令。
- **打包目标**：目前主要针对 Windows 平台配置了 NSIS 安装包生成。

### 3. 开发者规范
- **环境隔离**：所有 Python 依赖必须安装在项目根目录的 `.venv` 中，避免污染全局环境。
- **跨平台支持**：关键脚本均提供了 Linux/Mac (`.sh`) 和 Windows (`.bat/.ps1`) 版本，开发时应确保逻辑同步。
- **版本一致性**：Python 模块版本定义在 `lan_mesh/__init__.py`，Tauri 应用版本定义在 `package.json` 和 `Cargo.toml` 中，发布时需手动保持同步。