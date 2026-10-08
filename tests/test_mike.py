"""Smoke tests for mike.py — run WITHOUT touching the network.

The OpenAI client is stubbed everywhere it is needed, so this suite is fully
hermetic. Run with:

    venv/bin/python -m pytest tests/test_mike.py -q
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mike  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Prompted-mode tool-call parser                                            #
# --------------------------------------------------------------------------- #

def test_parse_single_tool_call():
    text = (
        "Let me read that file.\n"
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>\n'
        "One moment."
    )
    calls, clean = mike.parse_tool_calls(text)
    assert calls == [{"name": "read_file", "arguments": {"path": "a.py"}}]
    assert "Let me read that file." in clean
    assert "One moment." in clean
    assert "tool_call" not in clean


def test_parse_multiple_tool_calls():
    text = (
        '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'
        '<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>'
    )
    calls, clean = mike.parse_tool_calls(text)
    assert [c["name"] for c in calls] == ["read_file", "list_dir"]
    assert clean == ""


def test_parse_malformed_json_is_ignored():
    text = (
        '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'
        'some text <tool_call>{"name": "broken", "arguments": }</tool_call> more'
    )
    calls, clean = mike.parse_tool_calls(text)
    # Only the well-formed block is a call; the malformed one is dropped, not raised.
    assert [c["name"] for c in calls] == ["read_file"]
    assert "broken" not in clean
    assert "some text" in clean and "more" in clean


def test_parse_text_before_and_after():
    text = (
        "before the call "
        '<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>'
        " after the call"
    )
    calls, clean = mike.parse_tool_calls(text)
    assert calls[0]["name"] == "bash"
    assert clean == "before the call  after the call"


def test_parse_no_tool_calls():
    calls, clean = mike.parse_tool_calls("just a normal answer")
    assert calls == []
    assert clean == "just a normal answer"


def test_parse_arguments_as_json_string():
    text = '<tool_call>{"name": "glob", "arguments": "{\\"pattern\\": \\"*.py\\"}"}</tool_call>'
    calls, _ = mike.parse_tool_calls(text)
    assert calls == [{"name": "glob", "arguments": {"pattern": "*.py"}}]


BAR = "\uff5c"  # full-width vertical bar used by DeepSeek's DSML tool syntax


def _dsml(name, params):
    """Build a DeepSeek DSML tool-call block (params: list of (name, value))."""
    p = f"<{BAR}DSML{BAR}{BAR} parameter name=\"%s\" string=\"true\">%s</{BAR}{BAR}DSML{BAR}{BAR} parameter>"
    body = "\n".join(p % (k, v) for k, v in params)
    return (f"<{BAR}DSML{BAR}{BAR} calls>\n"
            f"<{BAR}{BAR}DSML{BAR}{BAR} invoke name=\"{name}\">\n"
            f"{body}\n"
            f"</{BAR}{BAR}DSML{BAR}{BAR} invoke>\n"
            f"</{BAR}{BAR}DSML{BAR}{BAR} calls>")


def test_parse_dsml_tool_call():
    """The free web model emits DeepSeek's native DSML syntax; mike parses it too."""
    text = _dsml("write_file", [("path", "/tmp/mike_e2e_check.txt"),
                                ("content", "hello from mike")])
    calls, clean = mike.parse_tool_calls(text)
    assert calls == [{"name": "write_file",
                      "arguments": {"path": "/tmp/mike_e2e_check.txt",
                                    "content": "hello from mike"}}]
    assert "DSML" not in clean
    assert "parameter" not in clean


def test_dsml_hidden_from_content():
    text = _dsml("bash", [("command", "ls")])
    reasoning, clean = mike.extract_thinking(text)
    assert clean == ""
    assert "DSML" not in clean


# --------------------------------------------------------------------------- #
# 2. Thinking-block stripper                                                   #
# --------------------------------------------------------------------------- #

def test_strip_think_tag():
    open_tag = "<thinking>"
    close_tag = "</thinking>"
    reasoning, clean = mike.extract_thinking("Answer: " + open_tag + "secret reasoning here" + close_tag + "42")
    assert "secret reasoning here" in reasoning
    assert clean == "Answer: 42"
    assert "reasoning" not in clean


def test_strip_reasoning_tag():
    reasoning, clean = mike.extract_thinking("<reasoning>because</reasoning>final")
    assert reasoning == "because"
    assert clean == "final"


