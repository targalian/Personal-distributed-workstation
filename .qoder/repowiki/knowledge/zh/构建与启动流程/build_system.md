该项目采用多语言混合构建体系，主要包含 Python 后端（LAN Mesh）和 Tauri 桌面前端（QuickLAN）。

### 1. Python 后端 (LAN Mesh)
- **依赖管理**：使用 `requirements.txt` 声明依赖，通过 `venv` 虚拟环境隔离。核心依赖包括 `fastapi`, `uvicorn`, `pydantic` 等。
- **初始化脚本**：提供 `scripts/setup_env.sh` (及 `.bat/.ps1`) 自动化创建虚拟环境、安装依赖并复制配置模板 (`model_pool.yaml`)。
- **统一入口**：`main.py` 作为程序主入口，支持通过命令行参数指定角色（`station`, `secretary`, `worker`）及端口、名称等配置。
- **启动方式**：推荐使用 `scripts/` 目录下的封装脚本（如 `start_station.sh`）启动不同角色的节点，这些脚本会自动检测虚拟环境并传递参数。

### 2. 桌面前端 (QuickLAN)
- **技术栈**：基于 Tauri v2 + React + TypeScript + Vite。
- **构建工具**：
  - 前端：使用 `npm run build` (Vite) 编译静态资源。
  - 桌面端：使用 `tauri build` 打包为原生应用（目前配置为 Windows NSIS 安装包）。
- **配置文件**：`src-tauri/tauri.conf.json` 定义了构建钩子（`beforeBuildCommand`）、窗口属性及打包目标。
- **开发模式**：通过 `npm run app:dev` 同时启动前端开发服务器和 Tauri 后端。

### 3. 开发者规范
- **环境准备**：新成员应先运行 `bash scripts/setup_env.sh` 初始化 Python 环境。
- **配置管理**：敏感信息（如 API Key）应配置在 `lan_mesh/model_pool.yaml` 或通过环境变量注入，避免硬编码。
- **跨平台支持**：Python 部分提供了 `.sh`, `.bat`, `.ps1` 三种脚本以兼容 Linux/Mac 和 Windows 环境。