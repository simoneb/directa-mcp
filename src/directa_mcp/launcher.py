"""Starting Darwin — as far as it can be automated, which is not all the way.

Darwin has no installed executable of its own. dGO downloads it to
`~/.directa/tmp/darwin.jar` and runs it as

    java -cp darwin.jar;... directa.ui.Darwin www1.directatrading.com <TOKEN> ...

where `<TOKEN>` is a session ticket minted at login and different at every
launch, so the command cannot be replayed. The only usable entry point is dGO
itself, which is why this module launches dGO and nothing else.

Left alone, dGO stops at its tile grid waiting for a click. It goes straight to
Darwin only if the account has *AutoSelezione* set to "Darwin 2" in dGO's own
preferences (`preferredAction` in its settings JSON, written by the Preferenze
page). That is the user's setting to make, once, and this module deliberately
does not write it: nothing here touches a file, a registry key, or a process it
did not start.

Even with autologin and AutoSelezione, Darwin asks for an OTP. So this is
start-and-hand-over, not unattended startup: `start_dgo` returns as soon as dGO
is running and the caller polls the ports afterwards. Nothing ever stops
Darwin — closing a platform that may be holding working orders is not a
decision to automate.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

#: How long after a launch Darwin is still described as "starting" while its
#: ports stay shut. It has to cover the JVM boot plus a human finding the OTP,
#: hence minutes rather than seconds. It is also the window in which a second
#: launch is refused: Directa allows one session at a time, so a duplicate dGO
#: risks knocking over the login that is already in progress.
STARTUP_GRACE_SECONDS = 300.0

#: Set by start_dgo, read by launch_state.
_launch: dict[str, Any] | None = None


class LauncherError(RuntimeError):
    """dGO could not be started. Nothing was launched."""


def default_dgo_path() -> Path | None:
    """Where dGO's installer puts it. Windows only — on any other platform the
    path has to be configured explicitly, since the layout differs."""
    if os.name != "nt":
        return None
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "dGO" / "dGO.exe" if local else None


def resolve_dgo_path(configured: str | None) -> Path:
    """The dGO executable to launch, or an explanation of why there is none."""
    path = Path(configured) if configured else default_dgo_path()
    if path is None:
        raise LauncherError(
            "No dGO executable is configured and this platform has no default "
            "location for one. Set DIRECTA_DGO_PATH to the dGO executable."
        )
    if not path.exists():
        raise LauncherError(
            f"dGO was not found at {path}. Set DIRECTA_DGO_PATH to its actual "
            "location."
        )
    return path


def _spawn(path: Path) -> int:
    """Start dGO in a process tree of its own, and return its pid.

    Detaching matters more than it looks. An MCP server is a child process the
    client starts and kills, and on Windows it may be held in a job object that
    takes its whole tree down on exit — which would close Darwin, possibly with
    working orders on the book, the moment the user quits Claude. Breaking away
    from the job is tried first and allowed to fail, since not every job permits
    it; a Darwin tied to the client's lifetime is still better than no Darwin.

    No pipes: a GUI app whose output nobody drains eventually blocks on a full
    pipe, and there is nothing here that would ever read them.
    """
    kwargs: dict[str, Any] = {
        "cwd": str(path.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name != "nt":
        return subprocess.Popen([str(path)], start_new_session=True, **kwargs).pid

    detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        flags = detached | subprocess.CREATE_BREAKAWAY_FROM_JOB
        return subprocess.Popen([str(path)], creationflags=flags, **kwargs).pid
    except OSError:
        return subprocess.Popen([str(path)], creationflags=detached, **kwargs).pid


def start_dgo(configured_path: str | None, now: float | None = None) -> dict[str, Any]:
    """Launch dGO and record the attempt. Returns immediately — the login,
    including its OTP, happens after this call and without us."""
    global _launch
    path = resolve_dgo_path(configured_path)
    pid = _spawn(path)
    _launch = {"pid": pid, "at": now if now is not None else time.time()}
    return {"dgo_path": str(path), "pid": pid}


def dgo_processes(configured_path: str | None) -> list[int]:
    """Pids of processes running out of dGO's installation directory, so a dGO
    the user started by hand is not invisible to us.

    Matching on the directory rather than on a process name is what makes this
    reliable: dGO.exe exits seconds after launch, handing over to the
    `java.exe` inside its own bundled runtime, and that is what actually stays
    up. The same runtime then runs Darwin (dGO puts `JAVA_HOME/bin` on the PATH
    before launching it), so a hit here means "something of Directa's is
    running" rather than specifically the launcher — which is all the caller
    needs, since the case where Darwin is fully up is settled by the ports
    before this is ever consulted.

    Any failure to look — no dGO installed, a process that closes mid-scan, a
    handle we may not open — is reported as "nothing found". A diagnostic must
    not be the thing that breaks a diagnosis.
    """
    if os.name != "nt":
        return []
    try:
        install_dir = resolve_dgo_path(configured_path).parent
    except LauncherError:
        return []

    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        return []

    found: list[int] = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            # Only the executables dGO can be running under, so we ask the OS
            # for a handle a few times rather than once per process on the box.
            if entry.szExeFile.lower() in ("java.exe", "javaw.exe", "dgo.exe"):
                pid = entry.th32ProcessID
                if _image_path(k32, pid, install_dir):
                    found.append(pid)
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return found


def _image_path(k32: Any, pid: int, install_dir: Path) -> bool:
    """Whether pid's executable lives under install_dir."""
    import ctypes
    from ctypes import wintypes

    handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        return Path(buffer.value).is_relative_to(install_dir)
    except (OSError, ValueError):
        return False
    finally:
        k32.CloseHandle(handle)


