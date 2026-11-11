import asyncio
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import format_datetime

from ai_inbox.application.outbox import UNCERTAIN
from ai_inbox.application.outgoing_dto import DeliveryResult, OutgoingMessage
from ai_inbox.domain.errors import ProviderUnavailable


class TrackingSMTP(smtplib.SMTP_SSL):
    def __init__(self, *args, **kwargs):
        self.data_started = False
        self.accepted = False
        super().__init__(*args, **kwargs)

    def data(self, msg):
        self.data_started = True
        code, response = super().data(msg)
        self.accepted = code == 250
        return code, response


def build_message(sender: str, outgoing: OutgoingMessage) -> EmailMessage:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = (
        sender,
        outgoing.recipient,
        outgoing.subject,
    )
    message["Message-ID"], message["Date"] = outgoing.message_id, format_datetime(outgoing.date)
    message.set_content(outgoing.body, cte="quoted-printable")
    for file in outgoing.attachments:
        maintype, subtype = file.content_type.split("/", 1)
        message.add_attachment(file.data, maintype=maintype, subtype=subtype, filename=file.name)
    return message


class SmtpSender:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float):
        self.host, self.port, self.user, self.password, self.timeout = (
            host,
            port,
            user,
            password,
            timeout,
        )
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="smtp")
        self.slots = asyncio.Semaphore(2)

    def _send(self, outgoing: OutgoingMessage) -> DeliveryResult:
        message = build_message(self.user, outgoing)
        connection = None
        try:
            connection = TrackingSMTP(
                self.host, self.port, context=ssl.create_default_context(), timeout=self.timeout
            )
            connection.login(self.user, self.password)
            refused = connection.send_message(
                message, from_addr=self.user, to_addrs=[outgoing.recipient]
            )
            if refused:
                return DeliveryResult("failed", "Почтовый сервер отклонил получателя.")
            return DeliveryResult("sent")
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult(
                "failed", "Не удалось войти на SMTP-сервер. Проверь пароль приложения."
            )
        except smtplib.SMTPRecipientsRefused:
            return DeliveryResult("failed", "Почтовый сервер отклонил адрес получателя.")
        except smtplib.SMTPResponseException as exc:
            # A final negative SMTP reply is definitive; never echo its potentially private text.
            return DeliveryResult(
                "failed", f"Почтовый сервер отклонил отправку (SMTP {exc.smtp_code})."
            )
        except (smtplib.SMTPException, OSError):
            if connection and connection.accepted:
                return DeliveryResult("sent")
            if connection and connection.data_started:
                return DeliveryResult("unknown", UNCERTAIN)
            return DeliveryResult(
                "failed", "Не удалось передать письмо почтовому серверу. Повтори позже."
            )
        finally:
            if connection:
                try:
                    connection.quit()
                except (smtplib.SMTPException, OSError):
                    pass
                finally:
                    connection.close()

    async def send(self, message: OutgoingMessage) -> DeliveryResult:
        # SMTP is never retried automatically, including after a timeout during DATA.
        async with self.slots:
            return await asyncio.get_running_loop().run_in_executor(self.pool, self._send, message)

    async def check_connection(self) -> None:
        def check():
            with smtplib.SMTP_SSL(
                self.host, self.port, context=ssl.create_default_context(), timeout=self.timeout
            ) as conn:
                conn.login(self.user, self.password)
                code, _ = conn.noop()
                if code != 250:
                    raise OSError("SMTP NOOP rejected")

        async with self.slots:
            try:
                await asyncio.get_running_loop().run_in_executor(self.pool, check)
            except (smtplib.SMTPException, OSError) as exc:
                raise ProviderUnavailable(
                    "SMTP недоступен или отклонил вход. Проверь соединение и настройки."
                ) from exc

    async def close(self):
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)
