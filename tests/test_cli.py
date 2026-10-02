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
    MAX_NUDGES_PER_TURN,
    NUDGE_MESSAGE,
    PatchStreamFilter,
    ResponseProcessor,
    TurnNudger,
    execute_first_tool_call,
    find_freeform_patches,
    has_valid_done,
    strip_done,
    strip_freeform_patches,
)
from deepseek.agent_tools import ToolExecutor, _parse_patch, apply_patch
from deepseek.tools import Tool, ToolCall, execute_tool
from deepseek_cli import (
    DeepSeekCLI,
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


def test_stream_with_tools_first_call_only() -> None:
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
    assert calls == [("a", {})]
    results = [text for kind, text in events if kind == "tool_result"]
    assert any("ERROR: Multiple tool calls" in text and "TOOL RESULT for b" in text
               for text in results)
    assert events[-1] == ("answer", "done")
    assert "Available tools:" in client.prompts[0]
    print("  PASS: stream_with_tools executes only the first requested tool")


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
    assert "Available tools:" in first and first.endswith("User: hello")
    client._remember_tool_prompt(schema, "sid:1")
    second = client._tool_prompt("again", schema, "sid:2")
    assert second == "again"
    changed = client._tool_prompt("again", schema + " ", "sid:2")
    assert "Available tools:" in changed
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
    nudger = TurnNudger(MAX_NUDGES_PER_TURN)
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
        f"{DONE_TOKEN}\n",
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


def test_multiple_tool_calls_first_only() -> None:
    tools, executed = _tool_set(["alpha", "beta", "gamma"])
    calls = [
        ToolCall("alpha", {}, '{"name":"alpha","arguments":{}}'),
        ToolCall("beta", {"x": 1}, '{"name":"beta","arguments":{"x":1}}'),
        ToolCall("gamma", {}, '{"name":"gamma","arguments":{}}'),
    ]
    results = execute_first_tool_call(ToolExecutor(approval="auto"), calls, tools)
    assert executed == ["alpha"]
    assert len(results) == 3
    assert results[0] == (calls[0], "alpha result")
    print("  PASS: only the first of three tool calls executes")


def test_multiple_tool_calls_synthetic_errors() -> None:
    tools, _ = _tool_set(["alpha", "beta", "gamma"])
    calls = [
        ToolCall("alpha", {}, "{}"),
        ToolCall("beta", {}, "{}"),
        ToolCall("gamma", {}, "{}"),
    ]
    results = execute_first_tool_call(ToolExecutor(approval="auto"), calls, tools)
    assert results[1][0].name == "beta"
    assert results[1][1].startswith("TOOL RESULT for beta:\nERROR: Multiple tool calls")
    assert "Only the first was executed" in results[1][1]
    assert results[2][0].name == "gamma"
    assert results[2][1].startswith("TOOL RESULT for gamma:\nERROR: Multiple tool calls")
    print("  PASS: synthetic errors name beta and gamma")


def test_nudge_message_has_no_hardcoded_paths() -> None:
    assert NUDGE_MESSAGE == (
        "No tool call or <<DONE>> detected. Finish properly: either emit a "
        "<tool_call> block now, or write <<DONE>> on its own line. Do not write prose."
    )
    for banned in ("~/", "Downloads", "hf_results", "ls ", "/home/", "Hugginface"):
        assert banned not in NUDGE_MESSAGE, banned
    print("  PASS: nudge text has no hardcoded commands or paths")


def test_nudge_counter_resets_on_tool_success() -> None:
    nudger = TurnNudger(MAX_NUDGES_PER_TURN)
    trace = [nudger.consecutive_failures]
    for signal in (None, "tool_call", None):
        nudger.note(signal)
        trace.append(nudger.consecutive_failures)
    assert trace == [0, 1, 0, 1], trace
    assert nudger.total_nudges == 2
    print("  PASS: nudge counter resets to 0 after a successful tool call")


def test_nudge_counter_dies_after_five_consecutive(tmp_path) -> None:
    nudger = TurnNudger(MAX_NUDGES_PER_TURN)
    decisions = [nudger.note(None) for _ in range(MAX_NUDGES_PER_TURN)]
    assert decisions[:-1] == ["nudge"] * (MAX_NUDGES_PER_TURN - 1)
    assert decisions[-1] == "give_up"
    assert nudger.total_nudges == 5 and nudger.gave_up
    path = nudger.write_stop_file("prose only", directory=tmp_path)
    assert path.exists() and path.name.startswith("prose_stop_")
    assert path.read_text() == "prose only"
    print("  PASS: five consecutive failures nudge five times, then stop")


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


def test_multiple_patches_first_wins(tmp_path) -> None:
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
    results = execute_first_tool_call(ToolExecutor(approval="auto"), calls, [apply_patch])
    assert (tmp_path / "first.txt").read_text() == "first\n"
    assert not (tmp_path / "second.txt").exists()
    assert results[0][1].startswith("applied")
    assert results[1][0] is calls[1]
    assert results[1][1].startswith(
        "TOOL RESULT for apply_patch:\nERROR: Multiple tool calls"
    )
    print("  PASS: only the first patch executes; the second reports an error")


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


def main() -> None:
    test_unknown_tool_returns_error()
    test_parse_sse_collects_multiple_tool_calls()
    test_parse_sse_native_dsml_tool_call()
    test_reply_tool_calls_list()
    test_stream_with_tools_first_call_only()
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
