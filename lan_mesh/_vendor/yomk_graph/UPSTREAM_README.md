# YomkGraph

把基于 Yomk 框架的项目画成可交互的 **模块 → 服务 → 功能 → 请求流向** 框图。
基于真实 `YomkServerInfo` 自省，支持 YomkServer C++ 与 YomkServerPy。
查看器仅使用 Python 标准库与本地 HTML/CSS/JavaScript，无前端构建、CDN 或运行时下载。
它是独立可选的开发工具；框架的默认构建、初始化和业务入口不会自动启动观测。

当前接入示例面向 C++ **3.1.8**；Python 源码运行版 **1.5.6 / 1.5.10** 均有真实框架回归。

![YomkGraph 演示框图](docs/preview.png)

## 先运行

从仓库根目录运行，Python 3.10 及以上即可：

```bash
cd Tools/YomkGraph
python3 -m yomk_graph --demo
```

打开 **http://127.0.0.1:18765**。这是明确标注的示例数据，可立即体验：

- 模块分组、服务卡片、功能与消息类型；点击查看说明、上下游及统计。
- 默认全局显示快照内的全部模块、服务与框架基础服务；支持搜索和按模块筛选。
- 点击“全局总览”清除筛选和选中高亮，恢复基础服务、声明连线、功能展示，并自动适应画布。
- 点击基础服务查看已采集的资源详情，或切换“功能目录”浏览全部入口。
- 平移、缩放、适应画布、邻接高亮、功能目录与两秒一次自动刷新。
- JSON 快照导入/导出与独立 SVG 框图导出，支持离线查看已导出的快照。
- Python 接入还提供 Context 键类型、Monitor 数量、EventLoop 状态/积压、FunctionPool 功能元信息。

`--port 18766` 可改变端口。Ctrl+C / SIGTERM 正常关闭。
如需安装进已有虚拟环境：`python -m pip install .`，之后使用 `yomk-graph --demo`。

## 图上什么是真实证据

**YomkServerInfo 只告诉我们注册了哪些服务和功能，不含完整数据流图。**
本工具不会把相同消息类型、同一个 Context、注册顺序或 DDS 可发现性猜成依赖。

| 图形 | 来源 | 含义 |
| --- | --- | --- |
| 服务卡片与功能 | `/YomkServerInfo/all` | 采集时读到的已注册服务、函数、期望消息类型与说明 |
| 模块框 | 项目显式 `module()` 分组 | 业务/包的归属；未配置时显示“未分组服务” |
| 实线箭头 | Python 请求观察 / C++ 显式 span | 采集期间实际发生的请求，方向为调用方 → 接收方 |
| 虚线箭头 | 项目 `declare()` | 声明的关系，不证明请求已发生 |
| 红线 | 异常或框架失败状态 | 含框架失败的调用，不代表硬件故障 |

响应沿反方向返回。计数是请求完成次数，耗时包含嵌套与等待；不是消息字节量或吞吐率。
成功响应（Python `eOk` / C++ `ResStatus::Successed`）负载内的业务拒绝不自动算框架失败。没有连线只表示未观测到，不能据此证明无依赖。
服务清单与调用统计也不是跨所有服务的事务快照。

## Python 项目接入

项目先自行 `api.init()`，在 Boot 或业务生产者启动前由控制线程挂接采集器。
业务服务初始化完成后再开放 HTTP；注册过程中产生的调用也可以记录。
`YomkApi` 由项目自己的安装环境提供；工具本身不初始化、不关闭项目框架。

```python
import YomkApi as api
from yomk_graph import GraphCollector, GraphServer, PythonObserver

# 此时项目已经 api.init()，尚未启动业务生产者。
graph = GraphCollector("我的项目")
graph.module("领域编排", ["/Motion", "/Enable"])
graph.module("设备执行", ["/ArmDriver", "/ChassisDriver"])

observer = PythonObserver(api, collector=graph)
# 在这里按项目原流程完成 Boot / 服务注册与初始化。
viewer = GraphServer(observer.snapshot, port=18765).start()

# 继续项目正常业务。api.request() 和 service.request() 的嵌套请求会被观察。
# 在项目已有的退出路径中：
# 1. viewer.close()  # 停止接收并排空 HTTP 读取
# 2. 停止并排空业务线程、回调和异步任务；
# 3. observer.close()
# 4. 由项目按原流程 api.shutdown()。
```

此观察模式临时包装**当前 server 实例**的 `request`，不修改框架源文件、类定义或其他进程。
响应原样返回、异常继续传播；卸载恢复之前的实例方法。一个实例只允许一个请求观察器。
`GraphServer.close()` 等待已经进入的 HTTP 查询结束后返回；provider 应是有界的只读查询，
不要在 provider 内调用 close。HTTP 客户端读超时为两秒，线程启动失败会释放监听端口。
HTTP 的排空不代替项目业务线程、EventLoop 或异步请求池的排空。
安装/卸载由同一控制线程、在生产者启动前/排空后执行，不与其他组件抢占同一方法。
若运行环境禁止实例方法包装，使用 `capture=False` 读取服务清单，并使用显式 `span()`。
目前验证了 Python 源码运行的 1.5.6 和 1.5.10；未声称所有 Cython 打包版本都支持自动挂接。

