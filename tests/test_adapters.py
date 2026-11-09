"""适配层：外部世界的形状有没有被翻错。

这里不问编排（那是 `test_app.py` 的事），只问翻译：线格式与请求体、jsonl 轨迹与旧文件兼容、
真实 PowerShell、真实钉死的解释器、真起一个 WebApi，最后一节是真 spark（`-m live`）。

`test_adapters_implement_every_port_method` 是显式继承（`class OpenAIClient(ModelClient)`）的保险：
Protocol 的方法体是 `...`，实现者漏写一个方法会静默返回 `None` 而不是报错。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest
from conftest import BASE, SETTINGS, FakeClient, FakeStore, reply

from adapters.api.web import WebApi
from adapters.locks import WorkspaceLock, WorkspaceLocked, pid_alive, process_state
from adapters.models.anthropic_client import AnthropicClient
from adapters.models.google_client import GoogleClient
from adapters.models.no_review import NoReviewClient
from adapters.models.openai_client import EFFORT_TEMPERATURE, OpenAIClient, parse_decision
from adapters.stores.jsonl import JsonlSessionStore, session_slug
from adapters.tools.python import PythonTool
from adapters.tools.shell import ShellTool
from adapters.tools.windows import _strip_clixml, conda_env, conda_python
from adapters.wire import STORE_ONLY_KEYS, from_wire, message_row, request_message, session_row, to_wire
from app.ports import Effort, ModelClient, ModelSettings, SessionStore
from app.service import AgentService
from domain.context import Context
from domain.roles import Assistant, Decision, ToolCallReviewer
from domain.session import AssistantMessage, Session, ToolCall, ToolResult, UserMessage
from domain.tool import Tool


def body(content: str) -> dict:
    return json.loads(content)


MESSAGES = [UserMessage("在中文里也没问题"),
            AssistantMessage("先看时间", (ToolCall("c1", "shell", {"command": "Get-Date"}),)),
            AssistantMessage(""),  # 纯 tool_call 的回复，content 不能变成 None
            ToolResult("c1", '{"returncode": 0}', review_ms=11234.5)]


# === 线格式：一份形状，两个用途 ==============================================


@pytest.mark.parametrize("message", MESSAGES)
def test_wire_roundtrip_is_lossless(message):
    assert from_wire(to_wire(message)) == message


@pytest.mark.parametrize("message", MESSAGES)
def test_request_body_never_carries_storage_only_keys(message):
    """多一个键就多改一次发给模型的字节：spark 的前缀缓存实测一个字之差从 0.08s 变 6.3s。"""
    row = request_message(to_wire(message))
    assert not (set(row) & set(STORE_ONLY_KEYS)), row
    assert set(row) <= {"role", "content", "tool_calls", "tool_call_id"}


def test_storage_rows_keep_the_keys_the_request_body_must_drop():
    row = message_row(MESSAGES[3])
    assert row["kind"] == "message" and row["type"] == "ToolResult"
    assert row["review_ms"] == 11234.5 and row["ts"] == 0.0
    assert request_message(row) == {"role": "tool", "tool_call_id": "c1", "content": '{"returncode": 0}'}
    assert session_row(Session(id="s9")) == {"kind": "session", "id": "s9"}


def test_legacy_rows_without_the_new_fields_still_load():
    """旧轨迹没有 ts / review_ms：缺字段取默认值，不许 KeyError。"""
    legacy = {"kind": "message", "type": "AssistantMessage", "role": "assistant", "content": "看时间",
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "shell", "arguments": '{"command":"Get-Date"}'}}]}
    message = from_wire(legacy)
    assert message.tool_calls[0].arguments == {"command": "Get-Date"} and message.ts == 0.0
    assert from_wire({"kind": "message", "role": "tool", "tool_call_id": "c1", "content": "ok"}).review_ms is None


def test_bad_tool_arguments_in_a_stored_row_become_empty_arguments():
    row = {"kind": "message", "role": "assistant", "content": "",
           "tool_calls": [{"id": "c1", "function": {"name": "shell", "arguments": "{oops"}}]}
    assert from_wire(row).tool_calls[0].arguments == {}


# === 模型适配器：Context -> 请求体（不打网络）================================


def new_client() -> OpenAIClient:
    return OpenAIClient("http://127.0.0.1:11434/v1", "")  # 本地端点没有 key，占位符即可


def test_payload_puts_the_instruction_first_and_translates_history():
    context = Context("系统指令", (), [UserMessage("干活"), ToolResult("c1", "ok")], AssistantMessage)
    messages = new_client().payload(context, SETTINGS)["messages"]
    assert messages[0] == {"role": "system", "content": "系统指令"}
    assert [m["role"] for m in messages[1:]] == ["user", "tool"]


def test_payload_switches_to_json_mode_only_for_decisions():
    review = new_client().payload(Context("r", (), [UserMessage("x")], Decision),
                                  ModelSettings("m", Effort.LOW))
    assert review["response_format"] == {"type": "json_object"} and "tools" not in review
    assert review["temperature"] == EFFORT_TEMPERATURE[Effort.LOW]


def test_payload_advertises_tools_when_answering_the_user(workdir):
    context = Context("i", (ShellTool(workdir), PythonTool(workdir)), [], AssistantMessage)
    payload = new_client().payload(context, SETTINGS)
    assert payload["tool_choice"] == "auto" and "response_format" not in payload
    assert [t["function"]["name"] for t in payload["tools"]] == ["shell", "python"]


def test_payload_survives_two_turn_history_end_to_end(workdir, make_service):
    """请求体由真实编排出来的历史生成：审核文本、工具结果、时间戳都得能穿过去。"""
    call = ToolCall("c1", "shell", {"command": "Get-Date"})
    service, _, _ = make_service(workdir, replies=[reply(call), reply(text="完成")])
    session = service.new_session()
    service.chat(session.id, "看时间")
    stored = service.session_store.load(session.id)
    payload = new_client().payload(Assistant("i", ()).context(stored), SETTINGS)
    assert [m["role"] for m in payload["messages"][1:]] == ["user", "assistant", "tool", "assistant"]
    assert all(not (set(m) & set(STORE_ONLY_KEYS)) for m in payload["messages"])


def test_parse_turns_a_wire_reply_into_domain_objects():
    """id 缺失时补一个：ToolResult 靠它回填，缺 id 会让整条轨迹断链。"""
    wire = SimpleNamespace(content="看一下",
                           tool_calls=[SimpleNamespace(id=None,
                                                       function=SimpleNamespace(name="shell",
                                                                                arguments='{"command":"dir"}'))])
    message = OpenAIClient.parse(wire, AssistantMessage)
    assert message.text == "看一下" and len(message.tool_calls) == 1
    assert message.tool_calls[0].id and message.tool_calls[0].arguments == {"command": "dir"}


@pytest.mark.parametrize("text,allow,reason", [
    ('{"allow": true, "reason": ""}', True, None),
    ('{"allow": false, "reason": "越界写入"}', False, "越界写入"),
    ('我的判断是：{"allow": true, "reason": "只读"}', True, "只读"),  # 不守约定也要读出来
])
def test_parse_decision_reads_the_json_contract(text, allow, reason):
    assert parse_decision(text) == Decision(allow, reason)


@pytest.mark.parametrize("text", ["", "我需要更多信息才能判断", '{"reason": "忘了 allow"}'])
def test_unparsable_review_is_a_denial(text):
    """解析不出来只能拒：审核器沉默不等于放行。"""
    assert parse_decision(text).should_allow is False


# === 契约：显式继承的代价由测试兜住 ==========================================

CONTRACTS = [(ModelClient, OpenAIClient), (ModelClient, AnthropicClient), (ModelClient, GoogleClient),
             (ModelClient, NoReviewClient), (SessionStore, JsonlSessionStore), (Tool, ShellTool),
             (Tool, PythonTool)]


@pytest.mark.parametrize("port,adapter", CONTRACTS)
def test_adapters_declare_every_port_method_themselves(port, adapter):
    """漏写方法会静默拿到 Protocol 的 `...`（返回 None），所以逐个方法点名。"""
    assert port in adapter.__mro__  # 本项目选择把继承写出来，读代码时一眼看出谁实现谁
    declared = set()
    for klass in adapter.__mro__:
        declared.update(vars(klass))
    for name in (n for n in dir(port) if not n.startswith("_")):
        assert name in declared, f"{adapter.__name__} 没有自己实现 {name}"


def test_no_review_client_approves_instantly_without_leaving_the_process():
    """`--review off` 的替身：零网络、零等待，但审计文本必须说清"没人审"。"""
    started = time.perf_counter()
    decision = NoReviewClient().generate(Context("i", (), [], Decision), SETTINGS)
    assert time.perf_counter() - started < 0.05
    assert isinstance(decision, Decision) and decision.should_allow is True
    assert "审核已关闭" in decision.reason


def test_vendor_adapters_without_an_endpoint_fail_loudly_instead_of_lying():
    """本机没有 Anthropic / Gemini 通路（SDK 未装、无 key、Ollama 也不提供兼容端点）：
    空位必须老实报错，不能被当成可用实现。"""
    context = Context("i", (), [UserMessage("x")], AssistantMessage)
    for adapter in (AnthropicClient(), GoogleClient()):
        with pytest.raises(NotImplementedError, match="照 adapters/models/openai_client.py"):
            adapter.generate(context, SETTINGS)


# === 存储适配器：jsonl 轨迹 ===================================================


def test_store_writes_a_header_row_and_one_row_per_message(workdir):
    call = ToolCall("c", "shell", {"command": "dir"})
    session = Session("s1", [UserMessage("写文件"), AssistantMessage("", (call,))])
    store = JsonlSessionStore(workdir)
    store.save(session)
    rows = [json.loads(line) for line in store.path("s1").read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"kind": "session", "id": "s1"}
    assert [r["type"] for r in rows[1:]] == ["UserMessage", "AssistantMessage"]


def test_store_roundtrip_survives_reopening_the_root(workdir):
    store = JsonlSessionStore(workdir)
    session = Session(id="s2", messages=list(MESSAGES))
    store.save(session)
    assert JsonlSessionStore(workdir).load("s2").messages == session.messages
    assert [s.id for s in JsonlSessionStore(workdir).list()] == ["s2"]


LEGACY_ROWS = [
    # 上一版 shape：行里没有 `segment`，也没有 `prompt_tokens/completion_tokens/cached_tokens`
    '{"kind": "session", "id": "legacy-0001"}',
    '{"kind": "message", "type": "UserMessage", "role": "user", "content": "写个报告", "ts": 1.0}',
    '{"kind": "message", "type": "AssistantMessage", "role": "assistant", "content": "好", "ts": 2.0}',
]


def test_store_reads_trajectories_written_by_the_previous_shape(workdir):
    """兼容旧文件：重构改的是代码，不该让用户手里的 sessions/ 变成砖。

    旧 shape 现在**自己造一份**写进临时目录——`sessions/` 是 `.gitignore` 掉的运行产物，新克隆的人
    那里根本没有这个目录，只读它的历史文件等于没测（这条兼容就得只在我这台机器上成立）。
    本机真有历史轨迹时顺带**每条读一遍**：以前只挑排在最前那条，被现实打过脸——`new_session()`
    一开工作区就先落一个只有头行的文件，那种文件恰好可能排最前。任何一条读不动都算回归。
    """
    (workdir / "legacy-0001.jsonl").write_text("\n".join(LEGACY_ROWS) + "\n", encoding="utf-8")
    loaded = JsonlSessionStore(workdir).load("legacy-0001")
    assert loaded.id == "legacy-0001"
    assert [type(m).__name__ for m in loaded.messages] == ["UserMessage", "AssistantMessage"]
    assert (loaded.goal, loaded.current_segment) == ("写个报告", "")     # 缺 segment 的旧行 = 未绑定

    files = sorted((BASE / "sessions").glob("*.jsonl"))
    if files:
        store = JsonlSessionStore(BASE / "sessions")
        for path in files:
            assert store.load(path.stem).id == path.stem
        filled = next((p for p in files if store.load(p.stem).messages), None)
        if filled is not None:
            assert store.load(filled.stem).goal


def test_store_is_a_real_file_dumper_for_a_whole_turn(workdir, make_service):
    call = ToolCall("c1", "shell", {"command": "Set-Content made.txt -Value ok"})
    service, _, _ = make_service(workdir, replies=[reply(call), reply(text="完成")])
    service.session_store = JsonlSessionStore(workdir)
    session = service.new_session()
    service.chat(session.id, "写文件")
    reloaded = JsonlSessionStore(workdir).load(session.id)
    assert [type(m).__name__ for m in reloaded.messages] == \
        ["UserMessage", "AssistantMessage", "ToolResult", "AssistantMessage"]
    assert reloaded.messages[2].review_ms is not None  # 审核耗时写进了文件


# === 执行环境：Windows + 钉死的解释器 =========================================


def test_interpreter_override_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("HARNESS_PYTHON", sys.executable)
    assert conda_python() == sys.executable
    monkeypatch.delenv("HARNESS_PYTHON")
    monkeypatch.setenv("HARNESS_CONDA_ENV", "no_such_env")
    assert conda_env() == "no_such_env"
    with pytest.raises(RuntimeError, match="no_such_env"):
        conda_python()


def test_clixml_is_unwrapped_into_readable_text_and_keeps_stdout():
    blob = ("目录里的文件\n#< CLIXML\n"
            '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<S S="Error">Get-ChildItem : _x000D__x000A_找不到路径 no_such_dir</S></Objs>')
    text = _strip_clixml(blob)
    assert text.startswith("目录里的文件") and "找不到路径 no_such_dir" in text
    assert "#< CLIXML" not in text and "_x000D_" not in text


def test_shell_errors_are_readable_text_and_keep_the_exit_code(workdir):
    observed = body(ShellTool(workdir).execute({"command": "Get-ChildItem no_such_dir_xyz"}))
    assert observed["returncode"] != 0 and "Cannot find path" in observed["output"]
    for junk in ("#< CLIXML", "<Objs", "_x00"):
        assert junk not in observed["output"]


def test_python_is_pinned_to_the_conda_interpreter(workdir):
    """python 工具与 shell 里的 `python` 必须落在同一个解释器上。"""
    reported = Path(body(PythonTool(workdir).execute(
        {"code": "import sys; print(sys.executable)"}))["output"].strip())
    assert reported.resolve().parent == Path(conda_python()).resolve().parent
    via_shell = body(ShellTool(workdir).execute({"command": 'python -c "import sys;print(sys.executable)"'}))
    assert Path(via_shell["output"].strip()).resolve().parent == Path(conda_python()).resolve().parent


def test_python_snippets_land_in_the_workspace_temp_folder(workdir):
    PythonTool(workdir).execute({"code": "print(1)"})
    scripts = sorted(p.name for p in (workdir / ".harness_tmp").glob("*.py"))
    assert scripts == ["run_1.py"]


def test_tools_are_loud_not_fatal(workdir):
    """坏命令、空参数、报错脚本都只能变成文本观察，不能把异常抛进循环。"""
    shell, python = ShellTool(workdir), PythonTool(workdir)
    assert body(shell.execute({"command": "exit 3"}))["returncode"] == 3
    assert body(shell.execute({"command": ""}))["returncode"] == -1
    assert body(shell.execute({"command": "type nope.txt"}))["returncode"] != 0
    assert body(python.execute({"code": "raise ValueError('x')"}))["returncode"] != 0
    assert body(python.execute({"code": ""}))["returncode"] == -1


def test_commands_run_inside_the_workspace(workdir):
    """路径监狱的一半：cwd 钉死在 workspace，模型不用写绝对路径也不会飘到别处。"""
    ShellTool(workdir).execute({"command": "Set-Content -Path made_by_model.txt -Value 'hi'"})
    assert (workdir / "made_by_model.txt").read_text(encoding="utf-8").strip() == "hi"


def test_long_output_is_truncated_for_the_context_wall(workdir, monkeypatch):
    """spark 的 32K 是硬墙（超了直接 HTTP 400），工具输出必须自己先收口。"""
    shell = ShellTool(workdir)
    monkeypatch.setattr(shell, "max_output_chars", 500)
    output = body(shell.execute({"command": "python -c \"print('x'*4000)\""}))["output"]
    assert len(output) < 900 and "chars omitted" in output and output.startswith("xxx")


def test_timeout_kills_the_whole_process_group(workdir, monkeypatch):
    shell = ShellTool(workdir)
    monkeypatch.setattr(shell, "timeout", 2.0)
    started = time.perf_counter()
    observed = body(shell.execute({"command": "Start-Sleep 30; echo never"}))
    elapsed = time.perf_counter() - started
    assert observed["returncode"] == -1 and "timeout after 2s" in observed["output"]
    # 守卫的判据不只是"返回 -1"，还包括"它没有陪着子进程把 30s 睡完"
    assert elapsed < 2.0 + shell.kill_grace + 5.0, elapsed


# === 前端适配：WebApi =========================================================


@pytest.fixture
def http():
    """真起一个服务，端口交给系统挑：WebApi 是适配器，值得像适配器那样测。"""
    service = AgentService(FakeClient(replies=[reply(text="你好")]), FakeStore(),
                           Assistant("i", ()), ToolCallReviewer("review"), SETTINGS, SETTINGS)
    server = WebApi(service, "127.0.0.1", 0).server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()


def call(base: str, method: str, path: str, raw: bytes | None = None):
    request = Request(base + path, data=raw, method=method)
    if raw is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except Exception as exc:  # 4xx 也走这里：坏请求本来就是被测行为
        status = getattr(exc, "code", None)
        if status is None:
            pytest.skip(f"本地端口不可用: {exc}")
        return status, exc.read().decode("utf-8")


def test_webui_page_and_session_list(http):
    assert "spark harness" in call(http, "GET", "/")[1]
    assert json.loads(call(http, "GET", "/sessions")[1]) == []


def test_webui_rejects_bad_requests_but_stays_alive(http):
    """实测踩出来的：PowerShell 的 curl -d 发中文 body 会变成非法 JSON。
    畸形 JSON / 缺 message / body 不是 object / 未知 session_id / 未知路径都不能把 handler 抛死。"""
    for method, path, raw in [("POST", "/chat", b"{not json"),
                              ("POST", "/chat", b"{}"),
                              ("POST", "/chat", b"[1,2]"),
                              ("POST", "/chat", b'{"session_id": "ghost", "message": "hi"}'),
                              ("GET", "/nope", None),
                              ("POST", "/nope", b"{}")]:
        assert call(http, method, path, raw)[0] in (400, 404), (method, path, raw)
    assert call(http, "GET", "/sessions")[0] == 200  # 服务还活着


def test_webui_chat_round_trip_returns_this_turns_messages(http):
    status, text = call(http, "POST", "/chat", json.dumps({"message": "你好"}, ensure_ascii=False).encode())
    assert status == 200
    produced = json.loads(text)["produced"]
    assert [m["role"] for m in produced] == ["user", "assistant"] and produced[-1]["content"] == "你好"


# === 审核请求不带思维链，token 用量留在轨迹里 =================================


def test_payload_asks_for_no_thinking_only_when_told_to():
    client = OpenAIClient("http://127.0.0.1:11434/v1", "")
    ctx = Context("指令", (), [UserMessage("目标")], AssistantMessage)
    assert "reasoning_effort" not in client.payload(ctx, ModelSettings("m", Effort.LOW))
    off = client.payload(ctx, ModelSettings("m", Effort.LOW, thinking=False))
    assert off["reasoning_effort"] == "none"  # 两端点实测都认这一个值；非法值回 400


def test_parse_keeps_the_usage_we_used_to_throw_away():
    answer = SimpleNamespace(content="好了", tool_calls=None)
    object_usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=40,
                                   prompt_tokens_details=SimpleNamespace(cached_tokens=1100))
    message = OpenAIClient.parse(answer, AssistantMessage, object_usage)
    assert (message.prompt_tokens, message.completion_tokens, message.cached_tokens) == (1200, 40, 1100)
    message = OpenAIClient.parse(answer, AssistantMessage, {"prompt_tokens": 9, "completion_tokens": 3})
    assert (message.prompt_tokens, message.completion_tokens, message.cached_tokens) == (9, 3, 0)
    assert OpenAIClient.parse(answer, AssistantMessage).prompt_tokens == 0  # 没有 usage 也不炸


def test_usage_and_segment_stay_out_of_the_request_body():
    rows = [message_row(AssistantMessage("好了", (), 1.0, 1200, 40, 1100)), message_row(UserMessage("目标"))]
    assert request_message(rows[0]) == {"role": "assistant", "content": "好了"}
    assert request_message(rows[1]) == {"role": "user", "content": "目标"}
    assert {"segment", "prompt_tokens", "completion_tokens", "cached_tokens"} <= set(STORE_ONLY_KEYS)


# === 工作区身份：一个目录一条轨迹，同一时刻只有一个写入者 =====================


def test_the_same_workspace_slugs_to_one_however_spelled(workdir):
    spellings = [workdir, Path(str(workdir).upper()), workdir.parent / "." / workdir.name,
                workdir / "unused" / ".."]
    slugs = {session_slug(p) for p in spellings}
    assert len(slugs) == 1
    slug = slugs.pop()
    assert slug.startswith(workdir.name) and len(slug.rsplit("-", 1)[1]) == 8


def test_new_session_opens_the_workspaces_existing_trajectory(workdir):
    """绑定了 session_id 且文件已存在，new_session 就是打开：一工作区一条轨迹。"""
    store = JsonlSessionStore(workdir)
    service = AgentService(FakeClient(replies=[reply(text="好")]), store, Assistant("i"),
                           ToolCallReviewer("r"), SETTINGS, SETTINGS, session_id="ws-abcd1234")
    opened = service.new_session()
    service.chat(opened.id, "第一件事")
    assert len(store.list()) == 1
    assert len(service.new_session().messages) == 2  # 同一条轨迹，接着写


def test_workspace_lock_keeps_a_second_writer_out(workdir):
    path = workdir / "runs" / "w.lock"
    with WorkspaceLock(path, workdir, "第一个") as held:
        assert held.held and path.exists()
        with pytest.raises(WorkspaceLocked) as busy:
            WorkspaceLock(path, workdir, "第二个").acquire()
        assert busy.value.holder["pid"] == os.getpid()
    assert not path.exists()  # 正常退出一定清干净


def test_a_lock_left_by_a_missing_pid_is_reclaimed(workdir):
    path = workdir / "w.lock"
    path.write_text(json.dumps({"pid": 987654, "workspace": "上一个进程"}), encoding="utf-8")
    lock = WorkspaceLock(path, workdir, "我").acquire()
    assert lock.held
    lock.release()
    assert not path.exists()


def test_an_exited_child_is_not_mistaken_for_a_holder():
    """`OpenProcess` 成功只证明"进程对象还在"，不证明"它在跑"。

    父进程不 `wait()` 的子进程正是这个形状：`poll()` 已经拿到退出码，句柄还吊着进程对象。
    早先用"能不能开句柄"判活就会把这种陈旧锁说成"还在运行"，把后来的人永久挡在门外。
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = child.pid
    while child.poll() is None:
        time.sleep(0.05)
    assert process_state(pid) in ("exited", "gone")
    assert not pid_alive(pid)


