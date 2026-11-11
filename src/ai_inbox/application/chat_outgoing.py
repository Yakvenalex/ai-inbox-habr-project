"""Deterministic chat confirmation; model text never grants send permission."""

import re
from typing import Any

from ai_inbox.domain.entities import PendingEmailReview, new_id

REVIEW_TOOLS = {"prepare_outgoing_email", "review_outgoing_email"}


def confirms_sending(text: str) -> bool:
    if len(text) > 160 or any(char in text for char in "?\"«»'"):
        return False
    normalized = re.sub(r"[,.!;:]+", " ", text.lower().replace("ё", "е"))
    normalized = " ".join(normalized.split())
    phrase = (
        r"да|ок|окей|все (?:ок|окей|хорошо|верно|правильно)|"
        r"отправ(?:ляй(?:те)?|ь(?:те)?)(?: письмо)?|подтверждаю(?: отправку)?|можно отправлять"
    )
    return bool(re.fullmatch(rf"(?:{phrase})(?: (?:{phrase}))*", normalized))


def pending_review(actions: list[dict[str, Any]], message_id: str) -> PendingEmailReview | None:
    candidates = {
        (a["result"]["id"], a["result"]["content_token"])
        for a in actions
        if a["name"] in REVIEW_TOOLS
        and not a["result"].get("error")
        and a["result"].get("status") in {"draft", "failed"}
        and a["result"].get("content_token")
    }
    if len(candidates) != 1:
        return None
    outbox_id, token = candidates.pop()
    return {
        "outbox_id": outbox_id,
        "content_token": token,
        "delivery_request_id": new_id(),
        "message_id": message_id,
    }


def delivery_answer(result: dict[str, Any]) -> tuple[str, str | None]:
    if isinstance(result.get("error"), dict):
        return result["error"]["message"], result["error"]["code"]
    if result["status"] == "sent":
        return (
            "SMTP-сервер принял письмо к отправке. Статус и Message-ID сохранены в исходящих. "
            "Это ещё не подтверждение доставки получателю.",
            None,
        )
    if result["status"] == "failed":
        return (
            "Сервер отклонил отправку письма. "
            + (result.get("error") or "Проверь исходящие.")
            + " Для новой попытки сначала попроси показать актуальный черновик.",
            "delivery_failed",
        )
    return (
        "Неизвестно, принял ли сервер письмо. Проверь получение и статус в исходящих. "
        "Повторная отправка заблокирована.",
        "delivery_unknown",
    )
