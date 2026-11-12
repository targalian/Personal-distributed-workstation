"""Bounded metadata-only collection, shared by the Python SDK and file viewer."""
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time

SCHEMA = "yomk-graph/v1"
BUILTINS = {"/YomkServerInfo", "/YomkContext", "/YomkEventLoop", "/YomkLogger", "/YomkFunctionPool"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def validate_snapshot(value):
    """Validate the transport boundary; do not silently accept unrelated JSON."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("expected yomk-graph/v1 snapshot")
    for key in ("project", "process", "version", "captured_at", "started_at"):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"{key} must be a string")
    if not isinstance(value.get("warnings", []), list) or not all(isinstance(w, str) for w in value.get("warnings", [])):
        raise ValueError("warnings must be an array of strings")
    resources = value.get("resources", {})
    if not isinstance(resources, dict):
        raise ValueError("resources must be an object")
    for items in resources.values():
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("resource metadata must be arrays of objects")
        for item in items:
            if "keys" in item and (not isinstance(item["keys"], list) or not all(isinstance(k, dict) for k in item["keys"])):
                raise ValueError("context keys must be an array of objects")
    telemetry = value.get("telemetry", {})
    if not isinstance(telemetry, dict):
        raise ValueError("telemetry must be an object")
    services, edges = value.get("services"), value.get("edges")
    if not isinstance(services, list) or not isinstance(edges, list):
        raise ValueError("services and edges must be arrays")
    if len(services) > 5000 or len(edges) > 10000:
        raise ValueError("snapshot exceeds 5000 services / 10000 edges")
    ids = set()
    for service in services:
        if not isinstance(service, dict) or not isinstance(service.get("id"), str) or not service["id"]:
            raise ValueError("each service requires a nonempty string id")
        if service["id"] in ids:
            raise ValueError("duplicate service id")
        ids.add(service["id"])
        if not isinstance(service.get("name"), str) or not isinstance(service.get("module"), str):
            raise ValueError("service name/module must be strings")
        if not isinstance(service.get("functions"), list):
            raise ValueError("service functions must be an array")
        for func in service["functions"]:
            if not isinstance(func, dict) or not all(isinstance(func.get(k, ""), str) for k in ("name", "message", "description")):
                raise ValueError("function metadata must contain strings")
    edge_ids = set()
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("id"), str) or not edge["id"] or edge["id"] in edge_ids:
            raise ValueError("edge id must be a unique string")
        edge_ids.add(edge["id"])
        if not all(isinstance(edge.get(k, ""), str) for k in ("endpoint", "message", "kind", "description")):
            raise ValueError("edge metadata must contain strings")
        if edge.get("source") not in ids or edge.get("target") not in ids:
            raise ValueError("edge endpoint is not a service id")
        if edge.get("evidence") not in ("observed", "declared"):
            raise ValueError("edge evidence must be observed or declared")
        for key in ("calls", "errors", "in_flight", "total_ms", "last_ms"):
            number = edge.get(key, 0)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                raise ValueError(f"invalid edge metric: {key}")
    return value


class GraphCollector:
    """A process-local collector. No payload, response body or log message is retained."""
    def __init__(self, project="Yomk Project", process=None, max_edges=2000):
        if not 1 <= max_edges <= 10000:
            raise ValueError("max_edges must be 1..10000")
        self.project = project
        self.process = process or f"python:{os.getpid()}"
        self.max_edges = max_edges
        self._lock = threading.RLock()
        self._edges = OrderedDict()
        self._modules = {}
        self._declarations = []
        self._dropped = 0
        self.started_at = utcnow()

    def service_id(self, name):
        return self.process + "::" + name

    def module(self, name, services, description=""):
        """Explicit module ownership; never infer dependencies from equal message names."""
        with self._lock:
            for service in services:
                self._modules[service] = (name, description)
        return self

    def declare(self, source, target, endpoint="", message="", kind="request", description=""):
        with self._lock:
            if len(self._declarations) >= self.max_edges:
                raise ValueError("too many declared edges")
            self._declarations.append(dict(source=source, target=target, endpoint=endpoint,
                                           message=message, kind=kind, description=description))
        return self

    def begin(self, source, target, endpoint, kind="request", message=""):
        key = (source, target, endpoint, kind)
        with self._lock:
            edge = self._edges.get(key)
            if edge is None:
                if len(self._edges) >= self.max_edges:
                    self._dropped += 1
                    return None
                edge = dict(id="observed:" + str(len(self._edges)), source=source, target=target,
                            endpoint=endpoint, kind=kind, message=message, evidence="observed",
                            calls=0, errors=0, in_flight=0, total_ms=0.0, last_ms=0.0)
                self._edges[key] = edge
            edge["in_flight"] += 1
            return key, time.perf_counter()

    def finish(self, ticket, failed=False):
        if ticket is None:
            return
        key, started = ticket
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            edge = self._edges[key]
            edge["in_flight"] -= 1
            edge["calls"] += 1
            edge["errors"] += int(failed)
            edge["last_ms"] = round(elapsed, 3)
            edge["total_ms"] += elapsed

    @contextmanager
    def span(self, source, target, endpoint="", kind="request", message=""):
        """Explicit instrumentation for custom tasks, Context or external transports."""
        ticket = self.begin(source, target, endpoint, kind, message)
        failed = True
        try:
            yield
            failed = False
        finally:
            self.finish(ticket, failed)

    def snapshot(self, service_infos, *, version="unknown", resources=None, warnings=()):
        with self._lock:
            modules = dict(self._modules)
            edges = [dict(edge) for edge in self._edges.values()]
            declared = [dict(edge) for edge in self._declarations]
            dropped = self._dropped
        services = {}
        for info in service_infos:
            name = info["name"]
            module, description = modules.get(name, ("框架基础服务" if name in BUILTINS else "未分组服务", ""))
            services[name] = dict(id=self.service_id(name), name=name, module=module,
                                  description=description, builtin=name in BUILTINS,
                                  registered=True, functions=info["functions"])
        for index, edge in enumerate(declared):
            edge.update(id=f"declared:{index}", evidence="declared", calls=0, errors=0,
                        in_flight=0, total_ms=0, last_ms=0)
            edges.append(edge)
        for edge in edges:
            for field in ("source", "target"):
                name = edge[field]
                if name not in services:
                    module, description = modules.get(name, ("进程边界" if name == "@caller" else "未注册 / 外部", ""))
                    services[name] = dict(id=self.service_id(name), name=name, module=module,
                                          description=description, builtin=False, registered=False,
                                          functions=[], boundary=name == "@caller")
                edge[field] = self.service_id(name)
            edge["total_ms"] = round(edge["total_ms"], 3)
        return dict(schema=SCHEMA, project=self.project, process=self.process, version=version,
                    captured_at=utcnow(), started_at=self.started_at, services=list(services.values()),
                    edges=edges, resources=resources or {}, warnings=list(warnings),
                    telemetry=dict(dropped_edges=dropped, max_edges=self.max_edges))


def write_snapshot(path, snapshot):
    """Atomic replacement, so file viewers cannot see a half-written snapshot."""
    validate_snapshot(snapshot)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, delete=False) as stream:
            name = stream.name
            json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
