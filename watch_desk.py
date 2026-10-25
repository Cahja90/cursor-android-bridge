#wartet auf cursor und startet die brücke einmal
import ctypes
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_PATH = ROOT / "desk_server.py"
CLOUDFLARED = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
TUNNEL_TOKEN_PATH = ROOT / ".tunnel_token"
TUNNEL_PID_PATH = ROOT / ".tunnel_pid"
LISTEN_PORT = 8765
POLL_SEC = 5
START_WARTE_SEC = 25
MUTEX_NAME = "Local\\handy_cursor_watch"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

MUTEX_HANDLE = None


def halte_mutex():
    #zweite wächter-kopie sofort beenden
    global MUTEX_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    MUTEX_HANDLE = handle
    return True


def cursor_laeuft():
    proc = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Cursor.exe", "/NH"],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return "cursor.exe" in (proc.stdout or "").lower()


def port_offen(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def server_laeuft():
    #prozess schon da, port noch nicht offen
    befehl = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='py.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", befehl],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return "desk_server.py" in (proc.stdout or "").lower()


def python_befehl():
    hier = Path(sys.executable).resolve()
    if hier.name.lower() in ("python.exe", "pythonw.exe"):
        py_exe = hier.with_name("python.exe")
        if py_exe.is_file():
            return [str(py_exe), str(SERVER_PATH)]
    return ["py", "-3", str(SERVER_PATH)]


def starte_bruecke():
    try:
        subprocess.Popen(
            python_befehl(),
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def pid_laeuft(pid):
    if pid <= 0:
        return False
    proc = subprocess.run(
        ["tasklist", "/FI", "PID eq %s" % pid, "/NH"],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return str(pid) in (proc.stdout or "")


def tunnel_laeuft():
    if not TUNNEL_PID_PATH.is_file():
        return False
    try:
        pid = int(TUNNEL_PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    return pid_laeuft(pid)


def starte_tunnel():
    if not CLOUDFLARED.is_file() or not TUNNEL_TOKEN_PATH.is_file():
        return False
    token = TUNNEL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if len(token) < 20:
        return False
    try:
        proc = subprocess.Popen(
            [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "run", "--token", token],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        return False
    TUNNEL_PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    return True


def stoppe_tunnel():
    if not TUNNEL_PID_PATH.is_file():
        return
    try:
        pid = int(TUNNEL_PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        TUNNEL_PID_PATH.unlink(missing_ok=True)
        return
    if pid_laeuft(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    TUNNEL_PID_PATH.unlink(missing_ok=True)


def warte_auf_port(port, sekunden):
    ende = time.time() + sekunden
    while time.time() < ende:
        if port_offen(port):
            return True
        time.sleep(0.5)
    return False


def main():
    if not halte_mutex():
        return
    while True:
        if cursor_laeuft():
            if not tunnel_laeuft():
                starte_tunnel()
        elif tunnel_laeuft():
            stoppe_tunnel()
        if port_offen(LISTEN_PORT):
            time.sleep(POLL_SEC)
            continue
        if not cursor_laeuft():
            time.sleep(POLL_SEC)
            continue
        if server_laeuft():
            time.sleep(POLL_SEC)
            continue
        if starte_bruecke():
            warte_auf_port(LISTEN_PORT, START_WARTE_SEC)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
