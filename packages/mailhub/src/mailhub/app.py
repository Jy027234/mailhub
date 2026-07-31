"""Environment-selecting ASGI entrypoint for MailHub."""

from mailhub.api import create_app
from mailhub.config import MailHubSettings
from mailhub.runtime import create_durable_app

settings = MailHubSettings.from_env()
app = (
    create_durable_app(settings)
    if settings.environment.casefold() in {"production", "prod", "staging"}
    else create_app(runtime_settings=settings)
)
