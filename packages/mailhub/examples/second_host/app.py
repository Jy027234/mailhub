"""Minimal second-host portability example; no CAPlatform imports."""

from mailhub.api import create_app
from mailhub.hosts.second_host import build_second_host

_service, _connector, _host_actions, _knowledge = build_second_host()

app = create_app(_service)
