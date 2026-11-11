import json
from datetime import datetime
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunConfig,
    Runner,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from ai_inbox.application.outgoing_dto import EmailComposition
from ai_inbox.application.ports import ToolExecutor
from ai_inbox.domain.errors import AssistantFailure, InvalidInput, ProviderUnavailable
from ai_inbox.domain.outgoing import validate_content

from .prompts import ASSISTANT, COMPOSE_EMAIL, SUMMARY
from .retry import provider_request

# No prompts, mail or tool arguments go to OpenAI tracing.
set_tracing_disabled(True)


def tool_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
NULL_TEXT = {"type": ["string", "null"]}
TOOL_DEFINITIONS = {
    "list_unread_emails": (
        "Последние непрочитанные письма, не более 20.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
    ),
    "read_email": ("Прочитать письмо по ID без изменения флага прочтения.", {"email_id": TEXT}),
    "create_task": (
        "Создать задачу по явной просьбе. Неясный срок требует уточнения.",
        {
            "task_title": {"type": "string", "description": "Короткое название задачи"},
            "notes": {"type": "string", "description": "Детали, номера и суммы с копейками"},
            "due_at": {
                "type": ["string", "null"],
                "description": "Точный ISO 8601 срок с часовым поясом или null",
            },
            "source": {
                "type": "string",
                "description": "Источник: image:<ID>, email:<ID> или manual",
            },
        },
    ),
    "list_tasks": ("Список задач: open, completed или null для всех.", {"status": NULL_TEXT}),
    "complete_task": ("Завершить задачу по ID по явной просьбе владельца.", {"task_id": TEXT}),
    "create_draft": (
        "Сохранить локальный черновик ответа, никогда не отправлять.",
        {"email_id": TEXT, "subject": TEXT, "body": TEXT},
    ),
    "prepare_outgoing_email": (
        "Подготовить исходящий черновик с явным адресом пользователя. Без отправки.",
        {"recipient": TEXT, "subject": TEXT, "body": TEXT},
    ),
    "review_outgoing_email": (
        "Показать актуальный исходящий черновик перед новым подтверждением отправки.",
        {"outbox_id": TEXT},
    ),
    "send_outgoing_email": (
        "Отправить единственный показанный черновик после отдельного подтверждения владельца. "
        "Получатель и версия определяются приложением, а не аргументами модели.",
        {},
    ),
    "generate_digest": ("Создать и сохранить дайджест по явной просьбе владельца.", {}),
}


def build_tools(execute: ToolExecutor) -> list[FunctionTool]:
    tools = []
    for name, (description, properties) in TOOL_DEFINITIONS.items():

        def build_invoke(tool_name):
            async def invoke(context, raw):
                try:
                    args = json.loads(raw)
                    # Keep provider-facing schema names separate from domain/API field names.
                    if tool_name == "create_task" and "task_title" in args:
                        args["title"] = args.pop("task_title")
                    result = await execute(tool_name, args, context.tool_call_id)
                except (ValueError, TypeError):
                    result = {
                        "error": {
                            "code": "invalid_arguments",
                            "message": "Неверные аргументы инструмента",
                        }
                    }
                return json.dumps(result, ensure_ascii=False)

            return invoke

        tools.append(
            FunctionTool(
                name=name,
                description=description,
                params_json_schema=tool_schema(properties),
                on_invoke_tool=build_invoke(name),
            )
        )
    return tools


class RetryingChatModel(OpenAIChatCompletionsModel):
    def __init__(self, model: str, client: AsyncOpenAI, attempts: int):
        super().__init__(model=model, openai_client=client)
        self.attempts = attempts

    async def get_response(self, *args, **kwargs):
        parent = super().get_response
        return await provider_request(lambda: parent(*args, **kwargs), self.attempts)


class AnyModelAssistant:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        attempts: int,
        max_turns: int,
        timezone: str,
        sdk_model=None,
    ):
        self.model = sdk_model or RetryingChatModel(model, client, attempts)
        self.max_turns, self.timezone = max_turns, timezone
        self.options = ModelSettings(max_tokens=4096, parallel_tool_calls=False)

    async def chat(
        self, history: list[dict[str, Any]], execute: ToolExecutor, now: datetime
    ) -> tuple[str, list[dict[str, Any]]]:
        agent = Agent(
            name="AI Inbox",
            instructions=ASSISTANT.format(now=now.isoformat(), timezone=self.timezone),
            model=self.model,
            model_settings=self.options,
            tools=build_tools(execute),
        )
        try:
            result = await Runner.run(
                agent,
                history,
                max_turns=self.max_turns,
                run_config=RunConfig(tracing_disabled=True),
            )
            answer = str(result.final_output or "").strip()
            if not answer:
                raise ProviderUnavailable("AnyModel вернул пустой текстовый ответ.")
            return answer, result.to_input_list()
        except ProviderUnavailable:
            raise
        except (MaxTurnsExceeded, ModelBehaviorError) as exc:
            # SDK errors can contain entire prompts. Do not echo or log their bodies.
            raise AssistantFailure("Агент не завершил текстовый ответ.") from exc

    async def summarize(self, context: str) -> str:
        agent = Agent(
            name="AI Inbox summary",
            instructions=SUMMARY,
            model=self.model,
            model_settings=self.options,
        )
        try:
            result = await Runner.run(
                agent, context, max_turns=2, run_config=RunConfig(tracing_disabled=True)
            )
            answer = str(result.final_output or "").strip()
            if not answer:
                raise ProviderUnavailable("AnyModel вернул пустую сводку. Попробуй позже.")
            return answer
        except ProviderUnavailable:
            raise
        except (MaxTurnsExceeded, ModelBehaviorError) as exc:
            raise AssistantFailure("Агент не завершил сводку. Попробуй позже.") from exc

    async def compose_email(self, outline: str) -> EmailComposition:

        agent = Agent(
            name="AI Inbox letter writer",
            instructions=COMPOSE_EMAIL,
            model=self.model,
            model_settings=self.options,
        )
        try:
            result = await Runner.run(
                agent, outline, max_turns=2, run_config=RunConfig(tracing_disabled=True)
            )
            text = str(result.final_output or "").strip()
            if text.startswith("```") and text.endswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            data = json.loads(text)
            if (
                not isinstance(data, dict)
                or set(data) != {"subject", "body"}
                or not all(isinstance(v, str) for v in data.values())
            ):
                raise ValueError
            validate_content(data["subject"], data["body"])
            return EmailComposition(data["subject"], data["body"])
        except (ValueError, TypeError, InvalidInput, MaxTurnsExceeded, ModelBehaviorError) as exc:
            raise AssistantFailure(
                "ИИ не вернул корректные тему и текст. Поля редактора сохранены."
            ) from exc
