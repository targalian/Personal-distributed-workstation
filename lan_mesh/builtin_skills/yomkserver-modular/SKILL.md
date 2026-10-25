---
name: yomkserver-modular
description: 使用 YomkServer 开发 C++17 服务、消息与工程骨架；仅适用于明确采用 Yomk 的 C++ 工程。
metadata:
  category: architecture
  tags: [yomk, yomkserver, cpp, "c++"]
  default_access: [station, secretary, pm, worker]
  version: "1.0-1"
  activation:
    all: [yomk]
    any: ["c++", cpp, yomkserver-modular]
---

# YomkServer C++17 模块化开发

核心思想是“一切皆服务，一切皆请求”。公共入口为 `<YomkServer/YomkAPI.h>`，命名空间 `yomk`。
用 YomkService 划分业务职责，以消息和 `/服务名/操作名` 调用协作；明确容器、状态、线程与退出的所有权。
Context、EventLoop、FunctionPool 和 Logger 的类型及签名以本技能参考资料和目标版本源码为准。

用户要求新建 Yomk C++ 工程时，按 [examples.md](examples.md) 示例 0 生成完整可编译骨架，
采用目标平台适用的构建命令并验证。修改已有工程时保留原有构建、接口和路径约定。
普通 C++ 项目不因此引入 Yomk；LAN Mesh Python 后端不使用 C++ 模板。

查 API 时读 [reference.md](reference.md)，创建服务或处理事件循环时读 [examples.md](examples.md) 的对应示例。
[upstream-skill.md](upstream-skill.md) 保留完整上游说明。技能不额外授权安装系统组件、部署或外部写操作。
