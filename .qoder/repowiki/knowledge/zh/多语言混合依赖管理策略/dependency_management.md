该仓库采用多语言混合架构，针对 Python、Rust 和 TypeScript/JavaScript 分别使用独立的包管理器进行依赖管理。

### 1. Python 后端 (`lan_mesh`)
- **管理工具**: `pip`
- **核心文件**: `requirements.txt`
- **版本策略**: 采用最小版本约束（如 `fastapi>=0.104.0`），允许向后兼容的自动升级。
- **配置管理**: 使用 YAML 文件（`config.yaml`, `model_pool.yaml`）管理运行时配置和第三方 API 密钥引用。其中 `model_pool.yaml` 被明确排除在版本控制之外（`.gitignore`），以保护敏感信息。

### 2. Rust 桌面端 (`quicklan-main/src-tauri`)
- **管理工具**: `Cargo`
- **核心文件**: `src-tauri/Cargo.toml` (声明), `src-tauri/Cargo.lock` (锁定)
- **版本策略**: 严格锁定依赖版本。`Cargo.lock` 确保了构建的可重复性，记录了所有传递性依赖的精确版本和校验和。
- **依赖来源**: 主要依赖 `crates.io` 官方源，同时使用了 Tauri 框架及其插件系统（如 `tauri-plugin-dialog`）。

### 3. Web 前端 (`quicklan-main`)
- **管理工具**: `npm`
- **核心文件**: `package.json` (声明), `package-lock.json` (锁定)
- **版本策略**: 使用 `^` 语义化版本前缀（如 `^2.0.0`），允许小版本和补丁版本的自动更新。`package-lock.json` 锁定了完整的依赖树。
- **构建工具链**: 基于 `Vite` 和 `Tauri CLI`，依赖包括 React 生态及 Lucide 图标库。

### 开发约定
- **安全隔离**: 敏感配置（如 API Keys）通过环境变量注入，并在配置文件中使用占位符或环境变量名引用，严禁硬编码。
- **跨语言协同**: Python 后端提供局域网服务，Rust/Tauri 前端作为客户端与之交互，两者通过局域网协议（UDP 发现 + HTTP/WebSocket）通信，依赖管理上保持解耦。