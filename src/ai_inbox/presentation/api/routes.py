from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordRequestForm

from .dependencies import Owner, container
from .schemas import (
    AttachmentResponse,
    ChatInput,
    ChatResponse,
    ComposeEmailInput,
    ComposeEmailResponse,
    ConversationCreate,
    ConversationResponse,
    DeliveryResponse,
    DigestResponse,
    DraftCreate,
    DraftPatch,
    DraftResponse,
    EmailReadBatchInput,
    EmailReadBatchResponse,
    EmailReadInput,
    EmailReadResponse,
    EmailResponse,
    ErrorResponse,
    MailboxCountsResponse,
    MediaResponse,
    MessageResponse,
    OutgoingInput,
    OutgoingResponse,
    OutgoingSummary,
    OwnerResponse,
    SendEmailInput,
    SpeechInput,
    SummarizeRequest,
    SummaryResponse,
    TaskCreate,
    TaskPatch,
    TaskResponse,
    TokenResponse,
    TranscriptionResponse,
)
from .uploads import image_upload, limited_upload

router = APIRouter(
    prefix="/api/v1",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)


@router.post(
    "/auth/token",
    response_model=TokenResponse,
    tags=["Авторизация"],
    summary="Получить Bearer JWT по логину и паролю",
)
async def token(
    request: Request, response: Response, form: Annotated[OAuth2PasswordRequestForm, Depends()]
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return container(request).authenticate(form.username, form.password)


@router.get(
    "/auth/me",
    response_model=OwnerResponse,
    tags=["Авторизация"],
    summary="Публичные данные владельца",
)
async def me(owner: Owner):
    return {"username": owner["sub"]}


@router.get("/tasks", response_model=list[TaskResponse], tags=["Задачи"], summary="Список задач")
async def tasks(
    request: Request,
    owner: Owner,
    status: str | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
):
    return await container(request).tasks.list(status, due_before, due_after)


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=201,
    tags=["Задачи"],
    summary="Создать задачу с точным сроком или без срока",
)
async def create_task(data: TaskCreate, request: Request, owner: Owner):
    return await container(request).tasks.create(**data.model_dump())


@router.get("/tasks/{id}", response_model=TaskResponse, tags=["Задачи"], summary="Прочитать задачу")
async def task(id: str, request: Request, owner: Owner):
    return await container(request).tasks.get(id)


@router.patch(
    "/tasks/{id}",
    response_model=TaskResponse,
    tags=["Задачи"],
    summary="Изменить или завершить задачу",
)
async def edit_task(id: str, data: TaskPatch, request: Request, owner: Owner):
    return await container(request).tasks.edit(id, data.model_dump(exclude_unset=True))


# The remaining routes stay thin: all state changes live in application use cases.


