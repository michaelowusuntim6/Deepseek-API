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
from deepseek.client import DeepSeekClient, Reply, _parse_sse
from deepseek.tools import Tool, ToolCall, execute_tool
from deepseek_cli import DeepSeekCLI, build_parser, read_file


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
    assert "Available tools:" in client.prompts[0]
    print("  PASS: B4 stream_with_tools executes every requested tool")


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


def test_panel_dirty_on_state_change() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tools", "off", "hi"])
    app = DeepSeekCLI(args, parser, interactive=False, prompt="hi")
    app._panel_dirty = False
    app.command_toggle("thinking", "on")
    assert app._panel_dirty is True
    print("  PASS: panel dirty flag is set by state changes")


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


def main() -> None:
    test_unknown_tool_returns_error()
    test_parse_sse_collects_multiple_tool_calls()
    test_parse_sse_native_dsml_tool_call()
    test_reply_tool_calls_list()
    test_stream_with_tools_executes_all_calls()
    test_tool_preamble_cached_per_session()
    test_json_tool_result_event_shape()
    test_extension_command_contexts()
    test_panel_dirty_on_state_change()
    test_broken_pipe_exit_and_json_help()
    print("all CLI regression tests passed")


if __name__ == "__main__":
    main()
