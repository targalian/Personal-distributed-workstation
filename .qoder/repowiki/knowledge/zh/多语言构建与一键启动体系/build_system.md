本项目采用 Python + Rust(Tauri) + TypeScript/React 的多语言混合架构，构建系统围绕三个层次组织：

**1. Python 后端（lan_mesh）**
- 依赖管理：根目录 `requirements.txt` 声明 FastAPI、uvicorn、pydantic 等运行时依赖，无虚拟环境锁定文件。
- 统一入口：`main.py` 通过 argparse 提供 `station` / `secretary` / `worker` 三种角色子命令，由 `__version__` 暴露版本。
- 开发/运行：通过 `scripts/start_workstation.sh`（Linux/macOS）和 `scripts/start_workstation.bat`（Windows）实现“一键启动”——自动检测 Python、创建 `.venv`、安装依赖、复制 `model_pool.example.yaml` → `model_pool.yaml`、校验 API Key，然后以 `exec python main.py station --port 45470` 启动 Station Director，并可选后台拉起 Worker。
- 配置：`config.yaml` 为全局配置，`lan_mesh/model_pool.yaml` 为模型池配置（首次启动自动生成）。

**2. Tauri 桌面前端（quicklan-main）**
- 前端工程：Vite + React + TypeScript，`package.json` 中定义 `dev` / `build` / `app:dev` / `app:build` 脚本。
- Rust 后端：`quicklan-main/src-tauri/Cargo.toml` 同时输出 staticlib/cdylib/rlib 三种 crate type，二进制名为 `quicklan`。
- 构建流程：`tauri.conf.json` 配置 `beforeBuildCommand: npm run build`，将 Vite 产物输出到 `../dist`，再由 Tauri 打包为 NSIS 安装包（仅 Windows target），包含图标、语言包与安装器钩子。
- 版本同步：`package.json`、`Cargo.toml`、`tauri.conf.json` 三处版本号均为 `0.1.1`，需手动维护一致性。
- 构建脚本：`src-tauri/build.rs` 调用 `tauri_build::build()` 完成代码生成。

**3. 约定与约束**
- 无 Makefile / Dockerfile / CI 流水线，本地构建完全依赖脚本与包管理器。
- Python 侧不提交 `.venv/`，Rust/Tauri 侧使用 Cargo.lock 锁定依赖。
- 跨平台差异通过两套 shell/bat 脚本分别处理路径与编码（bat 中设置 `chcp 65001` 与 `PYTHONIOENCODING=utf-8`）。
- 未实现自动化发布或交叉编译，Tauri 仅绑定 Windows NSIS target。