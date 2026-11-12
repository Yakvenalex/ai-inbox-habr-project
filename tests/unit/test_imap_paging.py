import imaplib

import pytest

from ai_inbox.domain.errors import Conflict, InvalidInput, NotFound, ProviderUnavailable
from ai_inbox.infrastructure.mail.imap import ImapMail, email_id


@pytest.fixture
async def mailbox(monkeypatch):
    class Server:
        ids = [b"9", b"3", b"12", b"5", b"1"]
        validity = "123"
        queries = []
        fetched = []
        vanished = set()
        search_status = "OK"

    server = Server()

    class Connection:
        def __init__(self, *args, **kwargs):
            assert kwargs["ssl_context"].check_hostname

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, *args):
            return "OK", []

        def logout(self):
            pass

        def select(self, mailbox, readonly):
            assert mailbox == "INBOX" and readonly is True
            return "OK", [b"5"]

        def response(self, key):
            return key, [server.validity.encode()]

        def uid(self, command, *args):
            if command == "search":
                server.queries.append(args)
                return server.search_status, [b" ".join(server.ids)]
            assert command == "fetch"
            uid, fields = args
            assert fields == "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            server.fetched.append(uid)
            if uid in server.vanished:
                return "OK", [None]
            return "OK", [
                (
                    b"1 (UID " + uid + b" FLAGS ())",
                    b"From: demo@example.test\r\nSubject: Synthetic\r\n\r\n",
                )
            ]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Connection)
    gateway = ImapMail("imap.example.test", 993, "demo", "fake", 1, 20)
    try:
        yield gateway, server
    finally:
        await gateway.close()


async def test_numeric_uid_order_bounded_fetch_and_cursor_survives_arrival_and_deletion(mailbox):
    gateway, server = mailbox
    first = await gateway.page(False, 2)
    assert [e.uid for e in first.items] == ["12", "9"]
    assert first.next_cursor == email_id("123", "9")
    assert server.fetched == [b"12", b"9"]
    server.ids = [b"99", b"12", b"5", b"3", b"1"]  # New arrival and removed cursor UID.
    second = await gateway.page(False, 2, first.next_cursor)
    assert [e.uid for e in second.items] == ["5", "3"]
    assert server.queries[-1] == (None, "ALL", "UID", "1:8")
    third = await gateway.page(False, 2, second.next_cursor)
    assert [e.uid for e in third.items] == ["1"] and third.next_cursor is None
    assert [e.uid for e in await gateway.list_emails(False, 2)] == ["99", "12"]


async def test_unread_criteria_and_no_zero_uid_range(mailbox):
    gateway, server = mailbox
    await gateway.page(True, 2, email_id("123", "3"))
    assert server.queries == [(None, "UNSEEN", "UID", "1:2")]
    assert not (await gateway.page(True, 2, email_id("123", "1"))).items
    assert len(server.queries) == 1


async def test_uidvalidity_and_bad_cursor_are_rejected_before_fetch(mailbox):
    gateway, server = mailbox
    with pytest.raises(Conflict):
        await gateway.page(False, 2, email_id("122", "9"))
    with pytest.raises(NotFound):
        await gateway.page(False, 2, "invalid cursor")
    assert not server.fetched and not server.queries


async def test_disappeared_headers_still_advance_cursor(mailbox):
    gateway, server = mailbox
    server.vanished = {b"12", b"9"}
    page = await gateway.page(False, 2)
    assert page.items == [] and page.next_cursor == email_id("123", "9")
    assert [e.uid for e in (await gateway.page(False, 2, page.next_cursor)).items] == ["5", "3"]


async def test_search_failure_and_invalid_uids_are_not_an_empty_success(mailbox):
    gateway, server = mailbox
    server.search_status = "NO"
    with pytest.raises(ProviderUnavailable):
        await gateway.page(False, 2)
    server.search_status = "OK"
    server.ids = [b"12", b"bad"]
    with pytest.raises(ProviderUnavailable):
        await gateway.page(False, 2)
    assert not server.fetched


async def test_page_limit(mailbox):
    gateway, server = mailbox
    for limit in (0, 21):
        with pytest.raises(InvalidInput):
            await gateway.page(True, limit)
    assert not server.queries