def test_strip_tool_call_from_content():
    _, clean = mike.extract_thinking(
        'hi <tool_call>{"name": "bash", "arguments": {}}</tool_call> bye'
    )
    assert clean == "hi  bye"


def test_stream_parser_incremental_reasoning():
    parser = mike.StreamParser()
    out = []
    for chunk in ["<thinking>", "thinking hard", "<｜end▁of▁thinking｜>", "the answer"]:
        out.extend(parser.feed(chunk))
    out.extend(parser.finish())
    reasoning = "".join(t for k, t in out if k == "reasoning")
    content = "".join(t for k, t in out if k == "content")
    assert reasoning == "thinking hard"
    assert content == "the answer"


def test_reasoning_content_on_mock_delta():
    """A streamed delta carrying `reasoning_content` surfaces as a reasoning event."""
    chunks = [
        _chunk(_delta(reasoning_content="step 1 … ")),
        _chunk(_delta(reasoning_content="step 2. ")),
        _chunk(_delta(content="The answer is 42")),
    ]
    llm = mike.DeepSeekLLM("http://127.0.0.1:1/v1")
    llm._openai = _FakeOpenAI(chunks)
    events = list(llm.chat_stream([{"role": "user", "content": "q"}], thinking=True))
    reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning")
    content = "".join(e["text"] for e in events if e["type"] == "content")
    assert reasoning == "step 1 … step 2. "
    assert content == "The answer is 42"


def test_native_tool_calls_from_mock_delta():
    """Native mode: structured tool_calls on the deltas are accumulated."""
    chunks = [
        _chunk(_delta(tool_calls=[_tc(index=0, id="call_1", name="read_file",
                                      arguments='{"path": "x.py"}')])),
        _chunk(_delta()),
    ]
    llm = mike.DeepSeekLLM("http://127.0.0.1:1/v1")
    llm._openai = _FakeOpenAI(chunks)
    events = list(llm.chat_stream([{"role": "user", "content": "q"}],
                                  tools=[{"type": "function"}]))
    calls = [e for e in events if e["type"] == "tool_calls"][0]["calls"]
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == {"path": "x.py"}


# --------------------------------------------------------------------------- #
# 3. Token estimator                                                           #
# --------------------------------------------------------------------------- #

def test_token_estimator_deterministic():
    counter = mike.TokenCounter("heuristic")
    text = "the quick brown fox jumps over the lazy dog" * 3
    assert counter.count(text) == counter.count(text)
    assert counter.count("x" * 40) == 10


def test_token_counter_create_reports_name():
    counter = mike.TokenCounter.create()
    assert counter.name in ("tiktoken", "heuristic")
    assert counter.count("hello world") >= 1


# --------------------------------------------------------------------------- #
# 4. Compactor trigger / non-trigger                                           #
# --------------------------------------------------------------------------- #

class _MockCounter:
    name = "mock"

    def __init__(self, value):
        self.value = value

    def count(self, text):
        return 0

    def count_messages(self, messages):
        return self.value


def test_compactor_threshold_is_400k():
    comp = mike.Compactor(_MockCounter(0))
    assert comp.max_context == 500_000
    assert comp.trigger_tokens == 400_000


def test_compactor_triggers_at_400k():
    comp = mike.Compactor(_MockCounter(400_001))
    assert comp.should_compact([{"role": "user", "content": "x"}]) is True


def test_compactor_does_not_trigger_at_300k():
    comp = mike.Compactor(_MockCounter(300_000))
    assert comp.should_compact([{"role": "user", "content": "x"}]) is False


def test_summary_has_seven_sections():
    comp = mike.Compactor(_MockCounter(0))
    messages = [
        {"role": "user", "content": "Build a web scraper that must never hit prod."},
        {"role": "assistant", "content": "I decided to use httpx instead of requests."},
        {"role": "user", "content": "<tool_result>\n1. [ ] write scraper\n2. [ ] add tests\n</tool_result>"},
    ]
    summary = comp.build_summary(messages)
    for header in mike.SUMMARY_SECTIONS:
        assert header in summary, f"missing {header}"


# --------------------------------------------------------------------------- #
# 5. Session rollover                                                          #
# --------------------------------------------------------------------------- #