@router.get(
    "/emails",
    response_model=list[EmailResponse],
    tags=["Почта"],
    summary="Последние письма без изменения флага прочтения",
)
async def emails(
    request: Request,
    owner: Owner,
    unread: bool = True,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    return await container(request).mail.list_emails(unread, limit)


@router.post(
    "/emails/summarize",
    response_model=SummaryResponse,
    tags=["Почта"],
    summary="Сводка выбранных писем или последних непрочитанных",
)
async def summarize(data: SummarizeRequest, request: Request, owner: Owner):
    return await container(request).mail.summarize(data.ids, data.limit)


@router.get(
    "/emails/status",
    response_model=MailboxCountsResponse,
    tags=["Почта"],
    summary="Число всех и непрочитанных писем в INBOX",
)
async def mailbox_counts(request: Request, owner: Owner):
    return await container(request).mail.counts()


@router.post(
    "/emails/read-state",
    response_model=EmailReadBatchResponse,
    tags=["Почта"],
    summary="Прочитано/непрочитано для выбранных писем; результат каждого отдельно",
)
async def email_read_states(data: EmailReadBatchInput, request: Request, owner: Owner):
    return await container(request).mail.set_many_unread(data.ids, data.unread)


@router.post(
    "/emails/{id}/read-state",
    response_model=EmailReadResponse,
    tags=["Почта"],
    summary="Сохранить прочитанность письма на почтовом сервере",
)
async def email_read_state(id: str, data: EmailReadInput, request: Request, owner: Owner):
    return await container(request).mail.set_unread(id, data.unread)


@router.get(
    "/emails/{id}",
    response_model=EmailResponse,
    tags=["Почта"],
    summary="Прочитать текст без изменения флага; для отметки используется POST read-state",
)
async def email(id: str, request: Request, owner: Owner):
    return await container(request).mail.read(id)


@router.get(
    "/drafts", response_model=list[DraftResponse], tags=["Черновики"], summary="Локальные черновики"
)
async def drafts(request: Request, owner: Owner):
    return await container(request).drafts.list()


@router.post(
    "/drafts",
    response_model=DraftResponse,
    status_code=201,
    tags=["Черновики"],
    summary="Сохранить черновик без отправки и без записи в IMAP Drafts",
)
async def create_draft(data: DraftCreate, request: Request, owner: Owner):
    return await container(request).drafts.create(**data.model_dump())


@router.get(
    "/drafts/{id}", response_model=DraftResponse, tags=["Черновики"], summary="Прочитать черновик"
)
async def draft(id: str, request: Request, owner: Owner):
    return await container(request).drafts.get(id)


@router.patch(
    "/drafts/{id}",
    response_model=DraftResponse,
    tags=["Черновики"],
    summary="Отредактировать черновик",
)
async def edit_draft(id: str, data: DraftPatch, request: Request, owner: Owner):
    return await container(request).drafts.edit(id, data.model_dump(exclude_unset=True))


@router.get(
    "/conversations",
    response_model=list[ConversationResponse],
    tags=["Диалоги"],
    summary="Сохранённые диалоги",
)
async def conversations(request: Request, owner: Owner):
    return await container(request).chat.list_conversations()


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=201,
    tags=["Диалоги"],
    summary="Начать диалог",
)
async def create_conversation(data: ConversationCreate, request: Request, owner: Owner):
    return await container(request).chat.create_conversation(data.title)


@router.get(
    "/conversations/{id}/messages",
    response_model=list[MessageResponse],
    tags=["Диалоги"],
    summary="История и реальные результаты действий",
)
async def messages(id: str, request: Request, owner: Owner):
    return await container(request).chat.messages(id)


@router.post(
    "/conversations/{id}/messages",
    response_model=ChatResponse,
    tags=["Диалоги"],
    summary="Ответ агента и квитанции инструментов; request_id защищает от повторов",
)
async def chat(id: str, data: ChatInput, request: Request, owner: Owner):
    return await container(request).chat(
        id, data.text, str(data.request_id), str(data.image_id) if data.image_id else None
    )


@router.post(
    "/files/images",
    response_model=MediaResponse,
    status_code=201,
    tags=["Файлы"],
    summary="Загрузить JPEG/PNG для vision",
)
async def image(file: UploadFile, request: Request, owner: Owner):
    data, content_type = await image_upload(file, request)
    return await container(request).media.save_image(data, content_type)


@router.post(
    "/speech/transcriptions",
    response_model=TranscriptionResponse,
    tags=["Речь"],
    summary="Расшифровать аудио, не выполняя команду",
)
async def transcribe(file: UploadFile, request: Request, owner: Owner):
    content_type = file.content_type or "application/octet-stream"
    data = await limited_upload(file, request)
    return await container(request).media.transcribe(data, content_type)


@router.post(
    "/speech/synthesis",
    response_model=MediaResponse,
    status_code=201,
    tags=["Речь"],
    summary="Озвучить текст и сохранить MP3",
)
async def synthesize(data: SpeechInput, request: Request, owner: Owner):
    return await container(request).media.synthesize(data.text)


