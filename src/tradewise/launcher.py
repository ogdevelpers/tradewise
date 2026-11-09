"""Run Tradewise quietly in the background, start it at login and put an icon on the desktop.

* Windows: a hidden launcher in the Startup folder and a "Tradewise" shortcut on the desktop.
* macOS:   a LaunchAgent (starts at login, restarts if it crashes) and a desktop link.
* Linux:   an XDG autostart entry and a desktop launcher.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)
APP_ID = "in.tradewise.app"


# ------------------------------------------------------------- server control
def ping(settings: Settings, timeout: float = 1.0) -> str:
    """'tradewise' if our app answers, 'other' if something else holds the port, '' if nothing does."""
    try:
        with urllib.request.urlopen(settings.url + "api/health", timeout=timeout) as r:
            body = json.loads(r.read().decode() or "{}")
            return "tradewise" if body.get("app") == "tradewise" else "other"
    except urllib.error.HTTPError:
        return "other"
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def python_for_background() -> str:
    """pythonw.exe on Windows (no console window), the normal interpreter elsewhere."""
    exe = Path(sys.executable)
    if os.name == "nt":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def server_command() -> list[str]:
    return [python_for_background(), "-m", "tradewise.cli", "serve"]


def spawn_background(settings: Settings) -> subprocess.Popen:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    out = open(settings.log_file, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child process
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": out, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    env = {**os.environ, "TRADEWISE_DATA_DIR": str(settings.data_dir), "TRADEWISE_API_PORT": str(settings.api_port)}
    return subprocess.Popen(server_command(), env=env, **kw)


def ensure_running(settings: Settings, wait_seconds: float = 30) -> bool:
    """Start the background server unless it already runs. True when it answers."""
    state = ping(settings)
    if state == "tradewise":
        return True
    if state == "other":
        raise RuntimeError(f"Another program is using port {settings.api_port}. "
                           "Set TRADEWISE_API_PORT to a free port, e.g. 8050.")
    spawn_background(settings)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if ping(settings) == "tradewise":
            return True
        time.sleep(0.5)
    return False


def stop_server(settings: Settings) -> bool:
    try:
        pid = int(settings.pid_file.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    settings.pid_file.unlink(missing_ok=True)
    return True


def open_browser(settings: Settings) -> None:
    webbrowser.open(settings.url)


# ---------------------------------------------------------- login + desktop
@dataclass
class InstallResult:
    created: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _desktop() -> Path:
    home = Path.home()
    candidates = [home / "Desktop", home / "OneDrive" / "Desktop"]
    if os.name != "nt":
        try:
            out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                candidates.insert(0, Path(out))
        except (OSError, subprocess.SubprocessError):
            pass
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _paths(settings: Settings, home: Path | None = None, desktop: Path | None = None) -> dict[str, Path]:
    home = home or Path.home()
    desktop = desktop or _desktop()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return {"autostart": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Tradewise.vbs",
                "shortcut": desktop / "Tradewise.vbs"}
    if sys.platform == "darwin":
        return {"autostart": home / "Library" / "LaunchAgents" / f"{APP_ID}.plist",
                "shortcut": desktop / "Tradewise.command"}
    return {"autostart": home / ".config" / "autostart" / "tradewise.desktop",
            "shortcut": desktop / "tradewise.desktop"}


def _vbs(args: list[str]) -> str:
    quoted = " ".join('""' + a + '""' if " " in a or "\\" in a else a for a in args)
    return ('\' Tradewise launcher (runs without a window)\r\n'
            f'CreateObject("WScript.Shell").Run "{quoted}", 0, False\r\n')


def install(settings: Settings, home: Path | None = None, desktop: Path | None = None,
            load_now: bool = True) -> InstallResult:
    """Start Tradewise at login and add a desktop icon that opens it."""
    res = InstallResult()
    paths = _paths(settings, home, desktop)
    py = python_for_background()
    env_note = {"TRADEWISE_DATA_DIR": str(settings.data_dir), "TRADEWISE_API_PORT": str(settings.api_port)}
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        paths["autostart"].write_text(_vbs([py, "-m", "tradewise.cli", "serve"]), encoding="utf-8")
        paths["shortcut"].write_text(_vbs([py, "-m", "tradewise.cli", "start"]), encoding="utf-8")
    elif sys.platform == "darwin":
        env_xml = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in env_note.items())
        args_xml = "".join(f"<string>{a}</string>" for a in [py, "-m", "tradewise.cli", "serve"])
        paths["autostart"].write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{APP_ID}</string>
<key>ProgramArguments</key><array>{args_xml}</array>
<key>EnvironmentVariables</key><dict>{env_xml}</dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>StandardOutPath</key><string>{settings.log_file}</string>
<key>StandardErrorPath</key><string>{settings.log_file}</string>
</dict></plist>
""", encoding="utf-8")
        paths["shortcut"].write_text(f'#!/bin/bash\n"{py}" -m tradewise.cli start\n'
                                     "osascript -e 'tell application \"Terminal\" to close front window' "
                                     ">/dev/null 2>&1 &\nexit 0\n", encoding="utf-8")
        paths["shortcut"].chmod(0o755)
        if load_now:
            subprocess.run(["launchctl", "unload", str(paths["autostart"])], capture_output=True, check=False)
            subprocess.run(["launchctl", "load", "-w", str(paths["autostart"])], capture_output=True, check=False)
    else:
        env_prefix = "env " + " ".join(f"{k}={v}" for k, v in env_note.items())
        paths["autostart"].write_text(
            f"[Desktop Entry]\nType=Application\nName=Tradewise (background)\n"
            f"Exec={env_prefix} \"{py}\" -m tradewise.cli serve\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n",
            encoding="utf-8")
        paths["shortcut"].write_text(
            f"[Desktop Entry]\nType=Application\nName=Tradewise\nComment=Open Tradewise\n"
            f"Exec={env_prefix} \"{py}\" -m tradewise.cli start\nTerminal=false\nIcon=utilities-system-monitor\n",
            encoding="utf-8")
        paths["shortcut"].chmod(0o755)
        res.notes.append("If your desktop asks, right-click the Tradewise icon and choose 'Allow launching'.")
    res.created = list(paths.values())
    return res


def uninstall(settings: Settings, home: Path | None = None, desktop: Path | None = None) -> list[Path]:
    removed = []
    paths = _paths(settings, home, desktop)
    if sys.platform == "darwin" and paths["autostart"].exists():
        subprocess.run(["launchctl", "unload", "-w", str(paths["autostart"])], capture_output=True, check=False)
    for p in paths.values():
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed


def is_installed(settings: Settings) -> bool:
    return _paths(settings)["autostart"].exists()