def test_rollover_creates_cont1_with_seeded_summary(tmp_path):
    store = mike.SessionStore(tmp_path)
    session = store.new_session(name="demo")
    session.messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "sure"},
    ]
    # Mock counter always reports above the threshold, so post-compaction the
    # estimate is still over budget and a rollover must happen.
    comp = mike.Compactor(_MockCounter(500_000), keep_recent=2)
    agent = mike.MikeAgent(
        llm=None, session=session, store=store, compactor=comp,
        toolbox=mike.Toolbox(str(tmp_path)), console=mike._PlainConsole(),
        cwd=str(tmp_path),
    )
    summary = agent.compact(force=True)

    assert "## Goal" in summary and "## Next Steps" in summary
    new = agent.session
    assert new.name == "demo-cont1"
    assert new.messages[0]["content"].startswith(
        "This is a continuation of session demo. Summary follows:"
    )
    assert "## Goal" in new.messages[0]["content"]
    created = list(tmp_path.glob("*__demo-cont1.json"))
    assert created, "expected a -cont1 session file on disk"
    data = json.loads(created[0].read_text())
    assert data["name"] == "demo-cont1"
    assert data["messages"][0]["content"].startswith("This is a continuation of session demo.")


def test_session_file_is_pretty_sorted_json(tmp_path):
    store = mike.SessionStore(tmp_path)
    session = store.new_session(name="fmt")
    session.messages.append({"role": "user", "content": "hi"})
    session.save()
    raw = session.path.read_text()
    assert raw.startswith("{\n")          # pretty-printed
    keys = list(json.loads(raw).keys())
    assert keys == sorted(keys)           # sorted keys


# --------------------------------------------------------------------------- #
# 6. Alias routing                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("alias", [
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v3",
    "deepseek-v4",
    "deepseek-v4.1-flash",
    "deepseek-flash",
    "deepseek-web",
    "default",
    "something-unknown",
    None,
])
def test_alias_routing(alias):
    assert mike.resolve_model(alias) == "deepseek-v4.1-flash"


def test_single_model_identity():
    assert mike.MODEL_ID == "deepseek-v4.1-flash"
    assert mike.MODEL_LABEL == "DeepSeek V4.1 Flash"


# --------------------------------------------------------------------------- #
# 7. End-to-end agent loop with a stubbed client                               #
# --------------------------------------------------------------------------- #

