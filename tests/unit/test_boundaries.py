import ast
import base64
from datetime import UTC, datetime
from email import message_from_bytes, policy
from pathlib import Path

import pytest

from ai_inbox.application.speech_text import prepare_speech
from ai_inbox.domain.entities import Task
from ai_inbox.domain.errors import InvalidInput, NotFound
from ai_inbox.infrastructure.mail.imap import body_text, decode_id, email_id


def test_domain_and_application_dependency_direction():
    root = Path(__file__).resolve().parents[2] / "src/ai_inbox"
    for layer, forbidden in [
        (
            "domain",
            {
                "sqlalchemy",
                "fastapi",
                "openai",
                "agents",
                "pydantic",
                "imaplib",
                "infrastructure",
                "application",
                "presentation",
                "bootstrap",
            },
        ),
        (
            "application",
            {
                "sqlalchemy",
                "fastapi",
                "openai",
                "agents",
                "imaplib",
                "infrastructure",
                "presentation",
                "bootstrap",
            },
        ),
    ]:
        for path in (root / layer).glob("*.py"):
            tree = ast.parse(path.read_text())
            for item in ast.walk(tree):
                names = []
                if isinstance(item, ast.Import):
                    names = [n.name for n in item.names]
                if isinstance(item, ast.ImportFrom):
                    names = [item.module or ""]
                for name in names:
                    assert not set(name.split(".")) & forbidden, (path, name)


def test_task_aware_time_and_completion():
    with pytest.raises(InvalidInput):
        Task(title="Проверить", due_at=datetime(2026, 11, 19))
    with pytest.raises(InvalidInput):
        Task(title="  ")
    task = Task(title="Проверить")
    task.complete(datetime(2026, 11, 11, tzinfo=UTC))
    assert task.status == "completed" and task.updated_at.tzinfo is UTC


def test_mail_identity_and_html_no_external_resources():
    assert decode_id(email_id("123", "42")) == ("123", "42")
    assert email_id("123", "42") != email_id("124", "42")
    with pytest.raises(NotFound):
        decode_id(base64.urlsafe_b64encode(b'["Other", "123", "42"]').decode())
    msg = message_from_bytes(
        b'Content-Type: text/html; charset=utf-8\r\n\r\n<p>Hello</p><script>evil()</script><img src="https://invalid.test/tracker"><style>hidden</style><p>World</p>',
        policy=policy.default,
    )
    text = body_text(msg)
    assert "Hello" in text and "World" in text
    assert "evil" not in text and "tracker" not in text and "hidden" not in text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("89 406,75 руб.", "восемьдесят девять тысяч четыреста шесть рублей семьдесят пять копеек"),
        ("1,01 ₽", "один рубль одна копейка"),
        ("0,50 руб.", "ноль рублей пятьдесят копеек"),
        ("19.11.2026", "девятнадцатого ноября две тысячи двадцать шестого года"),
        ("09:20", "девять часов двадцать минут"),
        ("11:11", "одиннадцать часов одиннадцать минут"),
    ],
)
def test_speech_preserves_amount_and_time(value, expected):
    assert expected in prepare_speech(value)
