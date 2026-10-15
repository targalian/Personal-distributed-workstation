"""Verify question-bound replies and durable refinement continuation across process death."""

import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_checkpoint import PMCheckpoints, read_checkpoint
from lan_mesh.pm_clarification import inspect_question, query_question, route_answer
from lan_mesh.pm_lease import lease_identity
from lan_mesh.pm_planner import PMPlanner
from lan_mesh.pm_resume import resume_pm_once
from lan_mesh.protocol import PMAgent
from lan_mesh.task_resume import prepare_resume, resume_authorization
from tests.test_pm_resume import finish, resume
from tests.test_pm_resume import recovery as _recovery_fixture
from tests.test_pm_start import controller as _controller_fixture
from tests.test_pm_start import live_server

controller = _controller_fixture
recovery = _recovery_fixture


def eventually(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Expected condition was not reached")


def pending(recovery, *, round_num=0, response=None, expires=None):
    st = recovery.state
    st.task["description"] = "做工具"
    st.task["input_data"] = {}
    context = ["Q1: 最初问题\nA1: 使用 Python"] if round_num else []
    st.plan = {}
    st.subtask_outputs = {}
    st.phase = "awaiting_input"
    st.refinement = {
        "format": 1,
        "task": dict(st.task),
        "description": "做工具",
        "round": round_num,
        "context": context,
        "complete": False,
    }
    question = PMPlanner._generate_refinement_question("做工具", context, round_num)
    st.clarification_request = {
        "format": 1,
        "question_id": "question-original",
        "question": question,
        "options": [],
        "asked_at": time.time() - 20,
        "timeout": 120,
        "deadline": expires if expires is not None else time.time() + 90,
        "continuation": {"kind": "refinement", "round": round_num},
        "consumed": False,
    }
    if response:
        st.clarification_response = {"question_id": "question-original", "response": response, "choice": response}
    recovery.save()


@pytest.fixture
def waiting(recovery, monkeypatch):
    monkeypatch.setattr(
        PMPlanner, "analyze_with_skill", lambda self, task: {"pattern": "single", "decomposition": [{"name": "工具"}]}
    )
    pending(recovery)
    return recovery


def current_question(recovery, previous=""):
    pm = recovery.controller._local_pm_agent
    return eventually(
        lambda: (
            dict(pm._state.clarification_request)
            if pm._state.phase == "awaiting_input" and pm._state.clarification_request.get("question_id") != previous
            else None
        )
    )


def answer(recovery, qid, text="使用 Python"):
    return route_answer(recovery.controller, "pm-resume", {"question_id": qid, "response": text})


def test_waiting_question_resumes_same_deadline_and_two_answers_once(waiting):
    deadline = waiting.state.clarification_request["deadline"]
    assert resume(waiting)["ok"]
    first = current_question(waiting)
    assert first["question_id"] == "question-original" and first["deadline"] == deadline
    waiting.controller.chat_runtime.execute.assert_not_called()
    assert answer(waiting, first["question_id"])["ok"]
    second = current_question(waiting, first["question_id"])
    assert answer(waiting, first["question_id"])["duplicate"]
    assert not answer(waiting, first["question_id"], "覆盖旧回答")["ok"]
    assert not waiting.controller._local_pm_agent._state.clarification_response
    assert answer(waiting, second["question_id"], "需要测试")["ok"]
    pm = finish(waiting)
    desc = pm.runtime.execute.call_args.args[0]["description"]
    assert desc.count("A1: 使用 Python") == 1 and desc.count("A2: 需要测试") == 1
    assert pm.runtime.execute.call_count == 1
    assert waiting.controller.db.get_task("task-resume").status == "completed"


def test_saved_answer_before_crash_is_consumed_without_asking_again(waiting):
    waiting.state.clarification_response = {
        "question_id": "question-original",
        "response": "已保存回答",
        "choice": "已保存回答",
    }
    waiting.save()
    assert resume(waiting)["ok"]
    second = current_question(waiting, "question-original")
    assert answer(waiting, second["question_id"], "无")["ok"]
    pm = finish(waiting)
    assert pm._state.task["description"].count("A1: 已保存回答") == 1


def test_expired_question_does_not_restart_wait_budget(waiting):
    waiting.state.clarification_request["deadline"] = time.time() - 1
    waiting.save()
    started = time.monotonic()
    assert resume(waiting)["ok"]
    pm = finish(waiting)
    assert time.monotonic() - started < 4
    assert pm._state.clarification_response["timed_out"]
    assert pm.runtime.execute.call_count == 1
    assert not answer(waiting, "question-original")["ok"]


@pytest.mark.parametrize("field", ["pm_id", "task_id", "device_id", "question_id"])
def test_wrong_reply_identity_cannot_wake_or_mutate(waiting, field):
    assert resume(waiting)["ok"]
    current_question(waiting)
    pm = waiting.controller._local_pm_agent
    payload = {
        "pm_id": "pm-resume",
        "task_id": "task-resume",
        "device_id": "self",
        "question_id": "question-original",
        "response": "answer",
    }
    payload[field] = "wrong"
    with pm._state.lock:
        before = waiting.path.read_bytes()
        assert not pm.receive_input(payload)["ok"]
        assert waiting.path.read_bytes() == before and not pm._state.clarification_event.is_set()
    pm.runtime.execute.assert_not_called()


def test_reply_write_failure_is_not_acknowledged_or_exposed_as_saved(waiting):
    assert resume(waiting)["ok"]
    current_question(waiting)
    with sqlite3.connect(waiting.path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_reply BEFORE INSERT ON pm_checkpoints BEGIN SELECT RAISE(ABORT,'disk failure'); END"
        )
    result = answer(waiting, "question-original")
    assert not result["ok"]
    pm = finish(waiting)
    assert pm._checkpoint_error and not pm._state.clarification_response
    view = inspect_question(waiting.path, "pm-resume", "task-resume", "self", lambda: pm)
    assert view["state"] != "answered" and not view["can_reply"] and not view["response"]
    assert not answer(waiting, "question-original")["ok"]
    pm.runtime.execute.assert_not_called()


def test_concurrent_different_answers_only_one_is_saved(waiting):
    assert resume(waiting)["ok"]
    current_question(waiting)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda text: answer(waiting, "question-original", text), ["A", "B", "C", "D", "E", "F"])
        )
    assert sum(r["ok"] for r in results) == 1
    second = current_question(waiting, "question-original")
    assert answer(waiting, second["question_id"], "无")["ok"]
    pm = finish(waiting)
    assert len(pm._state.refinement["context"]) == 2


