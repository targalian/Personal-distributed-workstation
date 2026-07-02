该项目采用**双栈混合架构**，包含 Python 后端（LAN Mesh）和 Rust/React 前端桌面应用（QuickLAN），两者拥有独立的构建与依赖管理体系。

### 1. Python 后端 (LAN Mesh)
- **依赖管理**: 使用 `requirements.txt` 管理 Python 依赖（FastAPI, Uvicorn 等）。
- **环境初始化**: 通过 `scripts/setup_env.sh` (及对应的 `.bat`/`.ps1`) 自动化创建 `.venv` 虚拟环境并安装依赖。
- **启动方式**: 
  - **统一入口**: `main.py` 作为核心入口，支持 `station` (主控), `secretary` (秘书节点), `worker` (工作节点) 三种角色。
  - **脚本封装**: `scripts/start_*.sh` 提供了针对不同角色的便捷启动脚本，自动处理虚拟环境激活和参数传递。
- **配置管理**: 支持通过命令行参数覆盖 `config.yaml` 中的默认配置。

### 2. 桌面客户端 (QuickLAN)
- **技术栈**: Tauri v2 (Rust) + React (TypeScript) + Vite。
- **前端构建**: 使用 `npm run build` (Vite) 编译 React 应用。
- **后端构建**: 使用 `cargo build` 编译 Rust 逻辑。
- **打包发布**: 通过 `tauri build` 生成原生安装包（目前配置为 Windows NSIS 格式）。
- **开发模式**: 支持 `npm run app:dev` 同时启动前端热更新和后端监听。

### 3. 开发者规范
- **环境隔离**: 严禁在全局 Python 环境中运行项目，必须使用项目根目录下的 `.venv`。
- **跨平台兼容**: 启动脚本均提供了 Shell, PowerShell 和 Batch 三种版本，以适配 Linux/Mac 和 Windows 环境。
- **配置优先**: 敏感信息（如 API Key）应配置在 `lan_mesh/model_pool.yaml` 或通过环境变量注入，避免硬编码。