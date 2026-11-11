import logging

import uvicorn
from pydantic import ValidationError

from ai_inbox.bootstrap.app import create_app
from ai_inbox.bootstrap.settings import Settings


def main():
    try:
        settings = Settings()
    except ValidationError as exc:
        # Pydantic's default text includes input_value, which may be a secret.
        messages = [str(e["msg"]) for e in exc.errors(include_input=False)]
        raise SystemExit("Ошибка конфигурации: " + "; ".join(messages)) from None
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "openai", "openai.agents", "agents"):
        logging.getLogger(name).setLevel(logging.WARNING)
    uvicorn.run(
        create_app(settings),
        host=settings.app_host,
        port=settings.app_port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
