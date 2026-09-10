import socket
import json

HOST = "127.0.0.1"
PORT = 65432


def _send(msg: dict) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        s.sendall(json.dumps(msg).encode())
        return json.loads(s.recv(4096).decode())


def start_scan(folder: str, params: dict, num_workers: int = 4) -> dict:
    return _send({
        "cmd":         "start",
        "folder":      folder,
        "params":      params,
        "num_workers": num_workers,
    })


def stop_scan() -> dict:
    return _send({"cmd": "stop"})