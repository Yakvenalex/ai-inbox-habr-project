import json

from agents import Model, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from ai_inbox.infrastructure.ai.assistant import TOOL_DEFINITIONS, AnyModelAssistant, build_tools
from tests.fakes import FakeClock


class ToolCycleModel(Model):
    def __init__(self):
        self.inputs = []

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        self.inputs.append(input)
        assert set(t.name for t in tools) == set(TOOL_DEFINITIONS)
        if len(self.inputs) == 1:
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    id="fc1",
                    call_id="sdk-call-1",
                    name="create_task",
                    arguments=json.dumps(
                        {"title": "Учебная задача", "notes": "", "due_at": None, "source": "manual"}
                    ),
                )
            ]
        else:
            result = next(item for item in input if item.get("type") == "function_call_output")
            assert json.loads(result["output"])["id"] == "real-task-id"
            output = [
                ResponseOutputMessage(
                    type="message",
                    id="msg1",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text="Создана задача real-task-id",
                            annotations=[],
                            logprobs=[],
                        )
                    ],
                )
            ]
        return ModelResponse(output=output, usage=Usage(), response_id="synthetic-response")

    async def stream_response(self, *args, **kwargs):
        if False:
            yield None


async def test_real_agents_sdk_tool_cycle_and_serializable_history():
    model = ToolCycleModel()
    assistant = AnyModelAssistant(None, "fake-model", 2, 6, "Europe/Moscow", sdk_model=model)
    calls = []

    async def execute(name, args, call_id):
        calls.append((name, args, call_id))
        return {"id": "real-task-id", "status": "open"}

    text, history = await assistant.chat(
        [{"role": "user", "content": "Создай задачу"}], execute, FakeClock().now()
    )
    assert "real-task-id" in text and len(calls) == 1
    assert calls[0][2] == "sdk-call-1"
    assert any(item.get("type") == "function_call" for item in history)
    assert any(item.get("type") == "function_call_output" for item in history)
    json.dumps(history)


def test_tools_have_strict_schemas_and_no_send():
    async def execute(*args):
        return {}

    tools = build_tools(execute)
    assert "send_email" not in {t.name for t in tools}
    for tool in tools:
        schema = tool.params_json_schema
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
