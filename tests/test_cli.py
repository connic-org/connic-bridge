import runpy
import sys

import pytest

from connic_bridge import cli


def test_cli_combines_allowed_hosts_from_environment_and_repeated_flags(monkeypatch, capsys):
    calls = []

    async def fake_run_agent(relay_url, token, allowed_hosts):
        calls.append((relay_url, token, allowed_hosts))

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    monkeypatch.setenv("BRIDGE_TOKEN", "cbr_env_token")
    monkeypatch.setenv("RELAY_URL", "wss://relay.internal.example")
    monkeypatch.setenv("ALLOWED_HOSTS", "kafka:9092, postgres:5432,")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "connic-bridge",
            "--allow",
            "redis:6379, kafka:9092,",
            "--allow",
            "my-db:5432",
            "--log-level",
            "DEBUG",
        ],
    )

    cli.main()

    assert calls == [
        (
            "wss://relay.internal.example",
            "cbr_env_token",
            {"kafka:9092", "postgres:5432", "redis:6379", "my-db:5432"},
        )
    ]
    output = capsys.readouterr().out
    assert "Connic Bridge starting..." in output
    assert "Relay:         wss://relay.internal.example" in output
    assert "Allowed hosts: kafka:9092, my-db:5432, postgres:5432, redis:6379" in output


def test_cli_exits_before_starting_when_token_is_missing(monkeypatch, capsys):
    async def fail_if_called(relay_url, token, allowed_hosts):
        raise AssertionError("run_agent should not be called without a bridge token")

    monkeypatch.setattr(cli, "run_agent", fail_if_called)
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(sys, "argv", ["connic-bridge", "--allow", "kafka:9092"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert capsys.readouterr().out == "Error: Bridge token is required. Use --token or set BRIDGE_TOKEN env var.\n"


def test_cli_exits_before_starting_with_unencrypted_relay_url(monkeypatch, capsys):
    async def fail_if_called(relay_url, token, allowed_hosts):
        raise AssertionError("run_agent should not be called with an insecure relay URL")

    monkeypatch.setattr(cli, "run_agent", fail_if_called)
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "connic-bridge",
            "--token",
            "cbr_cli_token",
            "--relay-url",
            "ws://relay.example",
            "--allow",
            "postgres:5432",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert capsys.readouterr().out == "Error: Relay URL must use wss:// to protect bridge traffic.\n"


def test_cli_starts_in_unrestricted_mode_when_no_allowed_hosts_are_configured(monkeypatch, capsys):
    calls = []

    async def fake_run_agent(relay_url, token, allowed_hosts):
        calls.append((relay_url, token, allowed_hosts))

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(sys, "argv", ["connic-bridge", "--token", "cbr_cli_token"])

    cli.main()

    assert calls == [(cli.DEFAULT_RELAY_URL, "cbr_cli_token", set())]
    output = capsys.readouterr().out
    assert "Warning: No allowed hosts specified. The bridge can reach any host available from this network." in output
    assert "Use --allow host:port or set ALLOWED_HOSTS to restrict access." in output
    assert f"Relay:         {cli.DEFAULT_RELAY_URL}" in output
    assert "Allowed hosts: (unrestricted)" in output


def test_cli_run_as_main_executes_module_entrypoint(monkeypatch, capsys):
    """python -m connic_bridge.cli runs the same path as the console_script (argv must be isolated)."""
    import connic_bridge.agent as agent_pkg

    ran = []

    async def fake_run_agent(relay_url, token, allowed_hosts):
        ran.append((relay_url, token, allowed_hosts))

    monkeypatch.setattr(agent_pkg, "run_agent", fake_run_agent)
    monkeypatch.setenv("BRIDGE_TOKEN", "")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "__main__.py",
            "--token",
            "cbr_from_main",
            "--allow",
            "db.internal:5432",
        ],
    )

    sys.modules.pop("connic_bridge.cli", None)
    runpy.run_module("connic_bridge.cli", run_name="__main__")

    out = capsys.readouterr().out
    assert "Connic Bridge starting..." in out
    assert ran == [(cli.DEFAULT_RELAY_URL, "cbr_from_main", {"db.internal:5432"})]
