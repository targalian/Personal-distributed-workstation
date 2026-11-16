"""Meaningful offline benchmark validation behavior."""
import copy, json, math, subprocess, sys
import pytest
from pathlib import Path
from scripts.task_benchmark import load_tasks, template, validate, compare, digest
TASKS=load_tasks(Path("benchmarks/tasks.json"))
def record(task_id="sum-v1", run_id="r1"):
 r=template(TASKS); r.update(task_id=task_id,run_id=run_id, input_hash=TASKS[task_id]["input_hash"], acceptance_hash=TASKS[task_id]["acceptance_hash"]); return r

def test_valid_template(): assert validate([record()],TASKS)["valid"]
def test_hash_mismatch():
 r=record(); r["input_hash"]="0"*64; assert not validate([r],TASKS)["valid"]
def test_duplicate_ids():
 assert not validate([record(),record()],TASKS)["valid"]
def test_list_task_id():
 r=record(); r["task_id"]=[]; assert not validate([r],TASKS)["valid"]
def test_missing_field():
 r=record(); del r["model"]; assert not validate([r],TASKS)["valid"]
def test_empty_fields():
 r=record(); r["system_version"]=" "; assert not validate([r],TASKS)["valid"]
def test_model_name_and_parameters():
 for v in ({"name":"","parameters":{}},{"name":"x","parameters":[]},{"name":"x"}):
  r=record(); r["model"]=v; assert not validate([r],TASKS)["valid"]
def test_bool_numbers():
 for field in ("attempt","duration_ms"):
  r=record(); r[field]=True; assert not validate([r],TASKS)["valid"]
def test_nan_inf_negative():
 for value in (float("nan"),float("inf"),-1):
  r=record(); r["duration_ms"]=value; assert not validate([r],TASKS)["valid"]
def test_usage_types():
 r=record(); r["usage"]["input_tokens"]=-1; assert not validate([r],TASKS)["valid"]
def test_evidence_requires_metadata():
 r=record(); r["acceptance_evidence"]={"status":"verified","refs":["x"]}; assert not validate([r],TASKS)["valid"]
def test_verified_evidence():
 r=record(); r["acceptance_evidence"]={"status":"verified","refs":["obs-1"],"verification":{"method":"sha256","verifier":"tester","verified_at":"2026-01-01"}}; assert validate([r],TASKS)["valid"]
def test_completed_enum_rejected():
 r=record(); r["acceptance_evidence"]={"status":"completed","refs":[]}; assert not validate([r],TASKS)["valid"]
def test_recovery_enum():
 r=record(); r["recovery_conclusion"]="done"; assert not validate([r],TASKS)["valid"]
def test_catalog_tamper(tmp_path):
 p=tmp_path/"tasks.json"; d=json.loads(Path("benchmarks/tasks.json").read_text()); d["tasks"][0]["acceptance_criteria"][0]="tampered"; p.write_text(json.dumps(d));
 try: load_tasks(p); assert False
 except ValueError: pass
def test_groups_model_parameters_acceptance():
 a=record("sum-v1","a"); b=record("sum-v1","b"); b["model"]["parameters"]={"temperature":1}; c=record("sort-v1","c"); out=compare([a,b,c],TASKS); assert out["valid"] and len(out["groups"])==3
def test_fixture_isolation():
 a=record("sum-v1","a"); b=record("sum-v1","b"); b["fixture"]=False; assert len(compare([a,b],TASKS)["groups"])==2
def test_mixed_currency():
 a=record("sum-v1","a"); b=record("sum-v1","b"); a["usage"].update(cost=1,currency="USD"); b["usage"].update(cost=2,currency="CNY"); row=compare([a,b],TASKS)["groups"][0]; assert not row["comparable"] and row["currency"]=="mixed"
def test_statistics():
 rs=[record("sum-v1",str(i)) for i in range(4)]
 for i,r in enumerate(rs): r["duration_ms"]=i*10; r["human_intervention"]=i%2==0; r["recovery_conclusion"]="succeeded" if i<3 else "failed"
 rs[0]["acceptance_evidence"]={"status":"verified","refs":["x"],"verification":{"method":"m","verifier":"v","verified_at":"t"}}
 row=compare(rs,TASKS)["groups"][0]; assert row["completion_rate"]==.25 and row["duration_ms"]["mean"]==15 and row["human_intervention_rate"]==.5 and row["recovery_success_rate"]==.75
def test_invalid_compare_no_stats():
 r=record(); r["attempt"]=True; out=compare([r],TASKS); assert out["valid"] is False and out["groups"]==[]
def test_malformed_container(): assert not validate({"records":[]},TASKS)["valid"]
def test_cli_invalid_nonzero(tmp_path):
 p=tmp_path/"bad.json"; p.write_text("[]"); x=subprocess.run([sys.executable,"scripts/task_benchmark.py","compare","--input",str(p)],capture_output=True,text=True); assert x.returncode!=0 and "Traceback" not in x.stderr


@pytest.mark.parametrize("field", ["recovery_conclusion", "acceptance_evidence"])
def test_unhashable_enums_rejected(field):
    r = record()
    r[field] = [] if field == "recovery_conclusion" else {"status": [], "refs": [], "verification": None}
    assert not compare([r], TASKS)["valid"]

@pytest.mark.parametrize("field", ["usage", "model", "acceptance_evidence"])
def test_missing_nested_fields_rejected(field):
    r = record(); r[field] = {}
    assert not validate([r], TASKS)["valid"]

@pytest.mark.parametrize("container", ["record", "model", "usage", "acceptance_evidence"])
def test_extra_fields_rejected(container):
    r = record()
    (r if container == "record" else r[container])["unexpected"] = 1
    assert not validate([r], TASKS)["valid"]

def test_unassessed_recovery_not_counted_and_unknown_cost_not_zero():
    a, b = record(run_id="a"), record(run_id="b")
    a["recovery_conclusion"] = "succeeded"
    a["usage"].update(cost=2, currency="USD")
    out = compare([a,b], TASKS)["groups"][0]
    assert out["recovery_assessed_count"] == 1 and out["recovery_success_rate"] == 1
    assert out["cost_total"] is None and out["cost_known_total"] == 2
    assert out["cost_known_coverage"] == .5 and not out["comparable"]
    assert compare([b], TASKS)["groups"][0]["recovery_success_rate"] is None

def test_catalog_direct_mutation_cannot_bypass_compare():
    tasks = copy.deepcopy(TASKS); tasks["sum-v1"]["input"] = "changed"
    assert not compare([record()], tasks)["valid"]

def test_empty_catalog_and_boolean_schema_rejected(tmp_path):
    data=json.loads(Path("benchmarks/tasks.json").read_text())
    for key,value in [("tasks",[]),("schema_version",True)]:
        modified=copy.deepcopy(data); modified[key]=value
        p=tmp_path/"tasks.json"; p.write_text(json.dumps(modified))
        with pytest.raises(ValueError): load_tasks(p)

def test_cli_template_roundtrip_and_missing_input(tmp_path):
    p=tmp_path/"record.json"
    command=[sys.executable,"scripts/task_benchmark.py"]
    assert subprocess.run(command+["template","--output",str(p)],capture_output=True).returncode == 0
    assert subprocess.run(command+["validate","--input",str(p)],capture_output=True).returncode == 0
    result=subprocess.run(command+["compare"],capture_output=True,text=True)
    assert result.returncode != 0 and "Traceback" not in result.stderr
