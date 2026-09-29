"""
Offline tests for server function calling translation and response generation.
"""

import json
from dataclasses import dataclass
from server.schemas import ChatMessage
from server.openai_format import (
    render_tools_preamble,
    messages_to_prompt,
    completion_response_with_tool_call,
    stream_chunks,
)

@dataclass
class FakeToolCall:
    name: str
    arguments: dict
    raw: str = ""

def test_all_offline():
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]

    # a. render_tools_preamble contains name and description
    p1 = render_tools_preamble(openai_tools)
    assert p1 is not None
    assert "get_weather" in p1
    assert "Get current weather" in p1

    # b. tool_choice="none" returns None
    assert render_tools_preamble(openai_tools, tool_choice="none") is None

    # c. tool_choice="required" appends sentence
    p_req = render_tools_preamble(openai_tools, tool_choice="required")
    assert "You MUST call one of the tools listed above." in p_req

    # d. messages_to_prompt with assistant tool_calls emits <tool_call>
    msgs_asst = [
        ChatMessage(
            role="assistant",
            content="Checking weather...",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                }
            ],
        )
    ]
    prompt_asst = messages_to_prompt(msgs_asst)
    assert "<tool_call>{\"name\": \"get_weather\", \"arguments\": {\"city\": \"Tokyo\"}}</tool_call>" in prompt_asst

    # e. messages_to_prompt with role="tool" matches tool_call_id
    msgs_tool = [
        ChatMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            tool_call_id="call_abc",
            content="22C, clear",
        ),
    ]
    prompt_tool = messages_to_prompt(msgs_tool)
    assert "User: TOOL RESULT for get_weather:\n22C, clear" in prompt_tool

    # f. completion_response_with_tool_call properties
    ftc = FakeToolCall(name="get_weather", arguments={"city": "Tokyo"})
    resp = completion_response_with_tool_call("deepseek-chat", ftc, conversation_id="cid123")
    msg = resp["choices"][0]["message"]
    assert msg["content"] is None
    assert "content" in msg
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert isinstance(msg["tool_calls"][0]["function"]["arguments"], str)
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"city": "Tokyo"}
    assert msg["tool_calls"][0]["id"].startswith("call_")
    assert len(msg["tool_calls"][0]["id"]) == 29
    assert resp["choices"][0]["finish_reason"] == "tool_calls"

    # g. Stream test with tool call
    class FakeStreamTool:
        conversation_id = "cid_stream"
        def iter_parts(self):
            yield ("thinking", "hmm")
            yield ("tool_call", '{"name":"x","arguments":{"a":1}}')

    lines_tool = list(stream_chunks("deepseek-chat", FakeStreamTool()))
    tc_chunks = [json.loads(l[len("data: "):]) for l in lines_tool if l.startswith("data: {") and "tool_calls" in json.loads(l[len("data: "):])["choices"][0]["delta"]]
    assert len(tc_chunks) == 2, f"Expected 2 tool call chunks, got {len(tc_chunks)}"

    c1 = tc_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert c1["id"].startswith("call_")
    assert c1["type"] == "function"
    assert c1["function"]["name"] == "x"
    assert c1["function"]["arguments"] == ""

    c2 = tc_chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    assert "id" not in c2
    assert "name" not in c2["function"]
    assert c2["function"]["arguments"] == '{"a": 1}'

    final_chunk = [json.loads(l[len("data: "):]) for l in lines_tool if l.startswith("data: {") and json.loads(l[len("data: "):])["choices"][0]["finish_reason"] is not None][0]
    assert final_chunk["choices"][0]["finish_reason"] == "tool_calls"
    assert lines_tool[-1] == "data: [DONE]\n\n"

    # h. Backwards compat stream test
    class FakeStreamAnswer:
        conversation_id = "cid_answer"
        def iter_parts(self):
            yield ("answer", "hello ")
            yield ("answer", "world")

    lines_ans = list(stream_chunks("deepseek-chat", FakeStreamAnswer()))
    final_ans_chunk = [json.loads(l[len("data: "):]) for l in lines_ans if l.startswith("data: {") and json.loads(l[len("data: "):])["choices"][0]["finish_reason"] is not None][0]
    assert final_ans_chunk["choices"][0]["finish_reason"] == "stop"

    print("all offline tests passed")

if __name__ == "__main__":
    test_all_offline()


def test_testclient_mock():
    from fastapi.testclient import TestClient
    from server.api import app, get_client
    from deepseek.tools import ToolCall
    from deepseek.client import Reply

    class FakeClientWithTool:
        def chat(self, prompt, conversation_id=None, model_type=None, thinking=False, search=False):
            return Reply(
                text="",
                conversation_id="cid_mock_tool",
                thinking="",
                tool_call=ToolCall(name="get_weather", arguments={"city": "Tokyo"}, raw='{"name":"get_weather","arguments":{"city":"Tokyo"}}')
            )

    class FakeClientNormal:
        def chat(self, prompt, conversation_id=None, model_type=None, thinking=False, search=False):
            return Reply(
                text="Hello there!",
                conversation_id="cid_mock_normal",
                thinking="",
                tool_call=None
            )

    # 4. Mock test returning tool_calls
    app.dependency_overrides = {}
    import server.api
    server.api._client = FakeClientWithTool()

    tc = TestClient(app)
    req_body_tools = {
        "model": "deepseek-chat",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        "messages": [{"role": "user", "content": "Weather in Tokyo?"}],
    }
    r1 = tc.post("/v1/chat/completions", json=req_body_tools)
    assert r1.status_code == 200, f"Status code: {r1.status_code}, body: {r1.text}"
    data1 = r1.json()
    assert data1["choices"][0]["finish_reason"] == "tool_calls"
    msg1 = data1["choices"][0]["message"]
    assert msg1["content"] is None
    assert msg1["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(msg1["tool_calls"][0]["function"]["arguments"]) == {"city": "Tokyo"}

    # 5. Mock test returning normal answer
    server.api._client = FakeClientNormal()
    req_body_normal = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    r2 = tc.post("/v1/chat/completions", json=req_body_normal)
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["choices"][0]["finish_reason"] == "stop"
    assert data2["choices"][0]["message"]["content"] == "Hello there!"

    print("Phase 2 TestClient mock tests passed")

if __name__ == "__main__":
    test_testclient_mock()
