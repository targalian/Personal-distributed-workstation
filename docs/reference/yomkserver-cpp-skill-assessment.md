# YomkServer C++ skill 评估

评估日期：2026-10-27。对象：`E:/ingobj/YomkServer`（CMake 版本 2.5.50，HEAD 506be42）与仓库内 `Skill/yomkserver-modular/`、本机已安装同名 skill。

## 结论

核心能力覆盖较完整，正常调用路径基本与源码一致；存在文档互相矛盾、边界条件遗漏、模板失败处理不足和安装后资源定位问题，尚不能认定“各项功能均能被可靠正确调用”。本轮仅评估，未修改框架或 skill。

框架是 C++17 进程内服务总线，URL 是进程内路由地址，并不自动提供 HTTP 或跨主机通信。核心为 Server/Service、Pkg、Boot；五个内置服务为 Context、EventLoop、FunctionPool、Logger、ServerInfo。

## 核验范围与证据

- 阅读公共头文件、服务与生命周期实现、Context/EventLoop 关键路径、工程/扩展模板及对应构建配置；核对 skill 主文档、reference.md 和 examples.md。
- 仓库 skill 与本机安装副本的三个 Markdown 文件在统一换行后内容完全一致，问题并非本机副本过旧。
- YomkAPI.h 中排除内部辅助和宏分派实现后有 73 个公共宏；56 个名字在 skill 文本中直接出现。其余 17 个中，16 个由日志级别及 ON/OFF 合并写法表达，仅 YOMK_VERSION 未覆盖。该指标只是入口文档覆盖，不代表运行正确率。
- 发现 4 条已失效的测试源码引用；内置标准消息包实际 30 种，reference.md 写为 31 种。
- 未进行编译或运行测试：当前 PATH 无 g++/cmake，WSL 列表访问返回 E_ACCESSDENIED。未运行需要 sudo/安装系统库的构建脚本。

## 功能覆盖

| 能力 | 覆盖与准确性 |
|---|---|
| Server/Service、同步/异步请求 | 注册、删除、替换、请求和回调已有说明；缺少强调同步请求可由多线程同时进入同一服务，Service 并不天然串行 |
| Pkg 消息与响应 | 定义、打包、解包、移动入包及响应字段覆盖；标准包数量错误 |
| Boot | 三阶段启动和模板覆盖；main 忽略失败返回值 |
| Context | CRUD、checker/monitor、同步/异步、只读发布、内省覆盖；空值和重入约束存在错误或遗漏 |
| EventLoop | 启停、续跑、投递、等待、销毁、tag 和内省覆盖；STOP 文档矛盾，POST_WAIT 的自循环内联语义未说明 |
| FunctionPool | 注册、热替换、调用、注销、元数据和内省覆盖；一处旧说明错误地声称动态函数没有类型信息 |
| Logger | 四级日志、文件落盘、代理、开关、删除和内省覆盖 |
| ServerInfo | 服务/函数列表、单查、全量拓扑覆盖 |
| 工程及扩展 | 完整骨架、CMake 导出和构建脚本覆盖；模板定位及扩展安装约束降低通用性 |
| 版本与质量工具 | 测试、静态分析和 sanitizer 有说明；缺少框架版本宏及 skill 版本绑定和覆盖回归 |

## 应优先修复的问题

### 1. EventLoop STOP 语义互相矛盾

`examples.md:776` 声称 stop 会清空队列；但同文件示例3和 reference.md 均写保留队列。源码 `src/Modules/EventLoop/EventLoop.cpp:37` 仅停线程，`:61` 的 destroy 才清队列。错误说明会导致停启、积压恢复和取消策略错误。

建议统一为：STOP 保留未执行队列，START 续跑；DESTROY 才丢弃未执行事件并释放等待者。

### 2. Context 的“永不为空”承诺不成立，SET 空值有崩溃风险

`reference.md:188` 声称 GET 永不返回空指针，但 YomkAPI::CONTEXT_GET 在未初始化、缺 key、解包失败时直接返回 ctxDefault；默认值传入空的对应消息 shared_ptr 时仍可能返回空。

`reference.md:189` 对 SET 只有“设置值”描述。源码 `src/Modules/Context/YomkContext.cpp:152` 对传入 m_value 直接解引用；已存在 key 执行 SET(key, nullptr) 有空指针解引用风险。该项属于框架实现缺陷，skill 同时缺少输入约束。

此外，reference.md 把“类型不匹配”放在 CREATE 行，实际类型检查发生于 SET；CREATE 对重复 key 先返回 key already exists。

建议删除无条件非空承诺，说明调用方提供非空默认包并检查结果；框架 SET 增加 nullptr 拒绝分支，并同步明确实际错误码。