def test_secretary_restart_does_not_need_memory_worker_map(waiting):
    assert resume(waiting)["ok"]
    current_question(waiting)
    waiting.controller._pm_worker_map = {}
    waiting.controller.db.mark_tasks_for_recovery()
    result = query_question(waiting.controller, "pm-resume")
    assert result["question_id"] == "question-original" and result["can_reply"]
    assert answer(waiting, result["question_id"])["ok"]
    second = current_question(waiting, result["question_id"])
    assert answer(waiting, second["question_id"], "无")["ok"]
    finish(waiting)


def test_query_while_node_restarted_reads_question_without_launching(waiting):
    before = waiting.path.read_bytes()
    question = query_question(waiting.controller, "pm-resume")
    assert question["state"] == "waiting" and not question["can_reply"] and not question["running"]
    assert not answer(waiting, question["question_id"])["ok"]
    assert waiting.path.read_bytes() == before and not waiting.created


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_stop_preserves_saved_answer_and_rejects_new_reply(waiting, action):
    pm = ProjectManagerAgent("pm-resume", waiting.controller.chat_runtime, "http://original", "self")
    pm._state.restore(waiting.state.snapshot())
    pm._checkpoint_store = PMCheckpoints(waiting.path, owner="exited-process", lease=lease_identity(waiting.path))
    pm._checkpoint_revision = waiting.revision[0]
    waiting.controller._local_pm_agent = pm
    assert answer(waiting, "question-original")["ok"]
    getattr(pm, action)()
    saved = read_checkpoint(waiting.path, "pm-resume", "task-resume", "self")["snapshot"]
    assert saved["clarification_response"]["response"] == "使用 Python"
    assert not answer(waiting, "question-original", "修改回答")["ok"]


@pytest.mark.parametrize("bad", ["continuation", "round", "question_id", "executing"])
def test_unsafe_clarification_continuation_rejected(waiting, bad):
    if bad == "continuation":
        waiting.state.clarification_request["continuation"] = {"kind": "unknown"}
    elif bad == "round":
        waiting.state.refinement["round"] = 9
    elif bad == "question_id":
        waiting.state.clarification_request.pop("question_id")
    else:
        waiting.state.dispatched.add("already-executing")
    waiting.save()
    assert not resume(waiting)["ok"] and not waiting.created


