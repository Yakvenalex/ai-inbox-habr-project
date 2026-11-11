import smtplib
from datetime import UTC, datetime

import pytest

from ai_inbox.application.outgoing_dto import EmailAttachment, OutgoingMessage
from ai_inbox.infrastructure.mail.smtp import SmtpSender, TrackingSMTP, build_message


def outgoing():
    return OutgoingMessage(
        "recipient@example.test",
        "Тестовая тема",
        "Текст письма",
        "<synthetic@example.test>",
        datetime(2026, 11, 11, tzinfo=UTC),
        [EmailAttachment("счёт.pdf", "application/pdf", b"%PDF-test")],
    )


def test_real_mime_message_contains_original_attachment_and_plain_body():
    message = build_message("sender@example.test", outgoing())
    assert str(message["Subject"]) == "Тестовая тема"
    assert message["Message-ID"] == "<synthetic@example.test>"
    assert message.get_body().get_content().strip() == "Текст письма"
    attachment = list(message.iter_attachments())[0]
    assert (
        attachment.get_filename() == "счёт.pdf"
        and attachment.get_payload(decode=True) == b"%PDF-test"
    )


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("none", "sent"),
        ("before_data", "failed"),
        ("during_data", "unknown"),
        ("rejected", "failed"),
        ("quit", "sent"),
    ],
)
async def test_smtp_delivery_classification_and_no_retries(monkeypatch, failure, expected):
    import ai_inbox.infrastructure.mail.smtp as module

    connections = []

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.data_started = False
            self.accepted = False
            self.calls = 0
            connections.append(self)
            assert kwargs["context"].check_hostname

        def login(self, *args):
            pass

        def send_message(self, *args, **kwargs):
            self.calls += 1
            if failure == "before_data":
                raise smtplib.SMTPServerDisconnected("before")
            self.data_started = True
            if failure == "during_data":
                raise smtplib.SMTPServerDisconnected("uncertain")
            if failure == "rejected":
                raise smtplib.SMTPDataError(550, b"private server details")
            self.accepted = True
            return {}

        def quit(self):
            if failure == "quit":
                raise smtplib.SMTPServerDisconnected("quit")

        def close(self):
            pass

    monkeypatch.setattr(module, "TrackingSMTP", FakeConnection)
    sender = SmtpSender("smtp.example.test", 465, "sender@example.test", "fake-pass", 1)
    try:
        result = await sender.send(outgoing())
        assert result.status == expected
        assert len(connections) == 1 and connections[0].calls == 1
        assert "private" not in (result.error or "")
    finally:
        await sender.close()


def test_tracking_data_marks_acceptance_only_after_250(monkeypatch):
    connection = TrackingSMTP.__new__(TrackingSMTP)
    monkeypatch.setattr(smtplib.SMTP_SSL, "data", lambda self, msg: (250, b"ok"))
    connection.data_started = False
    connection.accepted = False
    assert connection.data(b"message")[0] == 250
    assert connection.data_started and connection.accepted


def test_attachment_name_rejects_header_injection():
    from ai_inbox.domain.errors import InvalidInput
    from ai_inbox.domain.outgoing import attachment_name

    with pytest.raises(InvalidInput):
        attachment_name("invoice\r\nBcc:other@example.test", 100)
    assert attachment_name("../../счёт.pdf", 100) == "счёт.pdf"