### 3. 工程模板忽略启动失败

`Template/Project/main.cpp:9` 以及 examples.md 示例0调用 YOMK_BOOT 后不判断返回值，即使 MyBoot::after 返回失败，仍打印 running、等待输入并最终返回 0。会把初始化失败报告为正常退出。

建议检查 Boot 返回值，失败时关闭框架并返回非零；成功退出时显式关闭。

### 4. 关键并发约束未充分传达

- 同步 REQUEST 由调用线程直接 invoke；多个调用线程可并发进入同一服务。skill 应说明业务共享成员需要同步，或统一经指定 EventLoop 串行处理。
- checker 在 Context 写锁内执行；现文档只禁止重入 SET，但 GET/KEYS 等同样获取这把 shared_mutex，回调内再次访问同一 Context 服务也存在锁重入风险，应将限制写完整。
- EventLoop 的 POST_WAIT 若从本循环线程调用，会直接执行事件，而非排队等待。该分支存在于 EventLoop.cpp:135，skill 应说明其对 FIFO 直觉、递归深度的影响。
- EventLoop STOP 保留队列；已排队的 POST_WAIT 等待者不能假定 STOP 即完成或取消，需后续 START 或 DESTROY。

以上为源码推导，未以运行测试复现。

## 其他准确性与可移植性问题

1. `SKILL.md:81,256` 假定 skill 所在 Skill 目录的上层为框架根。安装副本只包含三个 Markdown，不含 Template/Examples；即使回退 examples.md 能生成，也无法保证优先复制真实模板。应显式解析框架路径、验证版本和模板是否存在。
2. 模板文件使用 TemplateProject/TemplateExtension/ExtensionService 等名字，而文档使用 ProjectName/ExtensionName/XxxService。应提供明确替换清单和生成后的残留占位符检查。
3. `examples.md:727` 声称 FunctionPool 动态注册无类型信息，与其后展示的三参/四参注册及源码矛盾；应限定为未声明 MsgName 的两参注册。
4. `reference.md:161` 的 installFunc 声明缺少实际已有的 description 第四参数，虽然宏章节已描述四参形式。
5. examples.md 引用以下不存在的测试：Test/YomkServer/TestYomkServerInfo.cpp、TestYomkContextInfo.cpp、TestYomkEventLoopInfo.cpp、TestYomkFunctionPoolInfo.cpp。应指向现有模块目录中的 Lifecycle 等测试。
6. 缺少 YOMK_VERSION（YomkAPI.h:593），标准消息数量应改为30。
7. 工程模板依赖 /proc/self/exe、Bash，扩展依赖 .so、sudo、ldconfig，适用边界实际为 Linux。应明确操作系统前提；不能把当前模板宣称为通用 Windows 工程模板。
8. 扩展 target 不导出自己的 include 使用要求，依赖扩展与框架同前缀安装；skill 已披露这一限制，但应避免让用户误以为标准导出 target 在任意安装前缀下可独立消费。
9. skill 未绑定框架兼容版本，也没有“公共 API → 说明 → 可编译示例 → 行为测试”映射。API 名称覆盖不能替代行为覆盖。

## 建议验收方式

在 Linux 可用环境中，生成临时工程及扩展并实际编译运行；至少验证 Boot 失败退出、缺 key 默认值、SET nullptr 拒绝、checker 禁止重入、服务并发状态、删除后的弱回调、STOP/START 续跑、DESTROY 释放等待者、POST_WAIT 内联分支、FunctionPool 元数据、日志代理卸载、框架版本和全量内省。

同时运行框架现有26个测试程序以及适用的 sanitizer 套件。测试通过后才能进一步评价“可可靠生成并正确调用”，本轮没有作出该运行时保证。

## 2026-10-27 后续修复结果

用户要求修复后，已将框架空值防护、工程入口失败处理、扩展导出配置、skill语义与资源定位修正写回 E:/ingobj/YomkServer，并同步 C:/Users/WindowShopper/.codex/skills/yomkserver-modular。新包附带22个模板文件、离线生成器及许可证。详细修复与可复验命令见 E:/ingobj/YomkServer/Docs/SkillValidation.md。

实测：框架及26个C++测试目标编译成功；22/26运行通过，4个Logger测试在原版DLL下的失败断言逐条相同。新增skill行为测试8/8通过，73公共宏/22模板/测试引用检查和quick_validate通过。离线生成的工程和扩展编译成功；扩展与框架分前缀安装的下游仅链接扩展target即可编译并调用版本接口。Linux交互构建、sudo安装及sanitizer未验收。

修改仅涉及目标C++框架及其安装skill，未提交或推送；work_station当前已有另一轮启动修复在进行，本项结论单独落盘，未覆盖其迭代状态。
