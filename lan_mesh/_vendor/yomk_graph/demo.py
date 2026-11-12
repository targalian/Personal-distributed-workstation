"""Clearly labelled illustrative topology; never presented as live project discovery."""
from .collector import GraphCollector


def demo_snapshot():
    graph = GraphCollector("移动机器人 · 演示项目", process="demo:robot")
    definitions = [
        ("操作与接入", "/Operator", [("/command", "ControlCommand", "接收操作请求与目标")]),
        ("操作与接入", "/RosBridge", [("/forward", "RosRequest", "公共协议到领域请求的适配")]),
        ("领域编排", "/Motion", [("/move", "MotionGoal", "目标准入与执行请求"), ("/cancel", "CancelRequest", "取消当前目标")]),
        ("领域编排", "/Enable", [("/set_enabled", "EnableRequest", "显式使能与失能请求")]),
        ("设备执行", "/ArmDriver", [("/execute", "JointCommand", "单一设备写入方"), ("/state", "", "只读驱动状态")]),
        ("设备执行", "/ChassisDriver", [("/velocity", "VelocityCommand", "底盘速度目标"), ("/stop", "", "受控停止请求")]),
        ("状态与观测", "/Diagnostics", [("/report", "DiagnosticSample", "归一化诊断数据"), ("/summary", "", "诊断摘要")]),
        ("状态与观测", "/Recorder", [("/append", "TelemetrySample", "遥测存储；此示例不连接真实DDS")]),
        ("框架基础服务", "/YomkServerInfo", [("/all", "", "服务与功能元信息"), ("/services", "", "当前已注册服务")]),
        ("框架基础服务", "/YomkContext", [("/get", "Context", "共享状态读取"), ("/set", "Context", "替换状态快照")]),
    ]
    infos = []
    for module, name, funcs in definitions:
        graph.module(module, [name])
        infos.append(dict(name=name, functions=[dict(name=f, message=m, description=d) for f, m, d in funcs]))
    flows = [
        ("/Operator", "/Motion", "/move", "MotionGoal", 126, 0),
        ("/RosBridge", "/Enable", "/set_enabled", "EnableRequest", 18, 0),
        ("/Motion", "/ArmDriver", "/execute", "JointCommand", 126, 0),
        ("/Motion", "/ChassisDriver", "/velocity", "VelocityCommand", 82, 2),
        ("/Enable", "/ArmDriver", "/state", "", 36, 0),
        ("/ArmDriver", "/Diagnostics", "/report", "DiagnosticSample", 252, 0),
        ("/ChassisDriver", "/Diagnostics", "/report", "DiagnosticSample", 164, 0),
        ("/Diagnostics", "/Recorder", "/append", "TelemetrySample", 416, 0),
    ]
    for source, target, endpoint, message, calls, errors in flows:
        ticket = graph.begin(source, target, endpoint, message=message)
        graph.finish(ticket)
        edge = next(reversed(graph._edges.values()))
        edge.update(calls=calls, errors=errors, total_ms=calls * 0.37, last_ms=0.37)
    graph.declare("/Operator", "/Enable", "/set_enabled", "EnableRequest", description="项目声明；演示中未执行")
    result = graph.snapshot(infos, version="演示", resources={}, warnings=[
        "当前为内置演示：模块、连线、计数均为示意数据。请接入项目或导入真实快照。",
        "实线：示意的已观测请求。虚线：仅项目声明，未证明发生。箭头朝向请求接收方。",
    ])
    result["demo"] = True
    return result