def launch_state(now: float | None = None) -> dict[str, Any]:
    """What this server knows about a dGO launch it performed itself. One the
    user started by hand does not appear here — dgo_processes is what sees
    those — but this is the stronger signal of the two, since a launch we made
    seconds ago says more than the existence of a process."""
    if _launch is None:
        return {"launched_here": False, "starting": False}
    elapsed = (now if now is not None else time.time()) - _launch["at"]
    return {
        "launched_here": True,
        "starting": elapsed < STARTUP_GRACE_SECONDS,
        "pid": _launch["pid"],
        "seconds_since_launch": round(elapsed, 1),
    }


def describe(
    trading_port_reachable: bool,
    autostart_enabled: bool,
    configured_path: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Turn port reachability, our own launch, and dGO's processes into the
    three states a caller can act on, each with the next step spelled out."""
    if trading_port_reachable:
        return {
            "state": "running",
            "hint": (
                "Darwin is running and listening. Nothing to start. Its link to "
                "Directa lags the ports by a few seconds after startup, so a "
                "get_darwin_status of CONN_UNAVAILABLE right after a launch is "
                "worth retrying once before reporting it as a fault."
            ),
        }

    state = launch_state(now)
    if state["starting"]:
        return {
            "state": "starting",
            "seconds_since_launch": state["seconds_since_launch"],
            "pid": state["pid"],
            "hint": (
                "dGO was launched from here and Darwin's ports are still shut, "
                "which normally means it is waiting for the user's OTP. Ask the "
                "user to finish the login, then call check_connection again — do "
                "not launch dGO a second time."
            ),
        }

    running = dgo_processes(configured_path)
    if running:
        return {
            "state": "starting",
            "dgo_pids": running,
            "hint": (
                "Darwin's ports are shut but Directa software is already running "
                "on this machine, so do not launch dGO: either a login is still "
                "in progress — the OTP, most likely — or Darwin is up without its "
                "API sockets, which is Sviluppatori > Dev kit in Darwin. Ask the "
                "user which, then call check_connection again."
            ),
        }

    return {
        "state": "stopped",
        "hint": (
            "Darwin is not running: no ports, and nothing of Directa's on the "
            "machine. "
        )
        + (
            "Call start_darwin to launch dGO."
            if autostart_enabled
            else "Ask the user to start it from dGO (autostart is off: the server "
            "was not started with DIRECTA_AUTOSTART=true)."
        ),
    }