异步任务的提交者不由框架传播，因此自动记录的异步入口来源为 `@caller`。
`@caller` 也包括应用入口和未知来源；不会伪装成业务服务。
给自建 worker、EventLoop 或 Monitor 回调加明确归属时：

```python
with observer.caller("/MyWorker"):
    response = api.request("/Motion/move", goal)

# 自定义数据通道可以显式记录（scope内发生的异常计入失败）：
with graph.span("/Sensor", "/Consumer", "/sample", kind="sample", message="Sample"):
    deliver_sample()

# 仅声明尚未运行/无法自动采集的设计关系，显示为虚线：
graph.declare("/Sensor", "/Consumer", "/sample", "Sample", kind="sample")
```

Context/FunctionPool/EventLoop 的请求可观察到其框架服务，但其内部回调并不自动拥有业务
服务身份。`caller()` 只影响当前执行上下文，不自动穿越线程。
资源列表读取的是白名单元信息，不读取 Context 值、队列 payload、回调对象或日志内容。
无需这些资源查询时传 `resources=False`。

实际框架无设备示例：在可导入已安装 `YomkApi` 的环境执行：

```bash
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" python3 examples/python_project.py
```

示例只在同进程调用 `/Motion/move → /ArmDriver/execute`，不使用 ROS、DDS 或设备。
`YomkServerPy` shutdown 后不可重启；不同运行周期使用新进程。

## C++ 项目接入

`include/yomk_graph.hpp` 是 C++17 头文件，不依赖 JSON 库、DDS 或 Python。
`Graph::from_server_info()` 直接接收 `ServerServiceInfoList` 包的 `d`，读取真实字段
`m_serviceName / m_funcs / m_funcName / m_msgName / m_description`。

[导出实现](examples/cpp_export.cpp) 在项目非实时侧请求 `YomkAPI::getAllServerInfo()`、校验状态与包类型，
生成统一 JSON 并原子替换快照文件。项目只需包含此实现和头文件。

```cpp
yomk_graph::Graph graph("我的C++项目", "control:instance-1");
graph.module("领域编排", {"/Motion"});
graph.module("设备执行", {"/ArmDriver"});

// 在已有非实时请求调用点插桩；span的寿命必须短于graph。
{
    auto span = graph.scope("/Motion", "/ArmDriver", "/execute");
    auto response = YomkAPI::request("/ArmDriver/execute", payload);
    span.failed(response.m_status != YomkResponse::ResStatus::Successed);
}
// 已初始化的非实时观测线程定期调用，输出文件保持单一writer：
export_yomk_graph(graph, "project-graph.json");
```

C++ 不自动替换所有框架请求入口；只有显式 span 才产生实线。未插桩的服务仍出现在清单。
若只想展示设计关系，使用 `graph.declare(source, target, endpoint, message)`。

完整无设备 C++ 示例（要求已经安装 3.1.8）：

```bash
cmake -S examples -B build/cpp -DCMAKE_PREFIX_PATH=/path/to/yomk-prefix
cmake --build build/cpp -j2
./build/cpp/yomk-graph-cpp project-graph.json
python3 -m yomk_graph --snapshot project-graph.json
```

导出并查看的链路已在真实 YomkServer 3.1.8 上验证。框架的初始化/关闭仍归原应用。
任何采集、锁、计时、字符串或文件写入都放在**非实时线程**，不可加入实时控制周期。

## 快照、部署与范围

```python
observer.export("project-graph.json")  # Python原子写入
```

```bash
python3 -m yomk_graph --snapshot project-graph.json
```

查看器每两秒重读文件。采集失败会显示“数据源不可用”并保留上次框图，不把旧图标成最新。
JSON最大16 MiB、5000个服务、10000条边；该上限是输入拒绝边界，不代表已通过对应规模的性能验收。
每个采集器默认最多保留2000种观测连线，达到上限时保留已有统计并报告新流向丢弃次数。

HTTP默认只绑定127.0.0.1；仅提供页面和只读 `/api/snapshot`，没有通用服务调用/使能/修改接口。
远程使用SSH端口转发，不直接暴露控制进程。端口、采集器寿命和应用退出由项目负责人协调。
这里的“全局”指当前接入进程的完整快照；尚未注册的模块不会被自动发现，资源详情取决于采集端提供的信息。
本版本展示单进程：不能扫描其他进程的singleton，暂不自动合并跨进程、Python/C++或DDS拓扑。
YomkRpc数据流可由项目显式标注，当前不启动DDS发现、录制、回放或pub/sub探针。

Qt 应用与后台任务的接入顺序见 [应用集成](docs/application-integration.md)。
格式细节和实现分层见 [architecture.md](docs/architecture.md)，验证边界见 [validation.md](docs/validation.md)。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

包含C++17导出器的编译/并发测试，需`c++`。若`YomkApi`不可导入，真实Python框架测试明确skip；
把自己的框架prefix加入`PYTHONPATH`后可运行全部集成测试，不需要安装/修改系统框架。
浏览器检查脚本为 `tests/browser.cjs`，使用现有Playwright/Chrome，设置方法见验证文档。
