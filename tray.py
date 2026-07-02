"""
Royal Road TTS — System Tray Launcher

Double-click this script (or run it) to start the server in the background
with a system tray icon. Right-click the tray icon for options.

Usage:
    python tray.py
    pythonw tray.py   (no console window)
"""

import os
import sys
import subprocess
import threading
import time
import webbrowser
import signal
import socket

from PIL import Image, ImageDraw, ImageFont
import pystray

# ── Configuration ──
HOST = "0.0.0.0"
PORT = 8000
APP_NAME = "Novel TTS"
URL = f"http://localhost:{PORT}"

# Path to this project
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, ".venv", "Scripts", "python.exe")
MAIN_SCRIPT = os.path.join(PROJECT_DIR, "main.py")

# Use venv python if available, otherwise system python
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable

# ── Globals ──
server_process = None
tray_icon = None


def create_book_icon(size=64):
    """Generate a simple book icon programmatically."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Book body (closed book, side view)
    margin = int(size * 0.12)
    w, h = size - 2 * margin, size - 2 * margin

    # Back cover (slightly offset)
    back_x = margin + 4
    back_y = margin
    draw.rounded_rectangle(
        [back_x, back_y, back_x + w - 4, back_y + h],
        radius=3,
        fill="#5B6EAE",
        outline="#3D4B7A",
        width=1,
    )

    # Front cover
    front_x = margin
    front_y = margin + 2
    draw.rounded_rectangle(
        [front_x, front_y, front_x + w - 4, front_y + h - 2],
        radius=3,
        fill="#7B8EC8",
        outline="#5B6EAE",
        width=1,
    )

    # Spine line
    spine_x = front_x + 4
    draw.line(
        [(spine_x, front_y + 4), (spine_x, front_y + h - 6)],
        fill="#5B6EAE",
        width=2,
    )

    # Title lines on cover
    line_y_start = front_y + int(h * 0.25)
    line_x_start = spine_x + 6
    line_x_end = front_x + w - 12
    for i in range(3):
        ly = line_y_start + i * 6
        lx_end = line_x_end - (i * 4)
        if lx_end > line_x_start:
            draw.line(
                [(line_x_start, ly), (lx_end, ly)],
                fill="#FFFFFF",
                width=1,
            )

    return img


def is_port_in_use(port):
    """Check if the port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server():
    """Start the FastAPI server as a subprocess."""
    global server_process

    if server_process and server_process.poll() is None:
        return  # Already running

    if is_port_in_use(PORT):
        return  # Something already on this port

    log_path = os.path.join(PROJECT_DIR, "server.log")
    log_file = open(log_path, "w")

    kwargs = dict(
        cwd=PROJECT_DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    # On Windows, hide the console window for the subprocess
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si

    server_process = subprocess.Popen(
        [PYTHON, MAIN_SCRIPT, "--host", HOST, "--port", str(PORT)],
        **kwargs,
    )


def _pids_listening_on_port(port):
    """PIDs of processes LISTENING on the given TCP port (netstat parse)."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP"
                and parts[1].endswith(f":{port}") and "LISTENING" in parts):
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    return pids


def stop_server():
    """
    Stop the server: our own subprocess if we spawned it, and otherwise
    whatever process holds the port — the tray may have been relaunched
    since the server was started, in which case it isn't our child.
    """
    global server_process
    if server_process and server_process.poll() is None:
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
    server_process = None

    for pid in _pids_listening_on_port(PORT):
        if pid == os.getpid():
            continue
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    # Wait for the port to actually free so a restart can bind
    for _ in range(20):
        if not is_port_in_use(PORT):
            return
        time.sleep(0.5)


def restart_server():
    """Restart the server and update the tooltip once it's back up."""
    stop_server()
    start_server()
    # Model imports take a while before the port binds; poll before updating
    for _ in range(60):
        if is_port_in_use(PORT):
            break
        time.sleep(1)
    update_tray_title()


def open_browser(icon=None, item=None):
    """Open the web app in the default browser."""
    webbrowser.open(URL)


def on_restart(icon, item):
    """Tray menu: restart server."""
    threading.Thread(target=restart_server, daemon=True).start()


def on_quit(icon, item):
    """Tray menu: stop server and exit."""
    stop_server()
    icon.stop()


def server_status():
    """Check if server is running."""
    if server_process and server_process.poll() is None:
        return "Running"
    return "Stopped"


def update_tray_title():
    """Update the tray icon tooltip."""
    global tray_icon
    if tray_icon:
        status = server_status()
        tray_icon.title = f"{APP_NAME} — {status}"


def create_tray():
    """Create and run the system tray icon."""
    global tray_icon

    icon_image = create_book_icon(64)

    menu = pystray.Menu(
        pystray.MenuItem(
            lambda text: f"{APP_NAME} ({server_status()})",
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open in Browser", open_browser, default=True),
        pystray.MenuItem("Restart Server", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    tray_icon = pystray.Icon(
        name="royal_road_tts",
        icon=icon_image,
        title=f"{APP_NAME} — Starting...",
        menu=menu,
    )

    return tray_icon


def main():
    # Start server in background
    start_server()

    # Create and run tray icon (blocks on main thread)
    icon = create_tray()

    # Update title once server is likely ready
    def _wait_and_update():
        import time
        for _ in range(30):
            time.sleep(1)
            if is_port_in_use(PORT):
                update_tray_title()
                return
        update_tray_title()

    threading.Thread(target=_wait_and_update, daemon=True).start()

    # Run the tray (blocks until quit)
    icon.run()


if __name__ == "__main__":
    main()
