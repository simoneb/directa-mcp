"""Tests for starting dGO.

Nothing here launches anything: `_spawn` is replaced throughout, so a failing
test cannot leave a login window on someone's screen. The clock is injected for
the same reason the launch state is kept in the module — the "starting" window
is the only thing standing between a slow OTP and a second dGO.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from directa_mcp import launcher, server

#: Kept before the stub below replaces the module attribute, for the one test
#: that exercises the real process scan.
real_dgo_processes = launcher.dgo_processes


@pytest.fixture(autouse=True)
def quiet_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts on a machine where this server has launched nothing and
    no Directa software is running — otherwise the suite would read the
    developer's own Darwin and pass or fail with it."""
    launcher._launch = None
    monkeypatch.setattr(launcher, "dgo_processes", lambda configured_path: [])


@pytest.fixture
def fake_dgo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A dGO that exists on disk and records its launch instead of running."""
    path = tmp_path / "dGO.exe"
    path.write_text("")
    monkeypatch.setattr(launcher, "_spawn", lambda p: 4321)
    return path


class TestResolvingTheExecutable:
    def test_a_configured_path_is_used_as_is(self, fake_dgo: Path) -> None:
        assert launcher.resolve_dgo_path(str(fake_dgo)) == fake_dgo

    def test_a_missing_executable_is_reported_not_launched(self, tmp_path: Path) -> None:
        missing = tmp_path / "nowhere" / "dGO.exe"
        with pytest.raises(launcher.LauncherError) as caught:
            launcher.resolve_dgo_path(str(missing))
        assert "DIRECTA_DGO_PATH" in str(caught.value)

    def test_the_default_is_windows_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(launcher.os, "name", "posix")
        assert launcher.default_dgo_path() is None
        with pytest.raises(launcher.LauncherError):
            launcher.resolve_dgo_path(None)


class TestLaunchState:
    def test_nothing_is_claimed_before_a_launch(self) -> None:
        assert launcher.launch_state(now=1000.0) == {
            "launched_here": False,
            "starting": False,
        }

    def test_a_launch_is_remembered_with_its_pid(self, fake_dgo: Path) -> None:
        assert launcher.start_dgo(str(fake_dgo), now=1000.0)["pid"] == 4321
        state = launcher.launch_state(now=1030.0)
        assert state == {
            "launched_here": True,
            "starting": True,
            "pid": 4321,
            "seconds_since_launch": 30.0,
        }

    def test_the_starting_window_expires(self, fake_dgo: Path) -> None:
        launcher.start_dgo(str(fake_dgo), now=1000.0)
        late = 1000.0 + launcher.STARTUP_GRACE_SECONDS + 1
        assert launcher.launch_state(now=late)["starting"] is False


class TestDescribe:
    def test_a_reachable_port_is_all_it_takes(self) -> None:
        assert launcher.describe(True, autostart_enabled=False)["state"] == "running"

    def test_a_recent_launch_reads_as_waiting_for_the_user(self, fake_dgo: Path) -> None:
        launcher.start_dgo(str(fake_dgo), now=1000.0)
        described = launcher.describe(False, autostart_enabled=True, now=1060.0)
        assert described["state"] == "starting"
        assert described["seconds_since_launch"] == 60.0
        assert "OTP" in described["hint"]

    def test_a_stale_launch_reads_as_stopped(self, fake_dgo: Path) -> None:
        launcher.start_dgo(str(fake_dgo), now=1000.0)
        late = 1000.0 + launcher.STARTUP_GRACE_SECONDS + 1
        assert launcher.describe(False, True, now=late)["state"] == "stopped"

    def test_the_hint_does_not_offer_a_tool_the_gate_forbids(self) -> None:
        assert "start_darwin" not in launcher.describe(False, autostart_enabled=False)["hint"]
        assert "start_darwin" in launcher.describe(False, autostart_enabled=True)["hint"]


class TestProcessDetection:
    def test_a_dgo_nobody_told_us_about_is_still_seen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launcher, "dgo_processes", lambda configured_path: [18896])
        described = launcher.describe(False, autostart_enabled=True)
        assert described["state"] == "starting"
        assert described["dgo_pids"] == [18896]
        assert "do not launch dGO" in described["hint"]

    def test_our_own_launch_outranks_the_process_scan(self, fake_dgo: Path) -> None:
        launcher.start_dgo(str(fake_dgo), now=1000.0)
        described = launcher.describe(False, True, str(fake_dgo), now=1010.0)
        assert described["seconds_since_launch"] == 10.0

    def test_an_unresolvable_dgo_is_not_an_error(self, tmp_path: Path) -> None:
        assert real_dgo_processes(str(tmp_path / "gone.exe")) == []


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Run the server's tools against altered settings, as a differently
    configured process would have loaded them."""
    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, **overrides))


