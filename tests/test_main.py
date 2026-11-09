"""组装根与它的两块素材：端点表、指令文案、体检表。

`main.py` 是全系统唯一知道所有具体类的地方，所以这里测的是"接线有没有接错"：
环境变量在调用时读、`build()` 交出去的是不是那些具体类、体检表能不能在没有网络时如实报错。
指令文案放这儿一起测：它是组装素材，`domain/` 和 `app/` 都不该认识它。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from conftest import BASE

import main
import prompts
from adapters.cli import Ports
from adapters.doctor import Rig, run_checks
from adapters.models.no_review import NoReviewClient
from adapters.models.openai_client import OpenAIClient
from adapters.stores.jsonl import JsonlSessionStore, session_slug
from app.policy import hard_deny
from app.ports import Effort
from app.service import AgentService
from domain.roles import Assistant, ToolCallReviewer
from domain.session import Session, ToolCall, UserMessage

LOCAL = "http://127.0.0.1:1/v1"  # 打不开的端点：让需要网络的检查项老实失败


# === 端点表：配置在调用时读 ==================================================


def test_endpoint_falls_back_to_the_table_and_prefers_the_environment(monkeypatch):
    monkeypatch.delenv("HARNESS_ASSISTANT_URL", raising=False)
    monkeypatch.delenv("HARNESS_ASSISTANT_MODEL", raising=False)
    assert main.endpoint("assistant")["url"] == main.ENDPOINTS["assistant"]["url"]
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    monkeypatch.setenv("HARNESS_ASSISTANT_MODEL", "some/other")
    a = main.endpoint("assistant")
    assert a["url"] == LOCAL and a["model_id"] == "some/other"
    assert a["effort"] is main.ENDPOINTS["assistant"]["effort"]  # 覆盖只动 url/model，档位跟着表走


def test_key_of_reads_the_named_env_var_and_never_raises_for_a_local_endpoint(monkeypatch):
    monkeypatch.setenv("HARNESS_REVIEWER_URL", LOCAL)
    assert main.key_of(main.endpoint("reviewer")) == ""  # key_env 为空的本地端点
    monkeypatch.setenv("DASHSCOPE_API_KEY", "  sk-test-abc ")
    assert main.key_of(main.endpoint("assistant")) == "sk-test-abc"


def test_the_reviewer_is_local_and_the_assistant_is_online():
    """两张表的分工是这台的实情：reviewer 走 Ollama（免费、串行、11 秒一次），assistant 走公网。"""
    assert main.ENDPOINTS["reviewer"]["url"].startswith("http://127.0.0.1")
    assert main.ENDPOINTS["reviewer"]["key_env"] == ""
    assert main.ENDPOINTS["assistant"]["key_env"] == "DASHSCOPE_API_KEY"


# === 组装：build 交出去的是哪些具体类 ========================================


def test_build_wires_the_real_adapters(monkeypatch, workdir):
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)  # 免 key 的本地端点：装配过程不碰网络
    service = main.build(workdir, steps=4)
    assert isinstance(service, AgentService)
    assert isinstance(service.model_client, OpenAIClient) and isinstance(service.reviewer_client, OpenAIClient)
    assert isinstance(service.session_store, JsonlSessionStore)
    assert isinstance(service.assistant, Assistant) and isinstance(service.reviewer, ToolCallReviewer)
    assert [t.name for t in service.assistant.tools] == ["shell", "python"]  # 本机是 Windows
    assert service.max_iterations == 4
    assert getattr(service.guard, "func", service.guard) is hard_deny  # 组装根把 workspace 绑进了 partial
    assert service.session_id == session_slug(workdir)  # 一工作区一条轨迹
    assert service.assistant_model_settings.model_id == main.endpoint("assistant")["model_id"]
    assert service.reviewer_model_settings.effort is main.ENDPOINTS["reviewer"]["effort"]


def test_build_tells_the_model_where_it_is_and_which_interpreter_is_pinned(monkeypatch, workdir):
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    instruction = main.build(workdir).assistant.instruction
    assert str(workdir) in instruction
    assert re.search(r"C:\\conda\\envs\\\w+\\python\.exe|python\.exe", instruction)


def test_build_refuses_to_run_an_online_assistant_without_a_key(monkeypatch):
    monkeypatch.delenv("HARNESS_ASSISTANT_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(main, "ENDPOINTS", dict(main.ENDPOINTS,
                                                assistant=dict(main.ENDPOINTS["assistant"], key_env="ABSENT_KEY")))
    with pytest.raises(SystemExit, match="ABSENT_KEY"):
        main.build(BASE / "quests" / "default")


def test_rig_carries_the_composition_knowledge_the_doctor_needs(workdir):
    rig = main.rig(workdir)
    assert isinstance(rig, Rig) and rig.base == BASE and rig.workspace == workdir
    assert rig.endpoint is main.endpoint and rig.key_for is main.key_of
    assert str(workdir) in rig.reviewer_instruction


# === 审核档位开关：--review / --no-guard =====================================


def _reviewer_verdict(service, command: str):
    session = Session(messages=[UserMessage("在工作目录写文件")])
    call = ToolCall("c1", "shell", {"command": command})
    return service.reviewer_client.generate(service.reviewer.context(session, call),
                                            service.reviewer_model_settings)


def test_off_mode_swaps_in_a_no_network_reviewer(monkeypatch, workdir):
    """`--review off`：不发请求、直接放行，但硬规则那一道还在。"""
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    service = main.build(workdir, steps=2, review="off")
    assert isinstance(service.reviewer_client, NoReviewClient)
    decision = _reviewer_verdict(service, "Get-Date")
    assert decision.should_allow and "审核已关闭" in decision.reason
    assert service.guard(ToolCall("c", "shell", {"command": "rm -rf C:\\"})) is not None


def test_online_mode_points_the_reviewer_at_the_assistant_endpoint(monkeypatch, workdir):
    """`--review online`：同一把 key 与端点审，模型默认与 assistant 相同，温度压到 LOW 求确定性。"""
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    service = main.build(workdir, steps=2, review="online")
    assert isinstance(service.reviewer_client, OpenAIClient)
    assert service.reviewer_client.base_url == LOCAL
    assert service.reviewer_model_settings.model_id == main.endpoint("assistant")["model_id"]
    assert service.reviewer_model_settings.effort is Effort.LOW


def test_a_different_reviewer_model_can_be_pinned_on_the_online_path(monkeypatch, workdir):
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    service = main.build(workdir, steps=2, review="online", reviewer_model="other/checker")
    assert service.reviewer_model_settings.model_id == "other/checker"


def test_unknown_review_mode_fails_loudly(monkeypatch, workdir):
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    with pytest.raises(SystemExit, match="--review"):
        main.build(workdir, review="maybe")


def test_no_guard_disables_the_regex_layer_too(monkeypatch, workdir, capsys):
    """`--no-guard` 是"完全无审核"的对照臂：危险形状也不再拦，必须打警告。"""
    monkeypatch.setenv("HARNESS_ASSISTANT_URL", LOCAL)
    service = main.build(workdir, steps=2, review="off", guard=False)
    assert service.guard(ToolCall("c", "shell", {"command": "rm -rf C:\\"})) is None
    assert "也关了" in capsys.readouterr().err


def test_the_cli_app_is_injected_rather_than_imported():
    """依赖方向是 main → cli：CLI 不 import 组装根，否则 main → cli → doctor → main 就成环。"""
    assert isinstance(main.app, typer.Typer)  # build_app 已经在模块加载时跑过
    for path in list((BASE / "domain").glob("*.py")) + list((BASE / "app").glob("*.py")) + \
            list((BASE / "adapters").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+main\b", source, re.M), f"{path.name} 反过来 import 了组装根"


def test_sessions_says_so_when_there_is_nothing_yet(workdir):
    """空 `sessions/` 必须出声：TUI 的空屏就写着「全部轨迹：python main.py sessions」，
    而新克隆的仓库里那个目录是空的——一声不响看着就像命令坏了。"""
    from typer.testing import CliRunner

    import adapters.cli as cli

    app = cli.build_app(cli.Ports(make_service=lambda *_a, **_k: None, make_rig=lambda: None,
                                   make_store=lambda: JsonlSessionStore(workdir), base=workdir))
    flat = "".join(CliRunner().invoke(app, ["sessions"]).output.split())
    assert "还没有轨迹" in flat and "main.pyrun" in flat      # Rich 会折行，比对前把空白全去掉
    assert "一工作区一条" in flat                              # 顺带把文件名的规矩说清


def test_cli_exposes_the_six_front_end_commands():
    assert sorted(c.callback.__name__ for c in main.app.registered_commands) == \
        ["doctor", "run", "serve", "sessions", "show", "tui"]


def test_ports_dataclass_is_what_main_injects():
    assert [f for f in Ports.__dataclass_fields__] == ["make_service", "make_store", "make_rig", "base"]


# === 指令文案：渲染是确定的、漏填是致命的 ====================================


def test_instructions_render_the_environment_facts():
    text = prompts.assistant_instruction(r"E:\w", r"C:\conda\envs\llm_learn\python.exe")
    assert r"E:\w" in text and "python.exe" in text
    assert "Get-ChildItem" not in text  # 指令只讲环境，不教具体命令
    reviewer = prompts.reviewer_instruction(r"E:\w")
    assert r"E:\w" in reviewer and '"allow"' in reviewer


def test_rendering_is_byte_stable_so_the_local_prefix_cache_survives():
    """同一会话内逐字节相同是硬约束：改一个字，spark 前缀缓存从 0.08s 变 6.3s。"""
    once = prompts.assistant_instruction(r"E:\w", "C:\\py\\python.exe")
    assert once == prompts.assistant_instruction(r"E:\w", "C:\\py\\python.exe")
    assert prompts.reviewer_instruction(r"E:\w") == prompts.reviewer_instruction(r"E:\w")


def test_a_missing_environment_fact_fails_at_startup_not_in_the_prompt():
    """Template.substitute 漏填即 KeyError：宁可启动崩，也不发一个带 $python 字面量的半截提示词。"""
    from string import Template
    with pytest.raises(KeyError):
        Template(prompts.ASSISTANT.template).substitute(workspace="w")
    with pytest.raises(KeyError):
        prompts.REVIEWER.substitute()


def test_prompts_never_carry_secrets_or_powerful_templates():
    """话术里不许有 key，也不许有 jinja2 那种能执行逻辑的模板语法（现在只是 $ 替换）。"""
    text = prompts.assistant_instruction(r"E:\w", "py") + prompts.reviewer_instruction(r"E:\w")
    assert "sk-" not in text and "{" in text  # reviewer 的 JSON 示例是纯文本，Template 不解析花括号
    assert "{%" not in text and "{{" not in text


# === 体检：没有网络时也要如实报告 ============================================


def test_doctor_reports_ten_independent_checks(workdir, monkeypatch):
    monkeypatch.setattr(main, "ENDPOINTS", dict(main.ENDPOINTS,
                                                reviewer=dict(main.ENDPOINTS["reviewer"], url=LOCAL),
                                                assistant=dict(main.ENDPOINTS["assistant"], url=LOCAL)))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    report = run_checks(Rig(base=BASE, workspace=workdir, endpoint=main.endpoint, key_for=main.key_of,
                            reviewer_instruction=prompts.reviewer_instruction(str(workdir))))
    assert [row["name"] for row in report] == ["python", "powershell", "git", "python_pinned",
                                               "tool_execution", "hard_deny", "console_host",
                                               "reviewer_ollama", "reviewer_verdict", "assistant_online"]
    local = {row["name"]: row["ok"] for row in report if row["name"] not in
             ("reviewer_ollama", "reviewer_verdict", "assistant_online")}
    assert all(local.values()), local  # 断网也能验完这七项：体检的意义就是分清哪一环断了
    host = {row["name"]: row["detail"] for row in report if row["name"] == "console_host"}["console_host"]
    assert "鼠标" in host and ("conhost" in host or "Terminal" in host)   # 宿主结论必须把鼠标这件事说清
    broken = {row["name"]: row for row in report if not row["ok"]}
    assert {"reviewer_ollama", "reviewer_verdict", "assistant_online"} <= set(broken)
    assert "未设置" in broken["assistant_online"]["detail"]


# === 入口脚本：cmd 的坑用断言钉住 ============================================


def test_entry_bat_is_ascii_only_crlf_and_forwards_args():
    """`harness.bat` 由 cmd 解释，三件事能静默毁掉它，症状都像"双击就闪退/没反应"：

    1. 非 ASCII 字节——cmd 按 OEM/GBK 解 UTF-8 中文，注释直接变成命令（`'ui' is not recognized`）；
    2. 裸 LF 行尾——cmd 按字节偏移回读脚本，`chcp` 之后偏移在新旧代码页间错位，于是从一行的中间
       开始执行（实测见过 `'the'` / `'nts'` / `'ast.log'` is not recognized 这种半截词）；
    3. **wt 的参数写错**。本机 wt 1.24.12741.0 实测：`-N`、`-n`、`--new-window` 一律被拒——wt 起来、
       解析失败、自己退出，`cmd /c` 那半截根本没跑，双击看过去就是"什么都没发生"；能用的是 `-w new`
       （`-w -1` 也可以）。这条是上一轮我自己埋的坑，所以钉成断言，别再按记忆写选项。

    起真窗口那条分支沙箱里其实验得了（WT 起得来，只是那扇窗没人看见），但界面在里面跑成什么样只能靠
    `--child` 分支手验参数：`main.py tui -w quests\\t05`。
    """
    raw = (BASE / "harness.bat").read_bytes()
    assert all(byte < 128 for byte in raw), "入口脚本混进了非 ASCII 字节"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "入口脚本必须整份 CRLF"
    text = raw.decode("ascii")
    assert "setlocal" in text and "endlocal" in text
    assert "wt.exe -w new" in text and "wt.exe -N" not in text        # 实测过的选项（见上面第 3 条）
    assert ":conhost" in text                                          # 找不到 wt 就回落
    assert 'call "%HARNESS_PY%" main.py tui %2' in text            # 必须 call，否则子窗口跑完吞掉父脚本
    assert "--child %*" in text                                    # 父层把用户给的参数原样转发


# === `tui` 那个循环：界面只置标志，重建 service 是 cli 的事 ==================


def run_tui(monkeypatch, workdir: Path, behave) -> tuple[list, list]:
    """用假 HarnessApp 跑 `main.py tui` 的循环：`behave(rounds, app)` 决定这一屏怎么退。

    这条路平时没人验：它不碰网络、也不渲染，就是"退出之后要不要再来一次、带哪些值再来"。
    """
    from typer.testing import CliRunner

    import adapters.cli as cli
    import adapters.tui as tui

    rounds: list = []
    built: list = []

    class Fake:
        def __init__(self, service, **kwargs) -> None:
            self.service, self.kwargs = service, kwargs
            self.review_mode, self.built_mode = "online", "local"      # 界面记的与真挂的不一致
            self.total_steps, self.steps, self.segment = 4, 2, "6a1f0001"
            self.session_id, self.restart_request = "s-1", False
            self.guard_label = lambda: "闸门 关 · 模型说什么就干什么"
            rounds.append(self)
            behave(rounds, self)

        def run(self) -> None:
            pass

    def make_service(workspace, steps, *_rest, **_kw):
        built.append((workspace, steps))
        return SimpleNamespace(max_iterations=steps, session_id="s-1")

    monkeypatch.setattr(tui, "HarnessApp", Fake)                   # cli 在函数里 import，换模块属性就行
    app = cli.build_app(cli.Ports(make_service=make_service, make_store=lambda: None,
                                   make_rig=lambda: None, base=workdir))
    result = CliRunner().invoke(app, ["tui", "-w", str(workdir), "-s", "12"])
    assert result.exit_code == 0, result.output
    return rounds, built


def test_tui_loop_restarts_when_the_ui_asks_and_logs_the_live_values(monkeypatch, workdir):
    """「重启界面（应用档位 X）」的落地路径：界面只置 `restart_request`，cli 重建 service 再开一屏。"""
    rounds, built = run_tui(monkeypatch, workdir,
                            lambda rs, app: setattr(app, "restart_request", len(rs) == 1))
    assert len(rounds) == 2 and len(built) == 2                    # 第一次要求重启，第二次正常退出
    assert built[0][1] == 12
    log = (workdir / "runs" / "tui-last.log").read_text(encoding="utf-8")
    assert "正常退出" in log                                        # 摘要只留最后一次
    assert "闸门 关 · 模型说什么就干什么" in log                      # 读的是界面上的当前值，不是启动参数
    assert "步数上限 12" in log and "对话段 6a1f0001" in log


def test_tui_loop_rebuilds_with_the_chosen_workspace(monkeypatch, workdir):
    """换工作区 = 退出 + 用新目录重建：工作目录绑着闸门与轨迹名，热切会骗人，所以宁可重启。"""
    target = workdir / "另一个工作区"

    def behave(rounds, app):
        if len(rounds) == 1:
            app.kwargs["on_exit"](target)                           # 界面里选定新目录后退出

    rounds, built = run_tui(monkeypatch, workdir, behave)
    assert len(rounds) == 2 and built[1][0] == target              # 第二屏的服务建在新目录上
    assert str(target) in (workdir / "runs" / "tui-last.log").read_text(encoding="utf-8")