@router.get(
    "/media/{id}",
    response_class=FileResponse,
    tags=["Файлы"],
    summary="Защищённое медиа или скачивание вложения по ID",
    responses={
        200: {
            "content": {
                "audio/mpeg": {},
                "image/png": {},
                "image/jpeg": {},
                "application/octet-stream": {},
            }
        }
    },
)
async def media(id: str, request: Request, owner: Owner):
    c = container(request)
    item = await c.media.get(id)
    return FileResponse(
        c.storage.path(item["filename"]),
        media_type=item["content_type"],
        filename=item["original_name"] or item["filename"],
        content_disposition_type="attachment" if item["kind"] == "attachment" else "inline",
    )


@router.get(
    "/digests",
    response_model=list[DigestResponse],
    tags=["Дайджесты"],
    summary="История дайджестов",
)
async def digests(request: Request, owner: Owner):
    return await container(request).digests.list()


@router.post(
    "/digests",
    response_model=DigestResponse,
    status_code=201,
    tags=["Дайджесты"],
    summary="Сформировать ручной дайджест",
)
async def create_digest(request: Request, owner: Owner):
    return await container(request).digests()


@router.get(
    "/digests/{id}",
    response_model=DigestResponse,
    tags=["Дайджесты"],
    summary="Текст, источники и состояние дайджеста",
)
async def digest(id: str, request: Request, owner: Owner):
    return await container(request).digests.get(id)


@router.post(
    "/digests/{id}/audio",
    response_model=MediaResponse,
    tags=["Дайджесты"],
    summary="Озвучить сохранённый дайджест",
)
async def digest_audio(id: str, request: Request, owner: Owner):
    return await container(request).media.digest_audio(id)


@router.post(
    "/files/attachments",
    response_model=AttachmentResponse,
    status_code=201,
    tags=["Исходящие"],
    summary="Загрузить файл для отправки, до 15 МБ",
)
async def upload_attachment(file: UploadFile, request: Request, owner: Owner):
    name, content_type = file.filename or "file", file.content_type or "application/octet-stream"
    data = await limited_upload(file, request)
    return await container(request).media.save_attachment(data, name, content_type)


@router.post(
    "/outbox/compose",
    response_model=ComposeEmailResponse,
    tags=["Исходящие"],
    summary="ИИ готовит тему и текст по наброску; письмо не отправляется",
)
async def compose_email(data: ComposeEmailInput, request: Request, owner: Owner):
    return await container(request).outbox.compose(data.outline)


@router.get(
    "/outbox",
    response_model=list[OutgoingSummary],
    tags=["Исходящие"],
    summary="Исходящие черновики и результаты отправок",
)
async def outgoing_emails(request: Request, owner: Owner):
    return await container(request).outbox.list()


@router.post(
    "/outbox",
    response_model=OutgoingResponse,
    status_code=201,
    tags=["Исходящие"],
    summary="Сохранить исходящий черновик без отправки",
)
async def create_outgoing(data: OutgoingInput, request: Request, owner: Owner):
    return await container(request).outbox.save(
        data.recipient, data.subject, data.body, [str(id) for id in data.attachment_ids]
    )


@router.get(
    "/outbox/{id}",
    response_model=OutgoingResponse,
    tags=["Исходящие"],
    summary="Письмо, вложения и состояние отправки",
)
async def outgoing_email(id: str, request: Request, owner: Owner):
    return await container(request).outbox.get(id)


@router.patch(
    "/outbox/{id}",
    response_model=OutgoingResponse,
    tags=["Исходящие"],
    summary="Сохранить полный редактор исходящего черновика",
)
async def edit_outgoing(id: str, data: OutgoingInput, request: Request, owner: Owner):
    return await container(request).outbox.save(
        data.recipient, data.subject, data.body, [str(id) for id in data.attachment_ids], id=id
    )


@router.post(
    "/outbox/{id}/send",
    response_model=DeliveryResponse,
    tags=["Исходящие"],
    summary="Подтверждённая отправка через SMTP без автоматического повтора",
)
async def send_email(id: str, data: SendEmailInput, request: Request, owner: Owner):
    return await container(request).outbox.send(
        id, str(data.request_id), data.confirmed, data.content_token
    )
