"""Tests for the host-side outbound observation.

The search itself is ordinary IMAP; what these tests pin is the three-state
contract, because MailHub resends on "absent".  A folder that could not be
opened, a server that refused the connection or a missing credential must all
come back as indeterminate, never as "not there".
"""

from __future__ import annotations

import imaplib
from typing import Any

import pytest

from local_host.outbound import (
    credential_fields,
    folder_list,
    observe_outbound_in_mailbox,
)


class FakeImap:
    """Records the search and answers however the test asks it to."""

    def __init__(
        self,
        *,
        hits: dict[str, list[bytes]] | None = None,
        unselectable: tuple[str, ...] = (),
        raises: Exception | None = None,
    ) -> None:
        self.hits = hits or {}
        self.unselectable = unselectable
        self.raises = raises
        self.searched: list[str] = []
        self.selected: list[str] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        del user, password
        if self.raises is not None:
            raise self.raises
        return ("OK", [b"ok"])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        del readonly
        self.selected.append(mailbox)
        if mailbox in self.unselectable:
            return ("NO", [b"nope"])
        return ("OK", [b"0"])

    def uid(self, command: str, *args: object) -> tuple[str, list[bytes]]:
        del args
        assert command == "search"
        self.searched.append(self.selected[-1])
        return ("OK", self.hits.get(self.selected[-1], [b""]))

    def logout(self) -> tuple[str, list[bytes]]:
        return ("BYE", [b"bye"])


def _patch(monkeypatch: pytest.MonkeyPatch, session: FakeImap) -> None:
    def factory(*args: object, **kwargs: object) -> FakeImap:
        del args, kwargs
        return session

    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)


def _observe(folders: tuple[str, ...] = ("INBOX", "Sent")) -> dict[str, Any]:
    return observe_outbound_in_mailbox(
        host="imap.example.test",
        port=993,
        username="user",
        password="secret",
        folders=folders,
        internet_message_id="<mailhub-1@mailhub.invalid>",
    )


def test_reports_a_match_and_stops_searching(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeImap(hits={"Sent": [b"42 43"]})
    _patch(monkeypatch, session)

    observation = _observe()

    assert observation["found"] is True
    assert observation["mailbox"] == "Sent"
    # INBOX is searched first; finding it in Sent means no later folder is read.
    assert session.searched == ["INBOX", "Sent"]


def test_reports_absent_only_after_every_folder_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeImap()
    _patch(monkeypatch, session)

    observation = _observe()

    assert observation["found"] is False
    assert session.searched == ["INBOX", "Sent"]


def test_an_unreadable_folder_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeImap(unselectable=("Sent",))
    _patch(monkeypatch, session)

    observation = _observe()

    # "Not in INBOX, could not look in Sent" is not "not sent".
    assert observation["found"] is None
    assert "Sent" in str(observation["detail"])


def test_a_refused_connection_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeImap(raises=imaplib.IMAP4.error("login refused"))
    _patch(monkeypatch, session)

    assert _observe()["found"] is None


def test_missing_configuration_is_indeterminate_rather_than_absent() -> None:
    observation = observe_outbound_in_mailbox(
        host="",
        port=993,
        username="user",
        password="secret",
        folders=("INBOX",),
        internet_message_id="<mailhub-1@mailhub.invalid>",
    )
    assert observation["found"] is None
    assert observation["detail"] == "host_imap_not_configured"


def test_an_empty_folder_list_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, FakeImap())

    assert _observe(folders=())["found"] is None


def test_folder_list_parses_and_defaults() -> None:
    assert folder_list("INBOX, Sent ,,") == ("INBOX", "Sent")
    assert folder_list("") == ("INBOX",)


def test_credential_fields_rejects_incomplete_mappings() -> None:
    assert credential_fields({"username": "u", "password": "p"}) == ("u", "p")
    assert credential_fields({"username": "u"}) is None
    assert credential_fields({"username": "", "password": "p"}) is None
    assert credential_fields(None) is None
