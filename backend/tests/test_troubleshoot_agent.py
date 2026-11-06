import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from openai import BadRequestError

from app.troubleshoot.agent import ModelError, OpenAIToolModel, RunBusy, TroubleshootAgent, clip, compact, redact
from app.troubleshoot.policy import ALLOW, ASK, DENY, Policy, Profile
from app.troubleshoot.skills import SkillError, SkillLibrary, parse_skill

PATH = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}
NONE = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
TOOLS = {
    "read_file": {"writes": False, "description": "read", "parameters": PATH},
    "delete_path": {"writes": True, "description": "delete", "parameters": PATH},
    "write_file": {"writes": True, "description": "write", "parameters": PATH},
    "edit_file": {"writes": True, "description": "edit", "parameters": PATH},
    "chmod_path": {"writes": True, "description": "chmod", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}}, "required": ["path", "mode"]}},
    "kill_process": {"writes": True, "description": "kill", "parameters": {
        "type": "object", "properties": {"pid": {"type": "integer"}}, "required": ["pid"]}},
    "run_script": {"writes": True, "description": "script", "parameters": PATH},
    "git_action": {"writes": True, "description": "git", "parameters": NONE},
}
PROFILE = Profile(services=("api",), health_urls=("http://x/health",), repos=(), protected=("/srv/data/*.db",),
                  scripts=("/opt/app/manage.py",))


def call(tool, index=0, /, **arguments):
    return {"id": f"c{index}-{tool}", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}


def finish(resolved=True):
    return call("finish", 99, root_cause="r", actions="a", resolved=resolved)


class Script:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []
        self.forced = []

    def __call__(self, messages, tools, force_finish):
        self.requests.append(list(messages))
        self.forced.append(force_finish)
        turn = self.turns.pop(0) if self.turns else [finish()]
        return {"message": {"content": "", "tool_calls": turn}, "prompt_tokens": 1, "completion_tokens": 1}


class Backend:
    def __init__(self, healthy=False, pid=7):
        self.healthy, self.pid = healthy, pid
        self.calls = []

    def __call__(self, name, arguments):
        if name == "service_status":
            return (f"api：运行中，pid={self.pid}\n日志" if self.healthy else "api：未运行\n日志"), True
        if name == "http_check":
            return ("HTTP 200\nok" if self.healthy else "HTTP 502\nbad"), True
        self.calls.append((name, arguments))
        return f"ran {name}", True


def agent(tmp_path, model, backend=None, policy=None, **options):
    return TroubleshootAgent(tmp_path / "a.db", model=model, backend=backend or Backend(), tools=TOOLS, policy=policy, **options)


def tool_outputs(model):
    return [message["content"] for message in model.requests[-1] if message["role"] == "tool"]


def test_tool_failure_returns_to_model(tmp_path):
    def broken(name, arguments):
        raise OSError("gone")

    model = Script([call("read_file", path="/a")], [finish()])
    view = agent(tmp_path, model, broken).start("task")
    assert view.status == "completed"
    assert "工具执行失败" in tool_outputs(model)[0]


@pytest.mark.parametrize("bad", [call("nope"), call("read_file"), call("read_file", path=1), call("read_file", path="/a", x=1)])
def test_invalid_call_returns_to_model(tmp_path, bad):
    model = Script([bad], [finish()])
    view = agent(tmp_path, model).start("task")
    assert view.status == "completed"
    assert view.stats["invalid_calls"] == 1
    assert tool_outputs(model)[0].startswith("错误：")


