import pytest

from ai_inbox.application.chat_outgoing import pending_review


def preview(id="draft-one", token="version-one", name="prepare_outgoing_email", **changes):
    return {
        "name": name,
        "result": {"id": id, "content_token": token, "status": "draft", **changes},
    }


def test_previous_preview_is_bound_to_one_exact_snapshot_and_message():
    review = pending_review([preview()], "preview-message")
    assert review is not None
    assert review["outbox_id"] == "draft-one"
    assert review["content_token"] == "version-one"
    assert review["message_id"] == "preview-message"
    assert review["delivery_request_id"]


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [preview(name="send_outgoing_email")],
        [preview(status="sent")],
        [preview(error={"code": "forbidden"})],
        [preview(content_token=None)],
        [preview(), preview(id="draft-two")],
        [preview(), preview(token="version-two")],
    ],
)
def test_missing_failed_or_ambiguous_previews_cannot_grant_snapshot(actions):
    assert pending_review(actions, "message") is None


def test_repeated_preview_of_same_version_is_one_candidate():
    review = pending_review([preview(), preview(name="review_outgoing_email")], "message")
    assert review and review["outbox_id"] == "draft-one"
