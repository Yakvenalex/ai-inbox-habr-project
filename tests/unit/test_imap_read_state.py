import pytest

from ai_inbox.domain.errors import Conflict, NotFound, ProviderUnavailable
from ai_inbox.infrastructure.mail.imap import ImapMail, email_id


@pytest.fixture
async def mail(monkeypatch):
    import ai_inbox.infrastructure.mail.imap as module

    class Server:
        validity = "123"
        exists = True
        flags = {b"\\Flagged", b"\\Answered", b"custom"}
        store_error = False
        ignore_store = False
        selects = []
        stores = []
        logouts = 0
        statuses = 0

    server = Server()

    class Connection:
        def __init__(self, *args, **kwargs):
            assert kwargs["ssl_context"].check_hostname

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.logout()

        def login(self, *args):
            return "OK", []

        def logout(self):
            server.logouts += 1

        def close(self):
            raise AssertionError("CLOSE could expunge other messages")

        def select(self, mailbox, readonly):
            assert mailbox == "INBOX"
            server.selects.append(readonly)
            return "OK", [b"1"]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return name, [server.validity.encode()]

        def status(self, mailbox, fields):
            server.statuses += 1
            count = 0 if any(f.lower() == b"\\seen" for f in server.flags) else 1
            return "OK", [f"INBOX (MESSAGES 1 UNSEEN {count})".encode()]

        def uid(self, command, uid, *args):
            if command == "store":
                server.stores.append((uid, args))
                if server.store_error:
                    return "NO", [b"denied"]
                assert args[1] == r"(\Seen)"
                if not server.ignore_store:
                    if args[0] == "+FLAGS.SILENT":
                        server.flags.add(b"\\Seen")
                    elif args[0] == "-FLAGS.SILENT":
                        server.flags = {f for f in server.flags if f.lower() != b"\\seen"}
                    else:
                        raise AssertionError("Must preserve all other flags")
                return "OK", [None]
            if command == "fetch":
                if not server.exists:
                    return "OK", [None]
                metadata = b"1 (UID 42 FLAGS (" + b" ".join(sorted(server.flags)) + b"))"
                if args == ("(RFC822.SIZE)",):
                    return "OK", [b"1 (RFC822.SIZE 80)"]
                if args == ("(UID FLAGS BODY.PEEK[])",):
                    return "OK", [(metadata, b"Subject: Synthetic\r\n\r\nHello")]
                assert args == ("(UID FLAGS)",)
                return "OK", [metadata]
            raise AssertionError(command)

    monkeypatch.setattr(module.imaplib, "IMAP4_SSL", Connection)
    gateway = ImapMail("imap.example.test", 993, "demo", "fake-pass", 1, 20)
    try:
        yield gateway, server
    finally:
        await gateway.close()


async def test_server_flags_roundtrip_and_other_flags_preserved(mail):
    gateway, server = mail
    id = email_id("123", "42")
    original = set(server.flags)
    assert (await gateway.set_unread(id, False)).unread is False
    assert server.flags == original | {b"\\Seen"}
    assert (await gateway.set_unread(id, False)).unread is False
    assert len(server.stores) == 1
    assert (await gateway.set_unread(id, True)).unread is True
    assert server.flags == original
    assert server.selects == [False, False, False] and server.logouts == 3


async def test_default_read_is_readonly_and_counts_do_not_select(mail):
    gateway, server = mail
    assert (await gateway.read(email_id("123", "42"))).unread is True
    assert server.selects == [True] and not server.stores
    assert (await gateway.counts()).unread == 1
    assert server.selects == [True] and server.statuses == 1


async def test_stale_uidvalidity_and_missing_uid_cannot_change_flags(mail):
    gateway, server = mail
    with pytest.raises(NotFound):
        await gateway.set_unread(email_id("122", "42"), False)
    server.exists = False
    with pytest.raises(NotFound):
        await gateway.set_unread(email_id("123", "42"), False)
    assert not server.stores


async def test_server_rejection_and_silent_ignored_store_are_not_success(mail):
    gateway, server = mail
    id = email_id("123", "42")
    server.store_error = True
    with pytest.raises(ProviderUnavailable):
        await gateway.set_unread(id, False)
    server.store_error = False
    server.ignore_store = True
    with pytest.raises(Conflict):
        await gateway.set_unread(id, False)


async def test_system_seen_flag_is_case_insensitive(mail):
    gateway, server = mail
    server.flags = {b"\\SEEN", b"\\Flagged"}
    assert (await gateway.read(email_id("123", "42"))).unread is False
    assert (await gateway.set_unread(email_id("123", "42"), False)).unread is False
    assert not server.stores
