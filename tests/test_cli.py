#!/usr/bin/env python3
"""Offline regression tests for CLI/core behavior changed during conversion."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deepseek.auth import Session
from deepseek import net
from deepseek.client import (
    COMPLETION_PATH,
    DeepSeekClient,
    Reply,
    _Stream,
    _extract_tool_call_json,
    _parse_sse,
)
from deepseek.response import (
    DONE_TOKEN,
    MAX_CONTINUATIONS_PER_TURN,
    PatchStreamFilter,
    ResponseProcessor,
    TurnNudger,
    execute_tool_calls,
    extract_tool_calls,
    find_freeform_patches,
    has_valid_done,
    is_complete_response,
    strip_done,
    strip_freeform_patches,
)
from deepseek.agent_tools import ToolExecutor, _parse_patch, apply_patch, exec_command
from deepseek.net import WatchConfig
from deepseek.tools import Tool, ToolCall, execute_tool
from deepseek_cli import (
    CONTINUE_PROMPT,
    DeepSeekCLI,
    FIRST_CONTINUE_PROMPT,
    WATCH_NO_SESSION_ERROR,
    WorkingIndicator,
    build_tool_registry,
    build_parser,
    codex_default_tools,
    read_file,
)


def test_unknown_tool_returns_error() -> None:
    result = execute_tool(ToolCall("nope", {}, "{}"), [read_file])
    assert result.startswith("Error: tool 'nope' is not registered")
    print("  PASS: B1 unknown tool returns error string")


def test_parse_sse_collects_multiple_tool_calls() -> None:
    payloads = [
        'data: {"p":"response/fragments","o":"APPEND","v":[{"id":1,"type":"RESPONSE",'
        '"content":"<tool_call>{\\"name\\":\\"a\\",\\"arguments\\":{}}</tool_call>'
        '<tool_call>{\\"name\\":\\"b\\",\\"arguments\\":{\\"x\\":1}}</tool_call>"}]}',
        'data: {"p":"response/fragments/-1/content","v":" done"}',
    ]
    parts = list(_parse_sse(payloads))
    calls = [json.loads(text) for kind, text in parts if kind == "tool_call"]
    assert calls == [
        {"name": "a", "arguments": {}},
        {"name": "b", "arguments": {"x": 1}},
    ]
    assert ("answer", " done") in parts
    print("  PASS: B4 parser emits every tool-call block")


def test_parse_sse_native_dsml_tool_call() -> None:
    bar = "\uff5c\uff5c"
    payload = (
        f"<{bar}DSML{bar} calls>\n"
        f'<{bar}DSML{bar} invoke name="update_plan">\n'
        f'<{bar}DSML{bar} parameter name="plan" string="false">'
        '[{"step": "Inspect relevant files", "status": "in_progress"}, '
        '{"step": "Make the code change", "status": "pending"}, '
        '{"step": "Run tests", "status": "pending"}]'
        f'</{bar}DSML{bar} parameter>\n'
        f'</{bar}DSML{bar} invoke>\n'
        f'</{bar}DSML{bar} calls>FINISHED'
    )
    lines = [
        "data: " + json.dumps(
            {
                "p": "response/fragments",
                "o": "APPEND",
                "v": [{"id": 1, "type": "RESPONSE", "content": payload}],
            },
            ensure_ascii=False,
        )
    ]
    parts = list(_parse_sse(lines))
    calls = [json.loads(text) for kind, text in parts if kind == "tool_call"]
    assert calls == [{
        "name": "update_plan",
        "arguments": {
            "plan": [
                {"step": "Inspect relevant files", "status": "in_progress"},
                {"step": "Make the code change", "status": "pending"},
                {"step": "Run tests", "status": "pending"},
            ]
        },
    }]
    assert not any("DSML" in text or "FINISHED" in text for kind, text in parts if kind == "answer")
    print("  PASS: native DSML tool call parses to ToolCall JSON")


def test_reply_tool_calls_list() -> None:
    a = ToolCall("a", {}, "{}")
    b = ToolCall("b", {}, "{}")
    reply = Reply("", "cid", tool_calls=[a, b])
    assert reply.tool_calls == [a, b]
    assert reply.tool_call is a
    legacy = Reply("", "cid", tool_call=a)
    assert legacy.tool_calls == [a]
    print("  PASS: B4 Reply.tool_calls and compatibility alias")


def test_stream_with_tools_executes_all_calls() -> None:
    calls: list[tuple[str, dict]] = []

    def make(name: str):
        def fn(**kwargs) -> str:
            calls.append((name, kwargs))
            return f"{name} ok"
        return Tool(name=name, description=name, parameters={"type": "object"}, fn=fn)

    a = ToolCall("a", {}, '{"name":"a","arguments":{}}')
    b = ToolCall("b", {"x": 1}, '{"name":"b","arguments":{"x":1}}')

    class FakeStream:
        def __init__(self, tool_calls=None, answer=""):
            self.tool_calls = tool_calls or []
            self.answer = answer
            self.conversation_id = "sid:1"

        def iter_parts(self):
            for call in self.tool_calls:
                yield ("tool_call", call.raw)
            if self.answer:
                yield ("answer", self.answer)

    class FakeClient(DeepSeekClient):
        def __init__(self):
            self._tool_preamble_fingerprints = {}
            self._last_stream_cid = None
            self.prompts: list[str] = []

        def stream(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return FakeStream(tool_calls=[a, b])
            return FakeStream(answer="done")

    client = FakeClient()
    events = list(client.stream_with_tools("hi", [make("a"), make("b")], approval="auto"))
    assert [kind for kind, _ in events].count("tool_call") == 2
    assert [kind for kind, _ in events].count("tool_result") == 2
    assert calls == [("a", {}), ("b", {"x": 1})]
    assert events[-1] == ("answer", "done")
    assert "Tool schemas (arguments):" in client.prompts[0]
    print("  PASS: stream_with_tools executes every requested tool")


def test_tool_preamble_cached_per_session() -> None:
    session = Session(
        token="t",
        cookies={"c": "v"},
        user_agent="ua",
        captured_at=time.time(),
    )
    client = DeepSeekClient(session=session)
    schema = json.dumps([read_file.schema()], indent=2)
    first = client._tool_prompt("hello", schema, None)
    assert "Tool schemas (arguments):" in first and first.endswith("User: hello")
    client._remember_tool_prompt(schema, "sid:1")
    second = client._tool_prompt("again", schema, "sid:2")
    assert second == "again"
    changed = client._tool_prompt("again", schema + " ", "sid:2")
    assert "Tool schemas (arguments):" in changed
    client.close()
    print("  PASS: B5 unchanged tool preamble is skipped for the same session")


def test_json_tool_result_event_shape() -> None:
    parser = build_parser()
    args = parser.parse_args(["--json", "--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app.emit_event("tool_result", "22C, clear", name="get_weather")
    event = json.loads(buf.getvalue())
    assert event == {
        "kind": "tool_result",
        "name": "get_weather",
        "result": "22C, clear",
    }
    print("  PASS: JSON tool_result event includes name and result")


def test_extension_command_contexts() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    rendered: list[str] = []
    app._render_system = rendered.append  # type: ignore[method-assign]
    app.extension_commands = {
        "/emit": lambda emit: emit("emit works"),
        "/app": lambda app_ctx: app_ctx.emit("app works"),
        "/old": lambda: print("old works"),
    }
    app.run_extension_command("/emit", "")
    app.run_extension_command("/app", "")
    app.run_extension_command("/old", "")
    assert "emit works" in rendered
    assert "app works" in rendered
    assert "old works" in rendered
    print("  PASS: B2 extension commands receive emit/app or keep legacy print")


def test_toolbar_reads_live_state() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    before = app.toolbar_text()
    app.model = "deepseek-expert"
    app.thinking = True
    after = app.toolbar_text()
    assert before != after
    assert "deepseek-expert" in after and "think on" in after
    print("  PASS: toolbar reads live state")


def test_streaming_code_fence_is_consumed() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")

    class Capture:
        def __init__(self):
            self.items = []
        def print(self, *args, **kwargs):
            self.items.append(args[0] if args else "")

    app.out = Capture()  # type: ignore[assignment]
    app._emit_markdown_line("```python")
    app._emit_markdown_line("x = 1")
    assert len(app.out.items) == 1
    app._emit_markdown_line("```")
    assert len(app.out.items) == 3
    assert app._in_code_fence is False
    print("  PASS: streaming code fences are consumed")


def test_dsml_suffixed_json_tool_call() -> None:
    raw = '{"name":"exec_command","arguments":{"cmd":"ls -la"}}</' + "｜｜DSML｜｜ parameter>"
    data = _extract_tool_call_json(raw)
    assert data["name"] == "exec_command"
    assert data["arguments"]["cmd"] == "ls -la"
    print("  PASS: DSML-suffixed JSON tool call is recovered")


def test_indicator_double_stop_and_agent_default() -> None:
    indicator = WorkingIndicator()
    indicator.start()
    indicator.stop()
    indicator.stop()
    assert indicator._stopped is True
    parser = build_parser()
    args = parser.parse_args(["--tools", "auto", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    assert app.model == "deepseek-expert"
    print("  PASS: indicator stop is idempotent and auto mode defaults to expert")


def test_broken_pipe_exit_and_json_help() -> None:
    cli = str(ROOT / "deepseek_cli.py")
    r = subprocess.run(
        [sys.executable, cli, "--json", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert r.returncode == 0
    event = json.loads(r.stdout)
    assert event["kind"] == "help" and "usage:" in event["text"]

    r2 = subprocess.run(
        ["bash", "-o", "pipefail", "-c", f"{sys.executable} {cli} --json --help | head -0"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert r2.returncode == 141, (r2.returncode, r2.stderr)
    assert "Traceback" not in r2.stderr
    print("  PASS: JSON help emits JSON and broken pipes exit 141 cleanly")


# ----- Strategy.md regression suite -----------------------------------------


def test_response_search_excludes_thinking() -> None:
    think_call = '{"name":"evil_think_call","arguments":{"x":1}}'
    response_call = '{"name":"good_response_call","arguments":{"y":2}}'
    lines = [
        "data: " + json.dumps({
            "p": "response/fragments",
            "o": "APPEND",
            "v": [
                {"id": 1, "type": "THINK",
                 "content": f"planning <tool_call>{think_call}</tool_call>\n\n{DONE_TOKEN}\n"},
                {"id": 2, "type": "RESPONSE",
                 "content": f"Working.\n<tool_call>{response_call}</tool_call>"},
            ],
        }),
    ]
    processor = ResponseProcessor().feed_all(_parse_sse(lines))
    assert "evil_think_call" in processor.thinking
    assert processor.done is False, "<<DONE>> inside THINK must not be searched"
    assert len(processor.tool_calls) == 1
    assert "good_response_call" in processor.tool_calls[0]
    assert processor.signal == "tool_call"
    print("  PASS: only RESPONSE fragments are searched for markers")


def test_tool_call_takes_priority_over_done() -> None:
    call = '<tool_call>{"name":"a","arguments":{}}</tool_call>'
    raw = f"Running it now.\n\n{call}\n\n{DONE_TOKEN}\n"
    processor = ResponseProcessor().feed_all([("answer", raw)])
    assert processor.done is True
    assert processor.signal == "tool_call"
    print("  PASS: a tool call wins over a valid completion marker")


def test_done_alone_ends_turn() -> None:
    raw = f"All three commands ran.\n\n{DONE_TOKEN}\n"
    processor = ResponseProcessor().feed_all([("answer", raw)])
    assert processor.signal == "done"
    nudger = TurnNudger(MAX_CONTINUATIONS_PER_TURN)
    assert nudger.note(processor.signal) == "complete"
    assert nudger.total_nudges == 0 and nudger.consecutive_failures == 0
    print("  PASS: a valid <<DONE>> alone ends the turn without a nudge")


def test_done_stripped_from_output() -> None:
    raw = f"Report:\n\n{DONE_TOKEN}\n"
    processor = ResponseProcessor().feed_all([("answer", raw)])
    assert processor.visible_answer() == "Report:"

    parser = build_parser()
    args = parser.parse_args(["--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app._handle_part("answer", "Report:\n\n<<DO")
        app._handle_part("answer", "NE>>\n")
        app._finish_turn("hi", None)
    assert DONE_TOKEN not in "".join(app._answer_parts)
    assert DONE_TOKEN not in buf.getvalue()
    print("  PASS: <<DONE>> never reaches user-visible output")


def test_done_malformed_not_complete() -> None:
    cases = [
        f"Here is the report. {DONE_TOKEN}\n",
        f"{DONE_TOKEN}\n\n{DONE_TOKEN}\n",
        f"Report.\n{DONE_TOKEN}\n",
    ]
    for raw in cases:
        assert not has_valid_done(raw), raw
        processor = ResponseProcessor().feed_all([("answer", raw)])
        assert processor.signal is None, raw
    print("  PASS: malformed <<DONE>> is not treated as complete")


def _tool_set(names: list[str]) -> tuple[list[Tool], list[str]]:
    executed: list[str] = []

    def make(name: str) -> Tool:
        def fn(**kwargs) -> str:
            executed.append(name)
            return f"{name} result"
        return Tool(name=name, description=name, parameters={"type": "object"}, fn=fn)

    return [make(name) for name in names], executed


def test_multiple_tool_calls_all_execute() -> None:
    tools, executed = _tool_set(["alpha", "beta", "gamma"])
    calls = [
        ToolCall("alpha", {}, '{"name":"alpha","arguments":{}}'),
        ToolCall("beta", {"x": 1}, '{"name":"beta","arguments":{"x":1}}'),
        ToolCall("gamma", {}, '{"name":"gamma","arguments":{}}'),
    ]
    results = execute_tool_calls(ToolExecutor(approval="auto"), calls, tools)
    assert executed == ["alpha", "beta", "gamma"]
    assert len(results) == 3
    assert [call.name for call, _ in results] == ["alpha", "beta", "gamma"]
    assert [result for _, result in results] == [
        "alpha result", "beta result", "gamma result",
    ]
    print("  PASS: all three tool calls execute in order")


def test_multiple_tool_calls_ordering_and_failure_continues() -> None:
    tools, executed = _tool_set(["alpha", "beta", "gamma"])
    calls = [
        ToolCall("alpha", {}, '{"name":"alpha","arguments":{}}'),
        ToolCall("missing", {}, '{"name":"missing","arguments":{}}'),
        ToolCall("gamma", {}, '{"name":"gamma","arguments":{}}'),
    ]
    results = execute_tool_calls(ToolExecutor(approval="auto"), calls, tools)
    assert [call.name for call, _ in results] == ["alpha", "missing", "gamma"]
    assert results[1][1].startswith("Error: tool 'missing' is not registered")
    assert executed == ["alpha", "gamma"]          # a failure does not stop the rest
    print("  PASS: every call runs, in order, and a failure does not stop the rest")


def test_continue_prompt_text() -> None:
    assert CONTINUE_PROMPT == (
        "Are you done? If so, write <<DONE>> on its own line "
        "(with a blank line before it). If not, emit a tool call to "
        "continue the task. Do not write prose without one of these."
    )
    assert "<<DONE>>" in CONTINUE_PROMPT
    assert "emit a tool call" in CONTINUE_PROMPT
    for banned in ("~/", "Downloads", "hf_results", "ls ", "/home/", "Hugginface"):
        assert banned not in CONTINUE_PROMPT, banned
    assert "You stopped" not in CONTINUE_PROMPT
    print("  PASS: continuation offers <<DONE>> or a tool call, no correction")


def test_nudge_counter_resets_on_tool_success() -> None:
    nudger = TurnNudger(MAX_CONTINUATIONS_PER_TURN)
    trace = [nudger.consecutive_failures]
    for signal in (None, "tool_call", None):
        nudger.note(signal)
        trace.append(nudger.consecutive_failures)
    assert trace == [0, 1, 0, 1], trace
    assert nudger.total_nudges == 2
    print("  PASS: nudge counter resets to 0 after a successful tool call")


def test_nudge_counter_dies_after_five_consecutive(tmp_path) -> None:
    nudger = TurnNudger(MAX_CONTINUATIONS_PER_TURN)
    decisions = [nudger.note(None) for _ in range(MAX_CONTINUATIONS_PER_TURN)]
    assert decisions == ["continue"] * MAX_CONTINUATIONS_PER_TURN
    assert nudger.gave_up is False
    assert nudger.note(None) == "give_up"          # the 6th failure ends the turn
    assert nudger.total_nudges == MAX_CONTINUATIONS_PER_TURN + 1 and nudger.gave_up
    path = nudger.write_stop_file("prose only", directory=tmp_path)
    assert path.exists() and path.name.startswith("incomplete_")
    assert path.read_text() == "prose only"
    print("  PASS: five consecutive continuations, then the turn stops")


def test_update_plan_still_registered() -> None:
    names = {t.name for t in codex_default_tools()}
    assert "update_plan" in names
    tool_obj = next(t for t in codex_default_tools() if t.name == "update_plan")
    assert callable(tool_obj.fn)
    registry = build_tool_registry(
        legacy_enabled=False, plan_enabled=False, plan_mode=False, verbose=False
    )
    assert "update_plan" in {t.name for t in registry.visible_tools()}
    print("  PASS: update_plan stays registered and callable")


def test_update_plan_rule_removed() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Mandatory planning" not in agents
    assert "update_plan first" not in agents
    assert DONE_TOKEN in agents
    from deepseek.client import TOOL_SYSTEM_PREAMBLE
    assert "call update_plan" not in TOOL_SYSTEM_PREAMBLE
    assert "TASK COMPLETE" not in TOOL_SYSTEM_PREAMBLE
    print("  PASS: planning-first rule removed from AGENTS.md and the preamble")


def test_config_reloaded_per_request(tmp_path, monkeypatch) -> None:
    real = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert real["network_retry"]["response_timeout_minutes"] == 3
    assert real["network_retry"]["max_consecutive_retries"] == 5

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"network_retry": {
        "response_timeout_minutes": 7, "max_consecutive_retries": 2}}))
    monkeypatch.setattr(net, "DEFAULT_CONFIG_PATH", cfg)
    first = net.load_network_retry_config()
    cfg.write_text(json.dumps({"network_retry": {
        "response_timeout_minutes": 1, "max_consecutive_retries": 9}}))
    second = net.load_network_retry_config()
    assert first.stall_timeout_seconds == 420
    assert first.max_consecutive_retries == 2
    assert second.stall_timeout_seconds == 60
    assert second.max_consecutive_retries == 9
    print("  PASS: config.json is re-read on every request")


def test_stall_detector_no_bytes() -> None:
    cfg = net.NetworkRetryConfig(response_timeout_minutes=3, max_consecutive_retries=5)
    events: list[str] = []
    state = {"n": 0}

    def factory():
        state["n"] += 1
        if state["n"] == 1:
            raise net.StallTimeout("no bytes")
        return iter(["data: hi"])

    retrier = net.StallRetrier(
        cfg,
        on_delete_previous=lambda: events.append("delete"),
        on_reattach=lambda: events.append("reattach"),
        error_stream=io.StringIO(),
    )
    assert list(retrier.run(factory)) == ["data: hi"]
    assert events == ["delete"]
    assert state["n"] == 2
    print("  PASS: a no-bytes stall deletes the previous message and resends")


def test_stall_retry_resets_on_success() -> None:
    cfg = net.NetworkRetryConfig(response_timeout_minutes=3, max_consecutive_retries=5)
    trace: list[int] = []
    state: dict = {"n": 0, "retrier": None}

    def factory():
        trace.append(state["retrier"].consecutive_failures)
        state["n"] += 1
        if state["n"] <= 2:
            raise net.StallTimeout("no bytes")
        return iter(["data: ok"])

    retrier = net.StallRetrier(cfg, error_stream=io.StringIO())
    state["retrier"] = retrier
    list(retrier.run(factory))
    trace.append(retrier.consecutive_failures)
    assert trace == [0, 1, 2, 0], trace
    print("  PASS: retry counter goes 0 -> 1 -> 2 -> 0")


def test_stall_max_retries_stops() -> None:
    cfg = net.NetworkRetryConfig(response_timeout_minutes=3, max_consecutive_retries=5)
    buf = io.StringIO()
    attempts = {"n": 0}

    def factory():
        attempts["n"] += 1
        raise net.StallTimeout("no bytes")

    retrier = net.StallRetrier(cfg, error_stream=buf)
    assert list(retrier.run(factory)) == []
    assert attempts["n"] == 5
    out = buf.getvalue()
    assert "Network Connection Error" in out
    assert "did not respond after 5 consecutive attempts" in out
    assert "The operation has been stopped." in out
    print("  PASS: five consecutive stalls stop the operation, no sixth retry")


# ----- freeform apply_patch ------------------------------------------------


class _ScriptedClient:
    """Minimal stand-in for DeepSeekClient that replays scripted responses."""

    def __init__(self, responses: list[list[tuple[str, str]]], finished=True,
                 received_bytes=None, incomplete=False):
        self._responses = list(responses)
        self._finished_seq = list(finished) if isinstance(finished, list) else None
        self._finished_default = True if isinstance(finished, list) else finished
        self._received_seq = (list(received_bytes)
                              if isinstance(received_bytes, list) else None)
        self._received_default = (None if isinstance(received_bytes, list)
                                  else received_bytes)
        self._incomplete = incomplete
        self.prompts: list[str] = []

    def _tool_prompt(self, prompt, schema, conversation_id, extra_sections=None):
        return prompt

    def _remember_tool_prompt(self, *args, **kwargs):
        pass

    def stream(self, prompt, **kwargs):
        self.prompts.append(prompt)
        parts = self._responses.pop(0) if self._responses else []
        index = len(self.prompts) - 1
        if self._finished_seq is not None:
            finished = self._finished_seq[index] if index < len(self._finished_seq) else True
        else:
            finished = self._finished_default

        class _Stream:
            conversation_id = "sid:1"
            tool_calls: list = []

            def iter_parts(self):
                yield from parts

        _Stream.finished = finished
        if self._received_seq is not None:
            received = self._received_seq[index] if index < len(self._received_seq) else True
        elif self._received_default is not None:
            received = self._received_default
        else:
            received = finished
        _Stream.received_bytes = received
        if isinstance(self._incomplete, list):
            _Stream.incomplete = (self._incomplete[index]
                                  if index < len(self._incomplete) else False)
        else:
            _Stream.incomplete = self._incomplete
        _Stream.raw_lines = [f'data: {{"probe":"response {index + 1}"}}']
        return _Stream()


def _run_scripted(responses: list[list[tuple[str, str]]]) -> tuple[DeepSeekCLI, _ScriptedClient]:
    parser = build_parser()
    args = parser.parse_args(["--tools", "auto", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    client = _ScriptedClient(responses)
    app.client = client
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        app._run_agent_turn("hi")
    return app, client


def _patch_block(path: str, old: str, new: str) -> str:
    return (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@ context\n"
        f"-{old}\n"
        f"+{new}\n"
        "*** End Patch"
    )


def test_freeform_patch_detected() -> None:
    patch = _patch_block("/tmp/demo.py", "    return 1", "    return 2")
    processor = ResponseProcessor().feed_all([("answer", patch)])
    calls = processor.freeform_patch_calls
    assert processor.signal == "tool_call"
    assert len(calls) == 1
    assert calls[0].name == "apply_patch"
    assert calls[0].arguments == {"patch": patch}
    assert json.loads(calls[0].raw) == {
        "name": "apply_patch", "arguments": {"patch": patch}
    }
    print("  PASS: freeform patch block is synthesised into an apply_patch call")


def test_freeform_patch_stripped_from_output() -> None:
    patch = _patch_block("/tmp/demo.py", "old", "new")
    processor = ResponseProcessor().feed_all([("answer", patch)])
    visible = processor.visible_answer()
    assert "*** Begin Patch" not in visible
    assert "*** End Patch" not in visible
    assert strip_freeform_patches("x " + patch + " y") == "x  y"
    print("  PASS: freeform patch block never reaches visible output")


def test_json_patch_still_works() -> None:
    patch = _patch_block("/tmp/demo.py", "old", "new")
    payload = json.dumps({"name": "apply_patch", "arguments": {"patch": patch}})
    processor = ResponseProcessor().feed_all([("tool_call", payload)])
    assert processor.signal == "tool_call"
    assert len(processor.tool_calls) == 1
    call = json.loads(processor.tool_calls[0])
    assert call["name"] == "apply_patch" and call["arguments"]["patch"] == patch

    # legacy wrapper arriving inside a RESPONSE fragment is still recovered
    wrapped = f"<tool_call>{payload}</tool_call>"
    fallback = ResponseProcessor().feed_all([("answer", wrapped)])
    assert json.loads(fallback.tool_calls[0])["name"] == "apply_patch"
    print("  PASS: legacy JSON apply_patch calls are still detected")


def test_freeform_with_prose() -> None:
    patch = _patch_block("/tmp/demo.py", "old", "new")
    text = "I'll fix this file:\n\n" + patch + "\n\nDone."
    processor = ResponseProcessor().feed_all([("answer", text)])
    assert processor.signal == "tool_call"
    assert len(processor.freeform_patch_calls) == 1
    visible = processor.visible_answer()
    assert "*** Begin Patch" not in visible
    assert "I'll fix this file:" in visible and "Done." in visible
    print("  PASS: prose around a freeform patch is retained")


def test_freeform_split_across_chunks() -> None:
    patch = _patch_block("/tmp/demo.py", "    return 1", "    return 2")
    points = [round(len(patch) * i / 5) for i in range(6)]
    chunks = [patch[points[i]:points[i + 1]] for i in range(5)]
    assert "".join(chunks) == patch and len(chunks) == 5

    lines = [
        "data: " + json.dumps({
            "p": "response/fragments",
            "o": "APPEND",
            "v": [{"id": i + 1, "type": "RESPONSE", "content": chunk}],
        })
        for i, chunk in enumerate(chunks)
    ]
    processor = ResponseProcessor().feed_all(_parse_sse(lines))
    calls = processor.freeform_patch_calls
    assert len(calls) == 1
    assert calls[0].arguments["patch"] == patch
    assert processor.visible_answer() == ""
    print("  PASS: patch split across 5 SSE chunks is detected after the stream")


def test_freeform_no_end_marker() -> None:
    text = "*** Begin Patch\n*** Update File: x.py\n"
    processor = ResponseProcessor().feed_all([("answer", text)])
    assert processor.freeform_patch_calls == []
    assert processor.signal is None
    assert "*** Begin Patch" in processor.visible_answer()

    # the streaming filter releases an unterminated block instead of eating it
    filt = PatchStreamFilter()
    streamed = "".join(filt.feed(c) for c in ["x ", "*** Beg", "in Patch\nnope\n"])
    streamed += filt.flush()
    assert "*** Begin Patch" in streamed
    print("  PASS: an unterminated patch block is not detected and does not crash")


def test_multiple_patches_all_execute(tmp_path) -> None:
    first = (
        "*** Begin Patch\n"
        f"*** Add File: {tmp_path}/first.txt\n"
        "+first\n"
        "*** End Patch"
    )
    second = (
        "*** Begin Patch\n"
        f"*** Add File: {tmp_path}/second.txt\n"
        "+second\n"
        "*** End Patch"
    )
    processor = ResponseProcessor().feed_all([("answer", first + "\n\n" + second)])
    calls = processor.freeform_patch_calls
    assert len(calls) == 2
    results = execute_tool_calls(ToolExecutor(approval="auto"), calls, [apply_patch])
    assert (tmp_path / "first.txt").read_text() == "first\n"
    assert (tmp_path / "second.txt").read_text() == "second\n"
    assert results[0][1].startswith("applied")
    assert results[1][0] is calls[1] and results[1][1].startswith("applied")
    print("  PASS: both patches execute in order, the second seeing the first")


def test_freeform_patch_lark_grammar_fields(tmp_path) -> None:
    (tmp_path / "src.py").write_text("old\n")
    (tmp_path / "moved.py").write_text("x\n")
    (tmp_path / "gone.txt").write_text("bye\n")
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {tmp_path}/added.txt\n"
        "+new\n"
        f"*** Update File: {tmp_path}/src.py\n"
        "@@\n"
        "-old\n"
        "+newer\n"
        f"*** Delete File: {tmp_path}/gone.txt\n"
        f"*** Update File: {tmp_path}/moved.py\n"
        f"*** Move to: {tmp_path}/moved2.py\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** End Patch"
    )
    for marker in ("*** Begin Patch", "*** End Patch", "*** Add File: ",
                   "*** Update File: ", "*** Delete File: ", "*** Move to: "):
        assert marker in patch
    assert len(find_freeform_patches(patch)) == 1
    operations = _parse_patch(patch)
    assert [op["op"] for op in operations] == ["add", "update", "delete", "update"]
    assert operations[3]["move_to"] == str(tmp_path / "moved2.py")

    assert apply_patch.fn(patch=patch).startswith("applied")
    assert (tmp_path / "added.txt").read_text() == "new\n"
    assert (tmp_path / "src.py").read_text() == "newer\n"
    assert not (tmp_path / "gone.txt").exists()
    assert (tmp_path / "moved2.py").read_text() == "y\n"
    assert not (tmp_path / "moved.py").exists()
    print("  PASS: all patch grammar markers parse and apply")


# ----- permissive-harness behaviour ----------------------------------------


def test_mixed_formats_in_one_response() -> None:
    patch = _patch_block("/tmp/demo.py", "old", "new")
    json_call = json.dumps({"name": "exec_command", "arguments": {"cmd": "ls"}})
    text = f"First.\n\n<tool_call>{json_call}</tool_call>\n\nthen\n\n{patch}"
    calls = extract_tool_calls(text)
    assert [call.name for call in calls] == ["exec_command", "apply_patch"]
    assert calls[0].arguments == {"cmd": "ls"}
    assert calls[1].arguments["patch"] == patch

    # same thing streamed through the processor (tool_call part + answer part)
    processor = ResponseProcessor().feed_all([
        ("answer", "First.\n\n"),
        ("tool_call", json_call),
        ("answer", f"\n\nthen\n\n{patch}"),
    ])
    assert [call.name for call in processor.parsed_tool_calls] == [
        "exec_command", "apply_patch",
    ]
    print("  PASS: JSON and freeform calls in one response extract in order")


def test_soft_continue_message() -> None:
    assert "<<DONE>>" in CONTINUE_PROMPT and "emit a tool call" in CONTINUE_PROMPT
    assert "You stopped" not in CONTINUE_PROMPT
    app, client = _run_scripted([
        [("answer", "Let me think about that.")],
        [("answer", "All done. " + "x" * 220)],
    ])
    assert client.prompts[1] == FIRST_CONTINUE_PROMPT
    assert "The task requires exec_command" not in client.prompts[1]
    print("  PASS: an incomplete response is answered with exactly 'Continue.'")


def test_continuation_counter_resets_on_tool_call() -> None:
    nudger = TurnNudger(MAX_CONTINUATIONS_PER_TURN)
    trace = [nudger.consecutive_failures]
    nudger.note(None)
    trace.append(nudger.consecutive_failures)
    nudger.reset()                       # the CLI resets after executing calls
    trace.append(nudger.consecutive_failures)
    nudger.note(None)
    trace.append(nudger.consecutive_failures)
    assert trace == [0, 1, 0, 1], trace

    app, client = _run_scripted([
        [("answer", "Let me check.")],
        [("tool_call", json.dumps({"name": "exec_command", "arguments": {"cmd": "ls"}}))],
        [("answer", "Let me look again.")],
        [("answer", "Done. " + "y" * 220)],
    ])
    assert client.prompts[1] == FIRST_CONTINUE_PROMPT
    assert "TOOL RESULT for exec_command" in client.prompts[2]
    assert client.prompts[3] == CONTINUE_PROMPT
    print("  PASS: continuation counter is 1, 0, 1 across a tool call")


def test_continuation_renders_on_new_line() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tools", "auto", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    app.reload_tools()
    app.client = _ScriptedClient([
        [("answer", "Hi! What can I help you with today?")],
        [("answer", "No task yet - just say what you'd like me to do.")],
        [("answer", "Ready whenever you are.\n\n<<DONE>>\n")],
    ])
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        app._run_agent_turn("hi")
    rendered = out.getvalue()
    assert "today?No task" not in rendered
    assert "do.Ready" not in rendered
    assert "\n\nNo task yet" in rendered
    assert "\n\nReady whenever you are." in rendered
    print("  PASS: each continued response starts on its own line")


def test_completion_heuristic_substantial_answer() -> None:
    text = "Here is the report. " + ("detail " * 40)
    assert len(text) >= 200
    assert is_complete_response(text) is True
    processor = ResponseProcessor().feed_all([("answer", text)])
    assert processor.complete is True
    app, client = _run_scripted([[("answer", text)], [("answer", "never sent")]])
    assert client.prompts == ["hi"]              # turn ended, no Continue.
    print("  PASS: a substantial final answer ends the turn without <<DONE>>")


def test_completion_heuristic_dangling_preamble() -> None:
    assert is_complete_response("Let me inspect the file.") is False
    assert is_complete_response("I'll run the command now.") is False
    assert is_complete_response("What would you like me to do?") is False
    app, client = _run_scripted([
        [("answer", "Let me inspect the file.")],
        [("answer", "All steps complete.")],
    ])
    assert client.prompts[1] == FIRST_CONTINUE_PROMPT
    print("  PASS: a dangling preamble does not end the turn; Continue. fires")


def test_done_marker_still_stripped() -> None:
    raw = f"Report is ready.\n\n{DONE_TOKEN}\n"
    processor = ResponseProcessor().feed_all([("answer", raw)])
    assert processor.complete is True
    assert DONE_TOKEN not in processor.visible_answer()
    assert processor.visible_answer() == "Report is ready."

    app, client = _run_scripted([[("answer", raw)], [("answer", "never sent")]])
    assert client.prompts == ["hi"]
    assert DONE_TOKEN not in "".join(app._answer_parts)
    print("  PASS: <<DONE>> still ends the turn and never reaches the output")


def test_done_marker_alone_is_valid() -> None:
    processor = ResponseProcessor().feed_all([("answer", f"{DONE_TOKEN}\n")])
    assert processor.done is True
    assert processor.complete is True
    print("  PASS: a response that is only <<DONE>> counts as complete")


def test_done_marker_no_preceding_blank_line_still_invalid() -> None:
    processor = ResponseProcessor().feed_all([("answer", f"Some text\n{DONE_TOKEN}\n")])
    assert processor.done is False
    assert processor.complete is False
    print("  PASS: a marker glued under text is still not complete")


def test_done_marker_with_preceding_blank_line_valid() -> None:
    processor = ResponseProcessor().feed_all([("answer", f"Some text\n\n{DONE_TOKEN}\n")])
    assert processor.done is True
    assert processor.complete is True
    print("  PASS: a marker after a blank line is complete")


def test_five_consecutive_continuations_give_up(tmp_path, monkeypatch) -> None:
    written: list[Path] = []
    original = TurnNudger.write_stop_file

    def spy(self, text, directory="/tmp"):
        path = original(self, text, directory=tmp_path)
        written.append(path)
        return path

    monkeypatch.setattr(TurnNudger, "write_stop_file", spy)
    prose = [[("answer", f"Still thinking {i}.")] for i in range(5)]
    app, client = _run_scripted(prose)
    assert client.prompts[0] == "hi"
    assert client.prompts.count(FIRST_CONTINUE_PROMPT) == 1
    assert client.prompts.count(CONTINUE_PROMPT) == 4
    assert len(written) == 1 and written[0].name.startswith("incomplete_")
    assert "Still thinking 4." in written[0].read_text()
    print("  PASS: five continuations exhaust the turn and write a diagnostic file")


# ----- <<WATCH>> background-session monitoring ------------------------------


# ----- FINISHED gating and the continuation grace period --------------------


# ----- multi-statement commands, continuation limit, directive first ping ---


# ----- the definitive end-of-response marker (see Part 1 stream probes) -----


def test_incomplete_does_not_retry(tmp_path, monkeypatch) -> None:
    saved = tmp_path / "incomplete_stream.txt"
    monkeypatch.setattr(
        DeepSeekCLI, "_save_incomplete_stream",
        staticmethod(lambda stream, directory="/tmp": saved),
    )
    monkeypatch.setenv("DEEPSEEK_SSE_DEBUG", str(tmp_path / "dbg.log"))
    app, client = _scripted_app(
        [[("answer", "Partial bytes arrived.")]],
        finished=False, received_bytes=True, incomplete=True,
    )
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    assert len(client.prompts) == 1                  # the request was not retried
    assert FIRST_CONTINUE_PROMPT not in client.prompts
    assert CONTINUE_PROMPT not in client.prompts
    assert (tmp_path / "dbg.log").read_text().count(
        "server returned INCOMPLETE. No retry"
    ) == 1
    print("  PASS: an INCOMPLETE stream with bytes is not retried")


def test_no_bytes_retries() -> None:
    app, client = _scripted_app([[]], finished=False, received_bytes=False)
    app._grace_sleep = lambda seconds: None
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        app._run_agent_turn("hi")
    assert len(client.prompts) >= 2                  # the request was retried
    assert "did not respond after" in err.getvalue()
    print("  PASS: a stream that delivered no bytes is retried")


def test_thinking_disabled_by_default() -> None:
    real = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert real["thinking"]["enabled"] is False

    from deepseek.net import load_thinking_config
    assert load_thinking_config().enabled is False

    parser = build_parser()
    args = parser.parse_args(["--tools", "auto", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    assert app.thinking is False                     # config default

    forced_on = DeepSeekCLI(
        parser.parse_args(["--thinking", "--tools", "auto", "hi"]),
        parser, interactive=False, prompt="hi",
    )
    assert forced_on.thinking is True

    forced_off = DeepSeekCLI(
        parser.parse_args(["--no-thinking", "--tools", "auto", "hi"]),
        parser, interactive=False, prompt="hi",
    )
    assert forced_off.thinking is False

    body = _Stream(object(), "hi", "sid", None, "default", False, False)._request_body()
    assert body["thinking_enabled"] is False
    print("  PASS: thinking defaults off from config and the payload says so")


def test_completion_waits_for_final_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_SSE_DEBUG", str(tmp_path / "dbg.log"))
    app, client = _scripted_app([
        [("thinking", "thinking..."), ("answer", "Partial answer.")],   # unfinished
        [("answer", "Done.\n\n<<DONE>>\n")],                            # finished
    ], finished=[False, True])
    app._grace_sleep = lambda seconds: None
    _run_app(app)

    turns = [line for line in (tmp_path / "dbg.log").read_text().splitlines()
             if line.startswith("[turn] completion check")]
    assert len(turns) == 1, turns
    assert "finished=True" in turns[0]
    assert FIRST_CONTINUE_PROMPT not in client.prompts
    assert CONTINUE_PROMPT not in client.prompts
    print("  PASS: the completion check waits for the final marker")


def test_think_fragment_completion_does_not_run_check(tmp_path, monkeypatch) -> None:
    think_only = [
        "data: " + json.dumps({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "hmm", "elapsed_secs": None},
        ]}}}),
        "data: " + json.dumps({
            "p": "response/fragments/-1/elapsed_secs", "o": "SET", "v": 1.234,
        }),
    ]
    meta: dict = {}
    list(_parse_sse(think_only, meta))
    assert meta.get("finished") is not True

    monkeypatch.setenv("DEEPSEEK_SSE_DEBUG", str(tmp_path / "dbg.log"))
    app, client = _scripted_app([[("thinking", "hmm")]], finished=False)
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    assert "[turn] completion check" not in (tmp_path / "dbg.log").read_text()
    print("  PASS: a completed THINK fragment never runs the completion check")


def test_final_marker_runs_check_once(tmp_path, monkeypatch) -> None:
    marker = ["data: " + json.dumps({
        "p": "response/status", "o": "SET", "v": "FINISHED",
    })]
    meta: dict = {}
    list(_parse_sse(marker, meta))
    assert meta.get("finished") is True

    for unfinished in ("INCOMPLETE",):
        partial = ["data: " + json.dumps({
            "p": "response/status", "o": "SET", "v": unfinished,
        })]
        partial_meta: dict = {}
        list(_parse_sse(partial, partial_meta))
        assert partial_meta.get("finished") is not True

    monkeypatch.setenv("DEEPSEEK_SSE_DEBUG", str(tmp_path / "dbg.log"))
    app, client = _scripted_app([[("answer", "Done.\n\n<<DONE>>\n")]])
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    turns = [line for line in (tmp_path / "dbg.log").read_text().splitlines()
             if line.startswith("[turn] completion check")]
    assert len(turns) == 1, turns
    assert "finished=True" in turns[0]
    print("  PASS: the final marker runs exactly one completion check")


def test_multi_statement_command_runs_to_completion() -> None:
    result = exec_command.fn(cmd="echo one; sleep 0.2; echo two; sleep 0.2; echo three")
    assert "one" in result and "two" in result and "three" in result, result

    result2 = exec_command.fn(
        cmd='ls venv/bin/python 2>&1; echo \'---\'; '
            'venv/bin/python -c "print(\'hello from python\')"'
    )
    assert "venv/bin/python" in result2, result2
    assert "---" in result2, result2
    assert "hello from python" in result2, result2

    parser = build_parser()
    args = parser.parse_args(["--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        app._emit_tool_result(ToolCall("exec_command", {}, "{}"), result2)
    shown = buf.getvalue()
    assert "venv/bin/python" in shown and "---" in shown and "hello from python" in shown
    print("  PASS: multi-statement commands run fully and all lines are rendered")


def test_continuation_limit_is_five(tmp_path, monkeypatch) -> None:
    written: list = []
    original = TurnNudger.write_stop_file

    def spy(self, text, directory="/tmp"):
        path = original(self, text, directory=tmp_path)
        written.append(path)
        return path

    monkeypatch.setattr(TurnNudger, "write_stop_file", spy)
    app, client = _scripted_app([[("answer", f"prose {i}.")] for i in range(5)])
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    continuations = (client.prompts.count(FIRST_CONTINUE_PROMPT)
                     + client.prompts.count(CONTINUE_PROMPT))
    assert continuations == 5, client.prompts
    assert client.prompts.count(FIRST_CONTINUE_PROMPT) == 1
    assert len(written) == 1                  # then the turn ends
    print("  PASS: exactly five continuations, then the turn ends")


def test_empty_response_does_not_reset_counter() -> None:
    app, client = _scripted_app([
        [("answer", "prose A.")],
        [],                                   # empty response
        [("answer", "prose B.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
    ])
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    # If the empty response had reset the counter, the next ping would have
    # been the directive first-continuation message again.
    assert client.prompts.count(FIRST_CONTINUE_PROMPT) == 1
    assert client.prompts.count(CONTINUE_PROMPT) == 2
    assert len(client.prompts) == 4
    print("  PASS: an empty response does not reset the continuation counter")


def test_first_continuation_message_is_directive() -> None:
    assert FIRST_CONTINUE_PROMPT == (
        "You wrote intent but emitted no tool call. Emit the tool call "
        "now, or write <<DONE>> if the task is finished. Do not write "
        "prose."
    )
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Before your first tool call in a turn, do not narrate." in agents

    app, client = _scripted_app([
        [("answer", "Let me inspect the file.")],
        [("answer", "Second prose.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
    ])
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    assert client.prompts[1] == FIRST_CONTINUE_PROMPT
    assert client.prompts[2] == CONTINUE_PROMPT
    print("  PASS: the first continuation is directive, later ones are questions")


def test_continuation_not_fired_on_partial_stream() -> None:
    partial = [
        "data: " + json.dumps({
            "p": "response/fragments", "o": "APPEND",
            "v": [{"id": 1, "type": "RESPONSE", "content": "Partial answer."}],
        })
    ]
    meta: dict = {}
    assert list(_parse_sse(partial, meta)) == [("answer", "Partial answer.")]
    assert meta.get("finished") is not True

    finished = partial + [
        "data: " + json.dumps({"p": "response/status", "o": "SET", "v": "FINISHED"})
    ]
    meta2: dict = {}
    list(_parse_sse(finished, meta2))
    assert meta2.get("finished") is True

    batched = [
        "data: " + json.dumps({
            "p": "response", "o": "BATCH",
            "v": [{"p": "accumulated_token_usage", "v": 10},
                  {"p": "quasi_status", "v": "FINISHED"}],
        })
    ]
    meta3: dict = {}
    list(_parse_sse(batched, meta3))
    # Only response/status=FINISHED marks the end (Part 1 probes); the
    # quasi_status BATCH is not the marker.
    assert meta3.get("finished") is not True

    app, client = _scripted_app([[("answer", "Partial.")]], finished=False)
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    assert CONTINUE_PROMPT not in client.prompts
    print("  PASS: a stream without FINISHED never runs the completion check")


def test_continuation_fired_only_after_finished() -> None:
    app, client = _scripted_app([
        [("answer", "Short prose.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
    ])
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    assert client.prompts.count(FIRST_CONTINUE_PROMPT) == 1
    assert client.prompts.count(CONTINUE_PROMPT) == 0
    assert len(client.prompts) == 2          # the <<DONE>> response ended the turn
    print("  PASS: exactly one continuation, only after FINISHED")


def test_think_fragment_does_not_trigger_check() -> None:
    app, client = _scripted_app([
        [("thinking", "thinking hard"), ("answer", "Hi there.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
    ])
    app._grace_sleep = lambda seconds: None
    _run_app(app)
    assert client.prompts.count(FIRST_CONTINUE_PROMPT) == 1   # once for the whole stream
    assert len(client.prompts) == 2
    print("  PASS: THINK fragments do not trigger a completion check")


def test_config_reloaded_per_turn(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"continuation": {"grace_ms": 111}}))
    monkeypatch.setattr(net, "DEFAULT_CONFIG_PATH", cfg)
    app, client = _scripted_app([
        [("answer", "Short one.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
        [("answer", "Short two.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
    ])
    sleeps: list[float] = []
    app._grace_sleep = sleeps.append
    _run_app(app)                                    # turn 1
    cfg.write_text(json.dumps({"continuation": {"grace_ms": 222}}))
    _run_app(app)                                    # turn 2
    assert sleeps[0] == 0.111
    assert sleeps[-1] == 0.222
    print("  PASS: config.json is re-read for every turn")


def test_grace_period_applied(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"continuation": {"grace_ms": 150}}))
    monkeypatch.setattr(net, "DEFAULT_CONFIG_PATH", cfg)
    app, client = _scripted_app([
        [("answer", "Short prose.")],
        [("answer", "Done.\n\n<<DONE>>\n")],
    ])
    recorded: list[float] = []
    real_sleep = app._grace_sleep

    def spy(seconds: float) -> None:
        recorded.append(seconds)
        real_sleep(seconds)

    app._grace_sleep = spy
    started = time.monotonic()
    _run_app(app)
    elapsed = time.monotonic() - started
    assert recorded and set(recorded) == {0.15}   # once per completion check
    assert elapsed >= 0.15
    print("  PASS: the continuation waits for the configured grace period")


def _scripted_app(responses, finished=True, received_bytes=None, incomplete=False):
    parser = build_parser()
    args = parser.parse_args(["--tools", "auto", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    app.reload_tools()
    client = _ScriptedClient(responses, finished=finished,
                             received_bytes=received_bytes, incomplete=incomplete)
    app.client = client
    return app, client


def _run_app(app):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        app._run_agent_turn("hi")


WATCH_REPLY = "Waiting for the build to finish.\n\n<<WATCH>>\n"
WATCH_RUNNING_POLL = "compiling...\n[process still running; session_id=sess_test]"
WATCH_EXITED_POLL = "build ok\n[process exited with code 0]"


def test_watch_token_stripped_from_output() -> None:
    processor = ResponseProcessor().feed_all([("answer", WATCH_REPLY)])
    assert processor.watch is True
    assert "<<WATCH>>" not in processor.visible_answer()
    assert processor.visible_answer() == "Waiting for the build to finish."

    parser = build_parser()
    args = parser.parse_args(["--tools", "auto", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    app._handle_part("answer", WATCH_REPLY)
    assert "<<WATCH>>" not in "".join(app._answer_parts)
    print("  PASS: <<WATCH>> never reaches the visible output")


def test_watch_does_not_fire_continuation() -> None:
    app, client = _scripted_app([
        [("answer", WATCH_REPLY)],
        [("answer", "Finished.\n\n<<DONE>>\n")],
    ])
    app._running_session_id = lambda: None
    app._watch_settings = lambda: WatchConfig(0.0, "")
    _run_app(app)
    assert client.prompts[1] == WATCH_NO_SESSION_ERROR
    assert CONTINUE_PROMPT not in client.prompts
    print("  PASS: <<WATCH>> never triggers a continuation")


def test_watch_does_not_reset_failure_counter(tmp_path, monkeypatch) -> None:
    written: list = []
    original = TurnNudger.write_stop_file

    def spy(self, text, directory="/tmp"):
        path = original(self, text, directory=tmp_path)
        written.append(path)
        return path

    monkeypatch.setattr(TurnNudger, "write_stop_file", spy)
    # Two stalls, a watch, then three more: only a successful tool call resets
    # the counter, so the sixth consecutive failure ends the turn.
    prose = [[("answer", f"Still thinking {i}.")] for i in range(5)]
    app, client = _scripted_app(
        prose[:2] + [[("answer", WATCH_REPLY)]] + prose[2:]
    )
    app._running_session_id = lambda: "sess_test"
    app._watch_settings = lambda: WatchConfig(0.0, "")
    app._watch_poll = lambda session_id, chars="": WATCH_RUNNING_POLL
    _run_app(app)
    assert len(written) == 1                   # the watch did not reset the counter
    # Five prose responses plus the empty one after them = six consecutive
    # failures, so the turn gives up on the seventh request.
    assert len(client.prompts) == 7
    print("  PASS: <<WATCH>> does not reset the consecutive-failure counter")


def test_watch_with_no_session_returns_error() -> None:
    app, client = _scripted_app([
        [("answer", WATCH_REPLY)],
        [("answer", "Finished.\n\n<<DONE>>\n")],
    ])
    app._running_session_id = lambda: None
    app._watch_settings = lambda: WatchConfig(0.0, "")
    _run_app(app)
    sent = client.prompts[1]
    assert sent == "ERROR: no running tool session to watch. Call a tool or write <<DONE>>."
    assert "TOOL RESULT for" not in sent
    print("  PASS: watching with no session answers with the error, not a continuation")


def test_watch_polls_session() -> None:
    app, client = _scripted_app([
        [("answer", WATCH_REPLY)],
        [("answer", "Finished.\n\n<<DONE>>\n")],
    ])
    polls: list[tuple[str, str]] = []

    def fake_poll(session_id: str, chars: str = "") -> str:
        polls.append((session_id, chars))
        return WATCH_RUNNING_POLL

    app._running_session_id = lambda: "sess_test"
    app._watch_settings = lambda: WatchConfig(0.0, "")
    app._watch_poll = fake_poll
    _run_app(app)
    assert polls == [("sess_test", "")]
    sent = client.prompts[1]
    assert sent.startswith("TOOL RESULT for sess_test (watched):")
    assert "compiling..." in sent
    assert "[process still running" not in sent
    assert "Continue watching, call another tool" in sent
    print("  PASS: <<WATCH>> polls the running session and feeds output back")


def test_watch_exited_session_reported() -> None:
    app, client = _scripted_app([
        [("answer", WATCH_REPLY)],
        [("answer", "Finished.\n\n<<DONE>>\n")],
    ])
    app._running_session_id = lambda: "sess_test"
    app._watch_settings = lambda: WatchConfig(0.0, "")
    app._watch_poll = lambda session_id, chars="": WATCH_EXITED_POLL
    _run_app(app)
    sent = client.prompts[1]
    assert "build ok" in sent
    assert "The watched session has exited with code 0." in sent
    print("  PASS: an exited watched session reports its exit code")


def test_session_still_running_blocks_completion(monkeypatch) -> None:
    long_prose = "Here is a long summary of the work so far. " + ("detail " * 40)
    assert len(long_prose) >= 200
    call = ToolCall("exec_command", {"cmd": "sleep 90 &"}, "{}")
    running = "started\n[process still running; session_id=sess_x]"
    monkeypatch.setattr(
        "deepseek_cli.execute_tool_calls",
        lambda executor, calls, tools, decisions=None: [(call, running)],
    )
    app, client = _scripted_app([
        [("tool_call", json.dumps({"name": "exec_command", "arguments": {"cmd": "sleep 90 &"}}))],
        [("answer", long_prose)],
        [("answer", "All done.\n\n<<DONE>>\n")],
    ])
    _run_app(app)
    assert client.prompts[2] == FIRST_CONTINUE_PROMPT  # prose did not finish the turn
    assert len(client.prompts) == 3                  # the <<DONE>> response ended it
    print("  PASS: a running session blocks a prose-only completion")


def test_original_prompt_updates_per_turn(monkeypatch) -> None:
    call = ToolCall("exec_command", {"cmd": "echo hi"}, "{}")
    monkeypatch.setattr(
        "deepseek_cli.execute_tool_calls",
        lambda executor, calls, tools, decisions=None: [(call, "hi\n[process exited with code 0]")],
    )
    app, client = _scripted_app([
        [("tool_call", json.dumps({"name": "exec_command", "arguments": {"cmd": "echo hi"}}))],
        [("answer", "First done.\n\n<<DONE>>\n")],
        [("tool_call", json.dumps({"name": "exec_command", "arguments": {"cmd": "echo hi"}}))],
        [("answer", "Second done.\n\n<<DONE>>\n")],
    ])
    app._running_session_id = lambda: None
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        app.run_prompt("first task")
        app.run_prompt("second task")
    footers = [p for p in client.prompts if "TOOL RESULT for" in p]
    assert len(footers) == 2
    assert "first task" in footers[0] and "first task" not in footers[1]
    assert "second task" in footers[1]
    print("  PASS: the TASK STATUS footer follows the current turn's prompt")


def main() -> None:
    test_unknown_tool_returns_error()
    test_parse_sse_collects_multiple_tool_calls()
    test_parse_sse_native_dsml_tool_call()
    test_reply_tool_calls_list()
    test_stream_with_tools_executes_all_calls()
    test_tool_preamble_cached_per_session()
    test_json_tool_result_event_shape()
    test_extension_command_contexts()
    test_toolbar_reads_live_state()
    test_streaming_code_fence_is_consumed()
    test_dsml_suffixed_json_tool_call()
    test_indicator_double_stop_and_agent_default()
    test_broken_pipe_exit_and_json_help()
    print("all CLI regression tests passed")


if __name__ == "__main__":
    main()