def _ports(reachable: bool) -> dict[str, dict[str, object]]:
    return {
        "trading": {"host": "127.0.0.1", "port": 10002, "reachable": reachable},
        "historical": {"host": "127.0.0.1", "port": 10003, "reachable": reachable},
    }


class TestStartDarwinTool:
    def test_the_gate_refuses_before_touching_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch, autostart_enabled=False, dgo_path="/does/not/exist")
        monkeypatch.setattr(
            server, "check_ports", lambda: pytest.fail("the gate must come first")
        )
        result = server.start_darwin()
        assert result["success"] is False
        assert result["blocked"] is True
        assert result["launched"] is False

    def test_a_running_darwin_is_not_launched_again(
        self, monkeypatch: pytest.MonkeyPatch, fake_dgo: Path
    ) -> None:
        _configure(monkeypatch, autostart_enabled=True, dgo_path=str(fake_dgo))
        monkeypatch.setattr(server, "check_ports", lambda: _ports(True))
        result = server.start_darwin()
        assert result["launched"] is False
        assert result["darwin"]["state"] == "running"

    def test_a_login_in_progress_is_not_disturbed(
        self, monkeypatch: pytest.MonkeyPatch, fake_dgo: Path
    ) -> None:
        _configure(monkeypatch, autostart_enabled=True, dgo_path=str(fake_dgo))
        monkeypatch.setattr(server, "check_ports", lambda: _ports(False))
        monkeypatch.setattr(launcher, "_spawn", lambda p: pytest.fail("launched twice"))
        launcher._launch = {"pid": 4321, "at": launcher.time.time()}

        result = server.start_darwin()
        assert result["launched"] is False
        assert result["darwin"]["state"] == "starting"

    def test_a_dgo_the_user_opened_is_not_duplicated(
        self, monkeypatch: pytest.MonkeyPatch, fake_dgo: Path
    ) -> None:
        _configure(monkeypatch, autostart_enabled=True, dgo_path=str(fake_dgo))
        monkeypatch.setattr(server, "check_ports", lambda: _ports(False))
        monkeypatch.setattr(launcher, "dgo_processes", lambda configured_path: [18896])
        monkeypatch.setattr(launcher, "_spawn", lambda p: pytest.fail("launched twice"))

        result = server.start_darwin()
        assert result["launched"] is False
        assert result["darwin"]["dgo_pids"] == [18896]

    def test_a_stopped_darwin_is_launched_and_handed_over(
        self, monkeypatch: pytest.MonkeyPatch, fake_dgo: Path
    ) -> None:
        _configure(monkeypatch, autostart_enabled=True, dgo_path=str(fake_dgo))
        monkeypatch.setattr(server, "check_ports", lambda: _ports(False))

        result = server.start_darwin()
        assert result["success"] is True
        assert result["launched"] is True
        assert result["dgo_path"] == str(fake_dgo)
        assert result["pid"] == 4321
        # Reported as starting rather than started: the OTP is still to come.
        assert result["darwin"]["state"] == "starting"
        assert launcher.launch_state()["pid"] == 4321

    def test_a_missing_dgo_fails_without_pretending_to_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configure(monkeypatch, autostart_enabled=True, dgo_path=str(tmp_path / "gone.exe"))
        monkeypatch.setattr(server, "check_ports", lambda: _ports(False))
        result = server.start_darwin()
        assert result["success"] is False
        assert result["launched"] is False
        assert launcher.launch_state()["launched_here"] is False
