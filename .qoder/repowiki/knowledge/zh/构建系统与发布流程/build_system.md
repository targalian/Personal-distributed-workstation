LAN Mesh 项目采用混合技术栈（Python 后端 + Tauri/Rust 桌面前端），其构建与部署体系呈现出**脚本驱动**和**工具链原生集成**的特点，缺乏统一的自动化构建入口（如 Makefile 或 CI/CD 流水线）。

### 1. Python 后端 (lan_mesh)
- **依赖管理**: 使用标准的 `requirements.txt` 管理 Python 依赖（FastAPI, Uvicorn, Pydantic 等）。
- **环境初始化**: 通过 `scripts/setup_env.ps1` 提供一键式环境配置，自动创建 `.venv` 虚拟环境、安装依赖并复制配置模板 (`model_pool.example.yaml`)。
- **启动方式**: 采用 `main.py` 作为统一入口，支持 `secretary`（主控）和 `worker`（工作节点）两种角色。提供了跨平台的 Shell/PowerShell 启动脚本 (`start_secretary.sh/ps1`, `start_worker.sh/ps1`)，支持端口、名称等参数的动态注入。
- **打包部署**: 目前未见专门的 Python 打包配置（如 `pyproject.toml` 或 `setup.py`），主要以源码运行或容器化（文档提及 Docker/K8s 但仓库根目录未见相关配置文件）方式部署。

### 2. 桌面前端 (quicklan-main)
- **技术栈**: React + TypeScript + Vite + Tauri (Rust)。
- **前端构建**: 使用 `npm run build` (Vite) 编译静态资源。
- **桌面应用打包**: 深度集成 Tauri CLI。
  - **开发**: `npm run app:dev` 同时启动 Vite 开发服务器和 Tauri 运行时。
  - **发布**: `npm run app:build` 触发 Rust 编译及安装包生成。
  - **产物**: 针对 Windows 平台生成 NSIS 安装包，输出路径为 `src-tauri/target/release/bundle/nsis/`。
- **版本管理**: 版本号在 `package.json`、`src-tauri/Cargo.toml` 和 `src-tauri/tauri.conf.json` 中手动同步（当前为 `0.1.1`）。

### 3. 缺失的自动化设施
- **无 CI/CD**: 仓库中未发现 `.github/workflows` 或其他 CI 配置文件，构建与测试主要依赖本地手动执行。
- **无统一构建脚本**: 缺少根目录下的 `Makefile` 或 `build.sh` 来串联前后端的构建流程。
- **无容器化定义**: 尽管文档提及 Docker，但根目录缺少 `Dockerfile` 或 `docker-compose.yml`。

### 开发者建议
1. **环境准备**: 首次运行需先执行 `scripts/setup_env.ps1` (Windows) 或手动创建虚拟环境并安装 `requirements.txt`。
2. **前端开发**: 进入 `quicklan-main` 目录，确保已安装 Node.js 和 Rust 工具链，运行 `npm install` 后使用 `npm run app:dev`。
3. **版本同步**: 发布新版本时，需手动更新 `quicklan-main` 下的三个版本标识文件，保持语义一致。