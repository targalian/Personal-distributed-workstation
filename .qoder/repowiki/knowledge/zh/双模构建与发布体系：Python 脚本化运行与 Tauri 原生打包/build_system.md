该项目采用**双模构建体系**，分别针对后端分布式网格（Python）和桌面客户端（Tauri/Rust）设计了独立的构建、依赖管理与发布流程。

### 1. LAN Mesh 分布式智能体网络 (Python)
- **构建方式**：采用轻量级脚本化运行模式。通过 `requirements.txt` 管理 Python 依赖，使用 `.venv` 虚拟环境隔离运行时。
- **入口与角色**：以 `main.py` 为统一入口，通过命令行参数区分 `secretary`（主控节点）和 `worker`（工作节点）角色。
- **启动脚本**：在 `scripts/` 目录下提供了跨平台的启动脚本（`.sh` 和 `.ps1`），负责环境检查、虚拟环境激活及参数透传。
- **版本管理**：版本号定义在 `lan_mesh/__init__.py` 中，目前为 `0.1.0`。
- **部署现状**：当前主要依赖手动或脚本化部署，暂未集成 Docker 容器化或自动化 CI/CD 流水线。

### 2. QuickLAN 桌面应用 (Tauri + React)
- **前端构建**：基于 Vite + React + TypeScript。通过 `npm run build` 将源码编译为静态资源并输出至 `dist` 目录。
- **后端构建**：基于 Rust (Cargo)。核心逻辑位于 `quicklan-main/src-tauri`，利用 Tauri 2 框架实现 Web 前端与 Rust 后端的深度集成。
- **打包流程**：
  - **开发模式**：`npm run app:dev` 自动触发前端热更新与后端监听。
  - **生产模式**：`npm run app:build` 调用 `tauri build` 生成原生安装包。
- **安装程序**：针对 Windows 平台配置了 NSIS 打包目标，并通过 `windows/hooks.nsi` 在安装/卸载阶段自动处理防火墙规则（开放 UDP/TCP 端口）。
- **版本同步**：要求在 `package.json`、`Cargo.toml` 与 `tauri.conf.json` 之间保持版本号（如 `0.1.1`）的一致性。

### 开发者规范
- **端口规划**：Python 端默认使用 45470 等端口，Tauri 端需在防火墙脚本中维护 45454-45476 范围的端口规则，确保局域网发现与传输协议互通。
- **跨语言协作**：前端 UI 仅通过 Tauri API 与 Rust 后端通信，Rust 端负责底层网络发现与文件 IO；Python 端则作为独立的服务端或后台进程运行。