@pytest.mark.parametrize("crash_round", [0, 1])
def test_actual_process_crash_keeps_reply_and_refinement_round(waiting, tmp_path, crash_round):
    path = tmp_path / "crash-question.sqlite3"
    task = dict(waiting.state.task)
    code = """
import json, os, sys, time
from pathlib import Path
from unittest.mock import Mock
from lan_mesh.pm_agent import ProjectManagerAgent
from lan_mesh.pm_start import start_pm_once
import lan_mesh.pm_agent as module
module.http_post=Mock()
original=ProjectManagerAgent.report_status
def report(self,status,**kwargs):
    if status=='awaiting_input':
        q=self._state.clarification_request
        if self._state.refinement['round']==0:
            ack=self.receive_input({'pm_id':self.pm_id,'task_id':'task-resume','device_id':'self',
                'question_id':q['question_id'],'response':'进程退出前已保存'})
            assert ack['ok']
        if self._state.refinement['round']==int(sys.argv[3]):
            os._exit(23)
    return original(self,status,**kwargs)
ProjectManagerAgent.report_status=report
ProjectManagerAgent._distribute_artifacts=Mock()
task=json.loads(sys.argv[2])
start_pm_once(pm_id='pm-resume',task_id='task-resume',secretary_url='http://original',task_data=task,
    runtime=Mock(),device_id='self',device_name='node',receipt_path=Path(sys.argv[1]),
    current=lambda:None,assign=lambda pm:None,lookup=lambda _:task)
time.sleep(10)
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(path), json.dumps(task), str(crash_round)], capture_output=True, timeout=15
    )
    assert proc.returncode == 23, proc.stderr.decode()
    record = read_checkpoint(path, "pm-resume", "task-resume", "self")
    assert record["snapshot"]["refinement"]["round"] == crash_round
    original_deadline = record["snapshot"]["clarification_request"]["deadline"]
    auth = prepare_resume(waiting.controller, "task-resume", record["revision"])
    result = resume_pm_once(
        payload=auth,
        receipt_path=path,
        runtime=waiting.controller.chat_runtime,
        device_id="self",
        device_name="node",
        current=lambda: waiting.controller._local_pm_agent,
        assign=lambda pm: setattr(waiting.controller, "_local_pm_agent", pm),
        authorization=lambda tid, rid: resume_authorization(waiting.controller, tid, rid),
    )
    assert result["ok"], result
    old_id = record["snapshot"]["clarification_request"]["question_id"]
    q = current_question(waiting, old_id if crash_round == 0 else "")
    if crash_round == 1:
        assert q["question_id"] == old_id and q["deadline"] == original_deadline
    assert answer(waiting, q["question_id"], "需要测试")["ok"]
    pm = finish(waiting)
    assert pm._state.task["description"].count("A1: 进程退出前已保存") == 1
    assert pm._state.task["description"].count("A2: 需要测试") == 1
    assert pm._state.start_time == record["snapshot"]["start_time"] and pm.runtime.execute.call_count == 1
    assert waiting.controller.db.get_task("task-resume").status == "completed"


@pytest.mark.parametrize("bad_id", [None, "", [], {}, 12])
def test_malformed_question_identity_never_changes_checkpoint(waiting, bad_id):
    assert resume(waiting)["ok"]
    current_question(waiting)
    pm = waiting.controller._local_pm_agent
    with pm._state.lock:
        before = waiting.path.read_bytes()
        assert not pm.receive_input({"question_id": bad_id, "response": "text"})["ok"]
        assert waiting.path.read_bytes() == before
        assert not pm._state.clarification_response


def test_lost_final_ack_can_be_confirmed_after_delivery(waiting):
    assert resume(waiting)["ok"]
    first = current_question(waiting)
    assert answer(waiting, first["question_id"])["ok"]
    second = current_question(waiting, first["question_id"])
    assert answer(waiting, second["question_id"], "最后的回答")["ok"]
    pm = finish(waiting)
    before = waiting.path.read_bytes()
    assert answer(waiting, second["question_id"], "最后的回答")["duplicate"]
    assert not answer(waiting, second["question_id"], "另一个回答")["ok"]
    assert waiting.path.read_bytes() == before
    assert pm.runtime.execute.call_count == 1


def test_chat_thread_records_only_saved_reply_and_deduplicates_retry(waiting, tmp_path):
    from lan_mesh.chat_handler import ChatHandler

    chat = ChatHandler(Mock(), waiting.controller, shared_folder=SimpleNamespace(path=str(tmp_path)))
    conv_id = chat._active_conv_id
    assert resume(waiting)["ok"]
    first = current_question(waiting)
    chat.attach_pm_thread(conv_id, "pm-resume", task_name="澄清任务", agent_name="PM")
    chat.notify_pm_clarification("pm-resume", first["question"], question_id=first["question_id"])
    chat.notify_pm_clarification("pm-resume", first["question"], question_id=first["question_id"])
    before = list(chat.get_pm_thread_messages("pm-resume"))
    assert sum(m.get("type") == "clarification" for m in before) == 1
    assert not chat.send_to_pm_thread(conv_id, "pm-resume", "保留草稿", "wrong")["ok"]
    assert chat.get_pm_thread_messages("pm-resume") == before
    assert chat.send_to_pm_thread(conv_id, "pm-resume", "保存此回答", first["question_id"])["ok"]
    second = current_question(waiting, first["question_id"])
    assert chat.send_to_pm_thread(conv_id, "pm-resume", "保存此回答", first["question_id"])["ok"]
    messages = chat.get_pm_thread_messages("pm-resume")
    assert sum(m["role"] == "user" for m in messages) == 1
    assert answer(waiting, second["question_id"], "无")["ok"]
    finish(waiting)


@pytest.mark.parametrize(
    "data,valid",
    [
        ("decideq:pm:current:1", True),
        ("decideq:pm:old:1", False),
        ("decide:pm:current:1", False),
        ("decide:pm:同意", False),
        ("decideq:pm:current:-1", False),
        ("decideq:pm:current:7", False),
    ],
)
def test_bot_buttons_bind_exact_question_and_option(monkeypatch, data, valid):
    from lan_mesh.bot_gateway import BotChannel, BotGateway

    gateway = BotGateway()
    controller = Mock()
    gateway._chat_handler = SimpleNamespace(controller=controller)
    gateway._send_telegram = Mock()
    controller.inject_input_to_pm.return_value = {"ok": True, "message": "已保存"}
    monkeypatch.setattr(
        "lan_mesh.pm_clarification.query_question",
        lambda *_: {"question_id": "current", "can_reply": True, "options": ["第一项", "第二项"]},
    )
    post = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr("lan_mesh.bot_gateway.requests.post", post)
    channel = BotChannel(channel_type="telegram", bot_token="test-token", chat_id="123")
    gateway._handle_callback_query(channel, {"id": "callback", "data": data, "message": {"chat": {"id": "123"}}})
    if valid:
        controller.inject_input_to_pm.assert_called_once_with(
            "pm", {"response": "第二项", "choice": "第二项", "source": "telegram_inline", "question_id": "current"}
        )
    else:
        controller.inject_input_to_pm.assert_not_called()
        gateway._send_telegram.assert_called_once()


@pytest.mark.parametrize("mode", ["station", "worker"])
def test_real_http_routes_question_and_answer_to_original_owner(waiting, monkeypatch, mode):
    import requests
    from fastapi import FastAPI

    from lan_mesh.http_retry import http_post
    from lan_mesh.station_controller import StationController
    from lan_mesh.station_routes_pm import build_pm_routes as secretary_routes
    from lan_mesh.station_routes_tasks import build_task_routes
    from lan_mesh.station_routes_worker import build_worker_routes
    from lan_mesh.worker import WorkerAgent
    from lan_mesh.worker_routes_pm import build_pm_routes as worker_routes

    ctl = waiting.controller
    secretary = FastAPI()
    secretary.include_router(build_task_routes(ctl))
    secretary.include_router(secretary_routes(ctl))
    monkeypatch.setattr("lan_mesh.pm_agent.http_post", http_post)
    with live_server(secretary) as secretary_port:
        node_app = FastAPI()
        if mode == "station":
            node = StationController.__new__(StationController)
            node.db = SimpleNamespace(path=waiting.path.with_name("remote.sqlite3"))
            node.state = SimpleNamespace(device_id="remote-node", device_name="remote")
            node.discovery = Mock()
            node.secretary_active = False
            node.chat_runtime = ctl.chat_runtime
            node._local_pm_agent = None
            path = node.db.path.with_name("remote-pm-starts.sqlite3")

            def current():
                return node._local_pm_agent

            node_app.include_router(build_worker_routes(node))
        else:
            node = WorkerAgent.__new__(WorkerAgent)
            node.data_dir = waiting.path.parent
            node.state = SimpleNamespace(
                device_id="remote-node", device_name="remote", pm_agent=None, agent_runtime=ctl.chat_runtime
            )
            path = node.data_dir / "worker-pm-starts.sqlite3"

            def current():
                return node.state.pm_agent

            node_app.include_router(worker_routes(node))
        PMCheckpoints(path, owner="exited-process", lease=lease_identity(path)).save(
            "pm-resume",
            "task-resume",
            "remote-node",
            {**waiting.state.snapshot(), "secretary_url": f"http://127.0.0.1:{secretary_port}"},
            0,
        )
        with live_server(node_app) as node_port:
            ctl.db.upsert_pm_agent(
                PMAgent(
                    pm_id="pm-resume",
                    task_id="task-resume",
                    device_id="remote-node",
                    ip="127.0.0.1",
                    api_port=node_port,
                )
            )
            base = f"http://127.0.0.1:{secretary_port}"

            def query():
                response = requests.get(base + "/api/pm/pm-resume/clarification", timeout=5)
                assert response.status_code == 200, response.text
                return response.json()

            def reply(qid, text, expected_status=200):
                response = requests.post(
                    base + "/api/pm/pm-resume/inject-input", json={"question_id": qid, "response": text}, timeout=5
                )
                assert response.status_code == expected_status, response.text
                return response.json()

            assert query()["question_id"] == "question-original" and not query()["can_reply"]
            result = requests.post(base + "/api/tasks/task-resume/resume", json={"expected_revision": 1}, timeout=5)
            assert result.status_code == 200 and result.json()["ok"], result.text
            eventually(lambda: query().get("can_reply"))
            ctl._pm_worker_map = {}
            assert reply("question-original", "远端回答")["ok"]
            second = eventually(
                lambda: (
                    (q if q["question_id"] != "question-original" and q["can_reply"] else None)
                    if (q := query())
                    else None
                )
            )
            assert reply("question-original", "远端回答")["duplicate"]
            assert reply("question-original", "错误覆盖", 409)["detail"]
            assert not current()._state.clarification_response
            assert reply(second["question_id"], "远端第二答")["ok"]
            current()._thread.join(timeout=4)
            current()._progress_thread.join(timeout=4)
            assert not current().running
            assert ctl.db.get_task("task-resume").status == "completed"
            assert current()._state.task["description"].count("A1: 远端回答") == 1
            assert current()._state.task["description"].count("A2: 远端第二答") == 1


def test_conversation_thread_api_preserves_question_id_and_failure(waiting, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lan_mesh.chat_handler import ChatHandler
    from lan_mesh.station_routes_chat import build_chat_routes

    chat = ChatHandler(Mock(), waiting.controller, shared_folder=SimpleNamespace(path=str(tmp_path)))
    waiting.controller.chat_handler = chat
    conv_id = chat._active_conv_id
    app = FastAPI()
    app.include_router(build_chat_routes(waiting.controller))
    client = TestClient(app)
    assert resume(waiting)["ok"]
    first = current_question(waiting)
    endpoint = f"/api/conversations/{conv_id}/messages"
    failed = client.post(endpoint, json={"message": "不带编号", "pm_thread_id": "pm-resume"})
    assert failed.status_code == 200 and not failed.json()["ok"]
    assert not waiting.controller._local_pm_agent._state.clarification_response
    result = client.post(
        endpoint, json={"message": "接口回答", "pm_thread_id": "pm-resume", "question_id": first["question_id"]}
    )
    assert result.status_code == 200 and result.json()["ok"]
    assert result.json()["question_id"] == first["question_id"]
    second = current_question(waiting, first["question_id"])
    assert answer(waiting, second["question_id"], "无")["ok"]
    finish(waiting)


def test_chat_retries_record_reply_when_node_ack_was_lost(waiting, tmp_path, monkeypatch):
    from lan_mesh.chat_handler import ChatHandler

    chat = ChatHandler(Mock(), waiting.controller, shared_folder=SimpleNamespace(path=str(tmp_path)))
    assert resume(waiting)["ok"]
    first = current_question(waiting)
    original = waiting.controller._local_inject_input

    def lose_ack(payload):
        assert original(payload)["ok"]
        return {"ok": False, "message": "节点确认丢失"}

    monkeypatch.setattr(waiting.controller, "_local_inject_input", lose_ack)
    assert not chat.send_to_pm_thread(chat._active_conv_id, "pm-resume", "已落盘回答", first["question_id"])["ok"]
    assert not any(m["role"] == "user" for m in chat.get_pm_thread_messages("pm-resume"))
    second = current_question(waiting, first["question_id"])
    monkeypatch.setattr(waiting.controller, "_local_inject_input", original)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: chat.send_to_pm_thread(chat._active_conv_id, "pm-resume", "已落盘回答", first["question_id"]),
                range(4),
            )
        )
    assert all(r["ok"] for r in results)
    assert sum(not r["duplicate"] for r in results) == 1
    assert sum(m["role"] == "user" for m in chat.get_pm_thread_messages("pm-resume")) == 1
    assert not waiting.controller._local_pm_agent._state.clarification_response
    assert answer(waiting, second["question_id"], "无")["ok"]
    finish(waiting)
