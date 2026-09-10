import os
import time
import subprocess
import threading
import queue
import socket
import sys
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

HOST = "127.0.0.1"
PORT = 65432

# --- Globals ---

_task_queue = queue.Queue()
_workers: list[threading.Thread] = []
_observer = None
_is_stopping = False
_server_running = True

# --- File readiness check ---

def _wait_for_file_ready(
    file_path: str,
    interval: float = 0.2,
    stable_count: int = 3,
    timeout: float = 30.0
) -> bool:
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_ticks = 0

    while time.monotonic() < deadline:
        try:
            current_size = os.path.getsize(file_path)
        except OSError:
            time.sleep(interval)
            continue

        if current_size == last_size and current_size > 0:
            stable_ticks += 1
            if stable_ticks >= stable_count:
                return True
        else:
            stable_ticks = 0
            last_size = current_size

        time.sleep(interval)

    return False


# --- Worker pool ---

def _worker(params: dict):
    while True:
        file_path = _task_queue.get()
        if file_path is None:
            _task_queue.task_done()
            break
        try:
            ready = _wait_for_file_ready(file_path)
            if not ready:
                continue
            subprocess.run([
                sys.executable,
                '-m',
                'src.autoscan.process_images',
                "--file",           file_path,
                "--material",       params["material"],
                "--substrate",      params["substrate"],
                "--confidence",     str(params["confidence_threshold"]),
                "--size_threshold",     str(params["size_threshold"]),
                "--scanned_date",   params["scanned_date"],
                "--keep_flakeless_images",   params["keep_flakeless_images"],
                "--user",   params["user"]
            ])
        except Exception as e:
            print(f"Failed with: {e}")
        finally:
            _task_queue.task_done()


# --- Watcher ---

class ScanHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            _task_queue.put(event.src_path)


# --- Lifecycle ---

def start_watcher(folder: str, params: dict, num_workers: int = 4):
    global _observer, _workers

    _workers = [
        threading.Thread(target=_worker, args=(params,), daemon=True)
        for _ in range(num_workers)
    ]
    for w in _workers:
        w.start()

    _observer = Observer()
    _observer.schedule(ScanHandler(), folder, recursive=False)
    _observer.start()


def stop_watcher(wait_for_queue: bool = True):
    global _observer, _workers, _is_stopping, _server_running

    if _is_stopping:
        return  # Prevent double-triggering
    _is_stopping = True

    # 1. FIRST, wait for workers to process everything currently in the queue
    if wait_for_queue:
        _task_queue.join()

    # 2. NOW stop the observer from listening to new files
    if _observer:
        _observer.stop()
        _observer.join()
        _observer = None

    # 3. Clean up the worker threads safely
    for _ in _workers:
        _task_queue.put(None)
    for w in _workers:
        w.join()
    _workers.clear()
    
    # 4. Signal the main server that it is completely safe to exit
    _is_stopping = False
    _server_running = False


# --- Command server ---

def _handle_command(conn: socket.socket):
    with conn:
        try:
            raw = conn.recv(4096).decode()
            if not raw:
                return
            msg = json.loads(raw)
            cmd = msg.get("cmd")

            if cmd == "start":
                start_watcher(msg["folder"], msg["params"], msg.get("num_workers", 4))
                conn.sendall(b'{"status": "ok", "msg": "watcher started"}')

            elif cmd == "stop":
                # We send the response immediately, then process the blocking shutdown
                conn.sendall(b'{"status": "ok", "msg": "watcher stopping and draining queue"}')
                stop_watcher(wait_for_queue=True)

            else:
                conn.sendall(b'{"status": "error", "msg": "unknown command"}')
        except Exception as e:
            try:
                conn.sendall(json.dumps({"status": "error", "msg": str(e)}).encode())
            except:
                pass


def run_server():
    global _server_running
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen()
        # Use a short timeout so the loop checks the _server_running flag regularly
        s.settimeout(1.0) 
        
        while _server_running:
            try:
                conn, _ = s.accept()
                # Run command inline or in thread; since stop_watcher blocks, 
                # a thread is fine, but it will now safely drop the server loop flag when done.
                threading.Thread(target=_handle_command, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue

if __name__ == "__main__":
    run_server()