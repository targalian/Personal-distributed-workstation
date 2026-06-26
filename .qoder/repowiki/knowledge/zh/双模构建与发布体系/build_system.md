该项目包含两个独立的子系统，分别采用不同的构建与打包策略：

### 1. 局域网分布式智能体协同网络 (Python)
- **构建方式**：基于 `requirements.txt` 进行依赖管理，通过 `main.py` 作为统一入口启动。
- **运行模式**：支持 `master`（主控节点）和 `worker`（工作节点）两种角色，通过命令行参数配置端口、设备名称及共享路径。
- **版本管理**：在 `lan_mesh/__init__.py` 中定义版本号 (`0.1.0`)。
- **部署**：目前为脚本化运行，未提供容器化（Docker）或自动化 CI/CD 配置文件。

### 2. 局域网文件共享桌面应用 (Tauri + React)
- **前端构建**：使用 Vite + React + TypeScript。通过 `npm run build` 编译静态资源至 `dist` 目录。
- **后端构建**：使用 Rust (Cargo)。核心逻辑位于 `src-tauri`，利用 Tauri 2 框架将 Web 前端与 Rust 后端打包为原生应用。
- **打包流程**：
  - 开发：`npm run app:dev` (自动触发前端热更新与后端监听)。
  - 生产：`npm run app:build` (执行 `tauri build`)。
- **安装程序**：针对 Windows 平台生成 NSIS 安装包 (`nsis` target)。
- **系统集成**：通过 `windows/hooks.nsi` 在安装/卸载时自动配置 Windows 防火墙规则，开放 UDP/TCP 特定端口以支持局域网发现与传输。
- **版本同步**：`package.json`、`Cargo.toml` 与 `tauri.conf.json` 中的版本号保持同步 (`0.1.1`)。

### 开发者规范
- **端口一致性**：Python 端默认使用 45470 等端口，Tauri 端在防火墙脚本中定义了 45454-45476 范围的端口规则，需确保两端协议端口不冲突或按设计互通。
- **跨语言协作**：前端 UI 通过 Tauri API 调用 Rust 命令，Rust 端处理底层网络发现与文件 IO；Python 端则独立运行于后台或服务器环境。