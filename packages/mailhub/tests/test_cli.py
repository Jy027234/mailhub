import pytest

from mailhub import cli


def test_standalone_cli_uses_local_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(application: str, **kwargs: object) -> None:
        calls.append({"application": application, **kwargs})

    monkeypatch.setattr("mailhub.cli.uvicorn.run", fake_run)
    cli.main([])

    assert calls == [
        {
            "application": "mailhub.app:app",
            "host": "127.0.0.1",
            "port": 8000,
            "log_level": "info",
            "proxy_headers": False,
            "forwarded_allow_ips": "",
        }
    ]


def test_standalone_cli_rejects_invalid_port() -> None:
    try:
        cli.main(["--port", "0"])
    except SystemExit as exc:
        assert str(exc) == "mailhub_port_out_of_range"
    else:
        raise AssertionError("invalid_port_accepted")
