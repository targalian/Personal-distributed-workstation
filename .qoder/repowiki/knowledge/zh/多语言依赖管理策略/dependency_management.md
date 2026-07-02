本项目为多语言混合仓库，采用三种独立的包管理器分别管理 Python、JavaScript/TypeScript 和 Rust 三方依赖，各子工程各自维护声明文件与锁文件。

Python（根目录）
- 使用 requirements.txt 声明运行时依赖，版本约束采用 >= 宽松下限（如 fastapi>=0.104.0），未生成 requirements.lock 或 poetry.lock，也未使用 pipenv/poetry。
- 项目自带 .venv 虚拟环境目录，但未被纳入版本控制；无 setup.py/pyproject.toml，仅通过 main.py 直接运行。
- 未配置私有 PyPI 源或 pip.conf，默认从官方 pypi.org 拉取。

前端（quicklan-main）
- 使用 package.json + package-lock.json（lockfileVersion=3）锁定 npm 依赖树，所有依赖均指向 registry.npmjs.org，包含完整 integrity hash。
- 依赖分为 dependencies（react、@tauri-apps/*、lucide-react）与 devDependencies（vite、typescript、@types/*、@tauri-apps/cli）。
- 未使用 pnpm-lock.yaml 或 yarn.lock，仅由 npm 生成 lock 文件。

Rust Tauri 后端（quicklan-main/src-tauri）
- 使用 Cargo.toml 声明 crate 依赖，版本采用语义化范围（如 tauri = { version = "2", features = [...] }），并通过 features 精确裁剪编译产物。
- 配套 Cargo.lock 记录每个 crate 的精确版本与 checksum，来源统一为 registry+https://github.com/rust-lang/crates.io-index。
- 未配置 ~/.cargo/config.toml 中的私有 registry 或 [source] 替换。

约定与注意事项
- 三个语言的依赖声明互不关联，新增依赖需同步更新对应清单文件。
- Python 侧缺少锁文件，建议引入 pip-tools/poetry 以固定可复现构建。
- 前端与 Rust 均已提交 lock 文件，CI 可直接基于 lock 安装，无需额外缓存策略。