def test_broken_json_arguments_return_to_model(tmp_path):
    bad = {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{oops"}}
    model = Script([bad], [finish()])
    assert agent(tmp_path, model).start("task").status == "completed"


def test_secrets_are_redacted_and_run_continues(tmp_path):
    leaked = "password=hunter2 url=postgres://u:pw@db/x Bearer abcdefghijk sk-abcdefghijklmnopqrstuv"
    model = Script([call("read_file", path="/a")], [finish()])
    view = agent(tmp_path, model, lambda name, arguments: (leaked, True)).start("task")
    output = tool_outputs(model)[0]
    assert view.status == "completed"
    assert not any(secret in output for secret in ("hunter2", ":pw@", "abcdefghijk", "sk-abc"))


def test_redact_keeps_plain_mentions():
    assert redact("ERROR invalid token in request") == "ERROR invalid token in request"


def test_long_output_is_clipped_and_run_continues(tmp_path):
    model = Script([call("read_file", path="/a")], [finish()])
    view = agent(tmp_path, model, lambda name, arguments: ("x" * 50000, True)).start("task")
    assert view.status == "completed"
    assert len(tool_outputs(model)[0]) < 6100
    assert "省略" in clip("x" * 7000)


def test_refused_approval_returns_to_model(tmp_path):
    model = Script([call("delete_path", path="/tmp/a")], [finish()])
    backend = Backend()
    instance = agent(tmp_path, model, backend, Policy(PROFILE, TOOLS))
    view = instance.start("task")
    assert view.status == "awaiting_approval"
    assert view.pending["name"] == "delete_path"
    view = instance.resume(view.run_id, False, "数据还要保留。")
    assert view.status == "completed"
    assert "用户拒绝" in tool_outputs(model)[0]
    assert not backend.calls


def test_approval_survives_restart_and_runs_once(tmp_path):
    model = Script([call("delete_path", path="/tmp/a")], [finish()])
    backend = Backend()
    first = agent(tmp_path, model, backend, Policy(PROFILE, TOOLS))
    run_id = first.start("task").run_id
    first.close()
    second = agent(tmp_path, model, backend, Policy(PROFILE, TOOLS))
    assert second.get(run_id).status == "awaiting_approval"
    view = second.resume(run_id, True)
    assert view.status == "completed"
    assert backend.calls == [("delete_path", {"path": "/tmp/a"})]


def test_started_write_without_result_is_not_repeated(tmp_path):
    backend = Backend()
    instance = agent(tmp_path, Script(), backend)
    run_id = instance.create("task")
    with instance._db() as db:
        db.execute("INSERT INTO troubleshoot_calls VALUES (?, 'c', 'started', NULL, NULL)", (run_id,))
    output, succeeded = instance._call_once(run_id, "c", "delete_path", {"path": "/a"})
    assert "结果未知" in output and not succeeded
    assert not backend.calls


def test_status_second_run_and_cancel_work_during_a_run(tmp_path):
    release = threading.Event()

    def slow(name, arguments):
        release.wait(5)
        return "ok", True

    model = Script([call("read_file", path="/a")], [call("read_file", path="/b")])
    instance = agent(tmp_path, model, slow)
    run_id = instance.create("first")
    other = instance.create("second")
    result = {}
    thread = threading.Thread(target=lambda: result.update(view=instance.run(run_id)))
    thread.start()
    time.sleep(0.3)
    assert instance.get(run_id).status == "running"
    assert instance.get(other).status == "running"
    with pytest.raises(RunBusy):
        instance.run(run_id)
    instance.cancel(run_id)
    release.set()
    thread.join(5)
    assert result["view"].status == "cancelled"


def test_cancel_while_waiting_for_approval(tmp_path):
    instance = agent(tmp_path, Script([call("delete_path", path="/tmp/a")]), Backend(), Policy(PROFILE, TOOLS))
    view = instance.start("task")
    assert instance.cancel(view.run_id).status == "cancelled"


def test_repeated_call_is_not_rerun_until_something_changes(tmp_path):
    backend = Backend()
    model = Script([call("read_file", path="/a")], [call("read_file", path="/a")], [call("delete_path", path="/tmp/x")],
                   [call("read_file", path="/a")], [finish()])
    view = agent(tmp_path, model, backend).start("task")
    assert view.stats["repeated_calls"] == 1
    assert [name for name, _ in backend.calls] == ["read_file", "delete_path", "read_file"]


def test_looping_tool_leaves_the_menu_for_one_turn(tmp_path):
    seen_tools = []

    class Recording(Script):
        def __call__(self, messages, tools, force_finish):
            seen_tools.append({item["function"]["name"] for item in tools})
            return super().__call__(messages, tools, force_finish)

    same = [call("read_file", path="/a")]
    model = Recording(same, same, same, [call("delete_path", path="/tmp/x")], [finish()])
    view = agent(tmp_path, model).start("task")
    assert "read_file" in seen_tools[2]
    assert "read_file" not in seen_tools[3]
    assert "read_file" in seen_tools[4]
    assert view.status == "completed"


def test_budget_forces_finish_on_last_step(tmp_path):
    model = Script(*[[call("read_file", i, path=f"/{i}")] for i in range(3)])
    view = agent(tmp_path, model, max_steps=4).start("task")
    assert model.forced == [False, False, False, True]
    assert view.status == "completed"


def test_budget_exhausted_when_model_never_finishes(tmp_path):
    model = Script(*[[call("read_file", i, path=f"/{i}")] for i in range(5)])
    assert agent(tmp_path, model, max_steps=3).start("task").status == "budget_exhausted"


def test_provider_failure_ends_run_with_code(tmp_path):
    def failing(messages, tools, force_finish):
        raise ModelError("provider_503")

    view = agent(tmp_path, failing).start("task")
    assert (view.status, view.error) == ("failed", "provider_503")


def test_finish_is_rejected_until_probes_pass_then_downgraded(tmp_path):
    model = Script([finish()], [finish()], [finish()])
    view = agent(tmp_path, model, Backend(healthy=False), Policy(PROFILE, TOOLS)).start("task")
    assert view.stats["finish_rejected"] == 2
    assert view.report["resolved"] is False
    assert view.report["verified"] is False


def test_finish_passes_when_probes_are_healthy(tmp_path):
    view = agent(tmp_path, Script([finish()]), Backend(healthy=True), Policy(PROFILE, TOOLS)).start("task")
    assert view.report["resolved"] is True and view.report["verified"] is True


def test_policy_rules_are_not_affected_by_model_text(tmp_path):
    policy = Policy(PROFILE, TOOLS)
    sick, well = Backend(healthy=False), Backend(healthy=True)
    assert policy.review("read_file", {"path": "/srv/data/orders.db"}, sick).action == ALLOW
    assert policy.review("delete_path", {"path": "/srv/data/orders.db"}, sick).action == DENY
    assert policy.review("delete_path", {"path": "/srv/data/../data/orders.db"}, sick).action == DENY
    assert policy.review("delete_path", {"path": "/srv/data"}, sick).action == DENY
    assert policy.review("delete_path", {"path": "/srv"}, sick).action == DENY
    assert policy.review("delete_path", {"path": "/srv/data/old.log.gz"}, sick).action == ASK
    assert policy.review("write_file", {"path": "/srv/data/orders.db"}, sick).action == DENY
    assert policy.review("edit_file", {"path": "/opt/app/main.py"}, sick).action == DENY
    assert policy.review("edit_file", {"path": "/opt/app/.env"}, sick).action == ASK
    assert policy.review("chmod_path", {"path": "/srv/data", "mode": "777"}, sick).action == DENY
    assert policy.review("chmod_path", {"path": "/srv/data/orders.db", "mode": "644"}, sick).action == ASK
    assert policy.review("run_script", {"path": "/opt/app/reset_db.py"}, sick).action == DENY
    assert policy.review("run_script", {"path": "/opt/app/manage.py"}, sick).action == ASK
    assert policy.review("edit_file", {"path": "/opt/app/fix.sh"}, sick).action == DENY
    assert policy.review("edit_file", {"path": "/opt/app/.env"}, well).action == DENY
    assert policy.review("git_action", {}, well).action == ASK


def test_policy_blocks_killing_managed_services_only(tmp_path):
    policy = Policy(PROFILE, TOOLS)
    backend = Backend(healthy=True, pid=7)
    assert policy.review("kill_process", {"pid": 7}, backend).action == DENY
    unhealthy = Backend(healthy=False)
    assert policy.review("kill_process", {"pid": 8}, unhealthy).action == ASK


def test_declared_script_can_run_more_than_once(tmp_path):
    backend = Backend()
    model = Script([call("run_script", path="/opt/app/manage.py")], [call("delete_path", path="/tmp/a")],
                   [call("run_script", 1, path="/opt/app/manage.py")], [finish(resolved=False)])
    instance = agent(tmp_path, model, backend, Policy(PROFILE, TOOLS))
    view = instance.start("task")
    while view.status == "awaiting_approval":
        view = instance.resume(view.run_id, True)
    assert view.stats["denied"] == 0
    assert [name for name, _ in backend.calls].count("run_script") == 2


def test_compact_shortens_oldest_tool_results_only():
    messages = [{"role": "system", "content": "s" * 100}, {"role": "tool", "content": "a" * 5000},
                {"role": "tool", "content": "b" * 5000}, {"role": "tool", "content": "c" * 500}]
    result = compact(messages, limit=6000)
    assert len(result[1]["content"]) < 400
    assert result[0] == messages[0]
    assert result[2]["content"] == "b" * 5000
    assert messages[1]["content"] == "a" * 5000


def test_probe_failure_fails_closed_and_keeps_run_alive(tmp_path):
    def broken(name, arguments):
        raise OSError("sandbox gone")

    model = Script([call("kill_process", pid=8)], [finish()], [finish()], [finish()])
    view = agent(tmp_path, model, broken, Policy(PROFILE, TOOLS)).start("task")
    assert view.stats["denied"] == 1
    assert view.status == "completed"
    assert view.report["resolved"] is False


def test_policy_denial_returns_to_model(tmp_path):
    backend = Backend()
    model = Script([call("delete_path", path="/srv/data/orders.db")], [finish(resolved=False)])
    view = agent(tmp_path, model, backend, Policy(PROFILE, TOOLS)).start("task")
    assert view.stats["denied"] == 1
    assert "策略拒绝" in tool_outputs(model)[0]
    assert not backend.calls


def write_skill(root, name, text=None):
    folder = root / name
    folder.mkdir()
    (folder / "SKILL.md").write_text(text or f"---\nname: {name}\ndescription: 何时使用\n---\n步骤 {name}\n", encoding="utf-8")
    return folder


def test_catalog_lists_descriptions_and_body_loads_on_demand(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    write_skill(root, "disk-full")
    library = SkillLibrary(root)
    assert "disk-full：何时使用" in library.catalog()
    assert "步骤 disk-full" not in library.catalog()
    model = Script([call("load_skill", name="disk-full")], [call("load_skill", 1, name="nope")], [finish()])
    view = agent(tmp_path, model, skills=library).start("task")
    assert model.requests[0][0]["content"].count("disk-full") == 1
    assert tool_outputs(model)[0] == "步骤 disk-full"
    assert tool_outputs(model)[1].startswith("错误：没有技能")
    assert view.stats["skill_loads"] == 1


def test_load_skill_needs_no_policy_review_but_grants_nothing(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    write_skill(root, "wipe", "---\nname: wipe\ndescription: d\nallowed-tools: delete_path\n---\n删除业务数据\n")
    backend = Backend()
    model = Script([call("load_skill", name="wipe")], [call("delete_path", path="/srv/data/orders.db")], [finish(resolved=False)])
    view = agent(tmp_path, model, backend, Policy(PROFILE, TOOLS), skills=SkillLibrary(root)).start("task")
    assert view.stats["denied"] == 1
    assert not backend.calls


@pytest.mark.parametrize("text,code", [
    ("没有头部", "missing_frontmatter"),
    ("---\nname: other\ndescription: d\n---\nx", "invalid_name"),
    ("---\nname: bad\n---\nx", "invalid_description"),
    ("---\nname: bad\ndescription: " + "d" * 2000 + "\n---\nx", "invalid_description"),
    ("---\nname: bad\ndescription: d\n---\n" + "x" * 20000, "too_large"),
])
def test_invalid_skills_are_rejected(tmp_path, text, code):
    write_skill(tmp_path, "bad", text)
    write_skill(tmp_path, "good")
    library = SkillLibrary(tmp_path)
    assert library.rejected == {"bad": code}
    assert list(library.skills) == ["good"]


def test_symlinked_skill_is_rejected(tmp_path, monkeypatch):
    folder = write_skill(tmp_path, "linked")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "SKILL.md")
    with pytest.raises(SkillError, match="symlink"):
        parse_skill(folder, tmp_path.resolve())


def test_bundled_skills_are_all_valid():
    import app.troubleshoot as package
    library = SkillLibrary(Path(package.__file__).parent / "skills")
    assert not library.rejected
    assert len(library.skills) == 9


class FakeCompletions:
    def __init__(self):
        self.choices = []

    def create(self, **body):
        self.choices.append(body["tool_choice"])
        if body["tool_choice"] != "auto":
            raise BadRequestError("nope", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
        message = type("Message", (), {"model_dump": lambda self, exclude_none: {"content": "hi"}})()
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})()], "usage": None})()


def test_model_adapter_falls_back_when_forced_finish_is_rejected():
    completions = FakeCompletions()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    model = OpenAIToolModel(client, "m")
    assert model([], [], True)["message"]["content"] == "hi"
    model([], [], True)
    assert completions.choices == [{"type": "function", "function": {"name": "finish"}}, "auto", "auto"]