def test_an_unreadable_lock_is_not_stolen(workdir):
    """内容读不出来就不知道有没有人活着，宁可拒绝也不抢——抢错的代价是轨迹被覆盖。"""
    path = workdir / "w.lock"
    path.write_text("写到一半", encoding="utf-8")
    with pytest.raises(WorkspaceLocked) as busy:
        WorkspaceLock(path, workdir, "我").acquire()
    assert busy.value.state == "unknown"


def test_an_old_unreadable_lock_is_reclaimed(workdir):
    path = workdir / "w.lock"
    path.write_text("写到一半", encoding="utf-8")
    old = time.time() - 3700
    os.utime(path, (old, old))
    assert WorkspaceLock(path, workdir, "我").acquire().held


# === 真 spark（可选，端点不通就 skip）========================================


@pytest.mark.live
def test_real_spark_answers_the_reviewer_contract():
    """无害命令要返回可解析的 Decision，顺带验 response_format 通路。

    默认跳过：它依赖"本机 Ollama 正好可用且不忙"，而 Ollama 半死（端口在听、请求超时）时
    这一条会把整套用例拖上三分钟。要真连线就显式开：`$env:HARNESS_LIVE=1`。
    """
    if os.getenv("HARNESS_LIVE", "").strip().lower() not in {"1", "true", "yes"}:
        pytest.skip("需要真 spark：设 HARNESS_LIVE=1（默认跳过，避免把套件拖在 Ollama 上）")
    url = os.getenv("HARNESS_REVIEWER_URL", "http://127.0.0.1:11434/v1")
    parsed = urlparse(url)
    # 先探端口：Ollama 没起时，让它 0.5s 内 skip，而不是把整套用例拖在连接超时上
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 11434), timeout=0.5):
            pass
    except OSError as exc:
        pytest.skip(f"Ollama 没在跑（{url}）：{exc}")
    client = OpenAIClient(url, "", timeout=60)
    try:
        models = [m.id for m in client.client.models.list().data]
    except Exception as exc:
        pytest.skip(f"Ollama 不通: {exc}")
    assert models, "Ollama 上没有模型"
    session = Session(id="live", messages=[UserMessage("在工作目录写一个文件")])
    context = Context("只输出一行 JSON。", (), [session.messages[0]], Decision)
    try:  # 单实例串行：上一条请求还在生成时，这里排队超时不算逻辑失败
        decision = client.generate(context, ModelSettings(models[0], Effort.LOW))
    except Exception as exc:
        pytest.skip(f"reviewer 端点忙: {type(exc).__name__}")
    assert isinstance(decision, Decision)
