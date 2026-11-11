import imaplib

import pytest

from ai_inbox.domain.errors import Conflict, NotFound, ProviderUnavailable
from ai_inbox.infrastructure.mail.imap import ImapMail, email_id


@pytest.fixture
async def mailbox(monkeypatch):
    class Server:
        exists = True
        validity = "123"
        supports_move = True
        folders = [b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"']
        move_result = "OK"
        list_result = "OK"
        ignore_move = False
        disconnect = False
        search_response = None
        moves = []
        selects = []

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
            assert mailbox == "INBOX"
            server.selects.append(readonly)
            return "OK", [b"2"]

        def response(self, key):
            assert key == "UIDVALIDITY"
            return key, [server.validity.encode()]

        def capability(self):
            return "OK", [b"IMAP4rev1 UIDPLUS MOVE" if server.supports_move else b"IMAP4rev1"]

        def list(self):
            return server.list_result, server.folders

        def uid(self, command, uid, *args):
            if command == "search":
                assert uid is None and args == ("UID", "42")
                if server.search_response is not None:
                    return server.search_response
                return "OK", [b"42" if server.exists else b""]
            assert uid == "42"  # No ranges, wildcards or unrelated messages.
            assert command == "move"  # COPY, STORE, EXPUNGE are forbidden here.
            server.moves.append((uid, args[0]))
            if server.disconnect:
                server.exists = False
                raise imaplib.IMAP4.abort("private provider response")
            if server.move_result == "OK" and not server.ignore_move:
                server.exists = False
            return server.move_result, [None]

        def close(self):
            raise AssertionError("Must not CLOSE/expunge unrelated messages")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Connection)
    gateway = ImapMail("imap.example.test", 993, "demo", "fake", 1, 20)
    try:
        yield gateway, server
    finally:
        await gateway.close()


async def test_delete_moves_only_uid_to_special_use_trash_and_confirms_absence(mailbox):
    gateway, server = mailbox
    id = email_id("123", "42")
    result = await gateway.delete(id)
    assert result.deleted and not result.already_absent
    assert server.moves == [("42", b'"Deleted Messages"')]
    assert server.selects == [False]
    # Retrying after a successful MOVE cannot create another copy or target a new UID.
    assert (await gateway.delete(id)).already_absent
    assert len(server.moves) == 1


async def test_changed_uidvalidity_never_moves_another_message(mailbox):
    gateway, server = mailbox
    with pytest.raises(NotFound):
        await gateway.delete(email_id("122", "42"))
    assert not server.moves


@pytest.mark.parametrize(
    "folders",
    [
        [],
        [b'(\\Trash) "/" INBOX'],
        [b'(\\Trash \\Noselect) "/" Trash'],
        [b'(\\Trash) "/" Trash', b'(\\Trash) "/" Other'],
        [b'(\\Trash) "/" "bad\r\nname"'],
        [b'(\\Trash) "/" "unterminated'],
    ],
)
async def test_missing_ambiguous_or_invalid_trash_cannot_move(mailbox, folders):
    gateway, server = mailbox
    server.folders = folders
    with pytest.raises(Conflict):
        await gateway.delete(email_id("123", "42"))
    assert not server.moves and server.exists


@pytest.mark.parametrize(
    "folder,expected",
    [
        (b'(\\trash) "/" &BBoEPgRABDcEOAQ9BDA-', b'"&BBoEPgRABDcEOAQ9BDA-"'),
        (b'(\\Trash) "/" "a\\"b\\\\c"', b'"a\\"b\\\\c"'),
        ((b'(\\Trash) "/" {8}', b"Trash Me"), b'"Trash Me"'),
    ],
)
async def test_server_mailbox_encoding_quoting_and_literals(mailbox, folder, expected):
    gateway, server = mailbox
    server.folders = [folder]
    await gateway.delete(email_id("123", "42"))
    assert server.moves == [("42", expected)]


async def test_unsupported_move_has_no_unsafe_fallback(mailbox):
    gateway, server = mailbox
    server.supports_move = False
    with pytest.raises(Conflict):
        await gateway.delete(email_id("123", "42"))
    assert not server.moves and server.exists


async def test_rejected_or_ignored_move_is_not_reported_success(mailbox):
    gateway, server = mailbox
    server.move_result = "NO"
    with pytest.raises(ProviderUnavailable):
        await gateway.delete(email_id("123", "42"))
    server.move_result = "OK"
    server.ignore_move = True
    with pytest.raises(Conflict):
        await gateway.delete(email_id("123", "42"))
    assert server.exists


async def test_disconnect_after_move_is_uncertain_and_explicit_retry_observes_absence(mailbox):
    gateway, server = mailbox
    server.disconnect = True
    with pytest.raises(ProviderUnavailable, match="проверь корзину") as error:
        await gateway.delete(email_id("123", "42"))
    assert "private provider response" not in str(error.value)
    assert (await gateway.delete(email_id("123", "42"))).already_absent
    assert len(server.moves) == 1


async def test_list_error_cannot_move(mailbox):
    gateway, server = mailbox
    server.list_result = "NO"
    with pytest.raises(ProviderUnavailable):
        await gateway.delete(email_id("123", "42"))
    assert not server.moves


@pytest.mark.parametrize(
    "response", [("NO", [b""]), ("OK", [None]), ("OK", [b"43"]), ("OK", [b"invalid"])]
)
async def test_invalid_uid_search_does_not_claim_absence_or_move(mailbox, response):
    gateway, server = mailbox
    server.search_response = response
    with pytest.raises(ProviderUnavailable):
        await gateway.delete(email_id("123", "42"))
    assert not server.moves and server.exists