class _FakeLLM:
    """A stub LLM: first turn asks for a tool, second turn gives the answer."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.seen_messages = []
        self.base_url = "http://stub/v1"

    def chat_stream(self, messages, tools=None, thinking=False, search=False):
        self.seen_messages.append(messages)
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        for event in turn:
            yield event

    def complete(self, messages, thinking=False, max_tokens=2048):
        return "ok"


def test_agent_runs_tool_then_answers(tmp_path):
    tool_turn = [
        {"type": "content", "text":
            '<tool_call>{"name": "write_file", "arguments": '
            '{"path": "hello.txt", "content": "hi"}}</tool_call>'},
    ]
    answer_turn = [{"type": "content", "text": "Done — created hello.txt."}]
    llm = _FakeLLM([tool_turn, answer_turn])

    store = mike.SessionStore(tmp_path / "sessions")
    session = store.new_session(name="e2e")
    comp = mike.Compactor(mike.TokenCounter("heuristic"))
    agent = mike.MikeAgent(
        llm=llm, session=session, store=store, compactor=comp,
        toolbox=mike.Toolbox(str(tmp_path)), console=mike._PlainConsole(),
        tool_mode="prompted", cwd=str(tmp_path),
    )

    result = agent.run("create hello.txt")

    assert (tmp_path / "hello.txt").read_text() == "hi"
    assert result == "Done — created hello.txt."
    # Tool result was fed back as a user message wrapped in <tool_result>.
    assert any("<tool_result>" in str(m.get("content")) for m in session.messages)
    # Reasoning/tool envelopes are not left in assistant content.
    for m in session.messages:
        if m["role"] == "assistant":
            assert "<tool_call>" not in (m.get("content") or "")


def test_thinking_never_becomes_assistant_content(tmp_path):
    turn = [{"type": "content", "text": "<thinking>thinking...<｜end▁of▁thinking｜>visible answer"}]
    llm = _FakeLLM([turn])
    store = mike.SessionStore(tmp_path / "s")
    session = store.new_session(name="t")
    agent = mike.MikeAgent(
        llm=llm, session=session, store=store,
        compactor=mike.Compactor(mike.TokenCounter("heuristic")),
        toolbox=mike.Toolbox(str(tmp_path)), console=mike._PlainConsole(),
        tool_mode="prompted", cwd=str(tmp_path),
    )
    agent.run("hi")
    assistant = [m for m in session.messages if m["role"] == "assistant"][-1]
    assert assistant["content"] == "visible answer"
    assert "thinking..." not in assistant["content"]


# --------------------------------------------------------------------------- #
# Stub helpers                                                                 #
# --------------------------------------------------------------------------- #

def _delta(content=None, reasoning_content=None, tool_calls=None):
    return SimpleNamespace(content=content, reasoning_content=reasoning_content,
                           tool_calls=tool_calls)


def _tc(index, id, name, arguments):
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=arguments))


def _chunk(delta, finish_reason=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **kwargs):
        return iter(self._chunks)


class _FakeOpenAI:
    def __init__(self, chunks):
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))


# --------------------------------------------------------------------------- #
# 8. Layered config (v0.2)                                                     #
# --------------------------------------------------------------------------- #

class TestConfig:
    def _clean_env(self, monkeypatch):
        for name in list(mike.ENV_MAP.values()) + ["HOST", "PORT", "MIKE_PORT"]:
            monkeypatch.delenv(name, raising=False)

    def test_defaults_when_no_toml(self, tmp_path, monkeypatch):
        self._clean_env(monkeypatch)
        cfg, src = mike.load_config({}, user_config=tmp_path / "u.toml",
                                    repo_config=tmp_path / "r.toml")
        assert cfg["max_context"] == 500_000
        assert cfg["compact_threshold"] == 0.80
        assert cfg["server_port"] == 8000
        assert cfg["model_id"] == "deepseek-v4.1-flash"
        assert set(src.values()) == {"default"}

    def test_precedence_default_user_repo_env_cli(self, tmp_path, monkeypatch):
        self._clean_env(monkeypatch)
        u = tmp_path / "u.toml"
        r = tmp_path / "r.toml"
        u.write_text("max_context = 100000\n")
        r.write_text("max_context = 300000\n")

        cfg, src = mike.load_config({}, user_config=u, repo_config=r)
        assert (cfg["max_context"], src["max_context"]) == (300_000, "repo")

        monkeypatch.setenv("MIKE_MAX_CONTEXT", "250000")
        cfg, src = mike.load_config({}, user_config=u, repo_config=r)
        assert (cfg["max_context"], src["max_context"]) == (250_000, "env")

        cfg, src = mike.load_config({"max_context": 200_000}, user_config=u, repo_config=r)
        assert (cfg["max_context"], src["max_context"]) == (200_000, "cli")

    def test_all_keys_optional_and_typed(self, tmp_path, monkeypatch):
        self._clean_env(monkeypatch)
        r = tmp_path / "r.toml"
        r.write_text('server_port = 8123\ncompact_threshold = 0.5\n'
                     'log_level = "debug"\nthinking_default = "on"\n')
        cfg, src = mike.load_config({}, user_config=tmp_path / "u.toml", repo_config=r)
        assert cfg["server_port"] == 8123 and src["server_port"] == "repo"
        assert cfg["compact_threshold"] == 0.5
        assert cfg["log_level"] == "debug"
        assert cfg["thinking_default"] == "on"

    def test_invalid_toml_is_ignored(self, tmp_path, monkeypatch):
        self._clean_env(monkeypatch)
        r = tmp_path / "r.toml"
        r.write_text("this is not = valid toml [[[\n")
        cfg, src = mike.load_config({}, user_config=tmp_path / "u.toml", repo_config=r)
        assert cfg["max_context"] == mike.DEFAULTS["max_context"]
        assert src["max_context"] == "default"

    def test_env_alias_host_port(self, tmp_path, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("PORT", "9123")
        cfg, src = mike.load_config({}, user_config=tmp_path / "u.toml",
                                    repo_config=tmp_path / "r.toml")
        assert cfg["server_port"] == 9123
        assert src["server_port"] == "env"

    def test_compact_at_derived_from_max(self):
        cfg = dict(mike.DEFAULTS)
        cfg["max_context"] = 500_000
        cfg["compact_threshold"] = 0.80
        assert mike.compact_at(cfg) == 400_000
        cfg["max_context"] = 200_000
        assert mike.compact_at(cfg) == 160_000

    def test_dir_resolution(self, tmp_path):
        cfg = {"session_dir": str(tmp_path / "s"), "export_dir": ""}
        assert mike.resolved_session_dir(cfg) == tmp_path / "s"
        assert mike.resolved_export_dir(cfg) == mike.ROOT / "exports"
