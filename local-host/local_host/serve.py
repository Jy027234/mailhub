"""Runtime entrypoint for the local MailHub host (B4).

Run with::

    uvicorn local_host.serve:app --host 127.0.0.1 --port 8090

``local_host.app.create_host_app`` stays a pure factory so contract tests can
assemble the application with fixture settings.
"""

from __future__ import annotations

from local_host.app import create_host_app
from local_host.broker import build_broker
from local_host.config import HostSettings
from local_host.stores import LocalStores

settings = HostSettings.from_env()
stores = LocalStores(settings.database_path, settings.encryption_secret)
broker = build_broker(settings, settings.database_path)
app = create_host_app(settings, stores, broker)
