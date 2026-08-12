"""A stand-in for Darwin's dAPI socket, so the client can be tested without the
platform running.

It reproduces the behaviours that actually broke things: an unsolicited push
before any command is sent, BEGIN/END framed list responses, ERR lines, and
responses delivered in small chunks that split lines across TCP reads.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Sequence

Response = Sequence[str] | Callable[[str], Sequence[str]]


class FakeDarwin:
    """Serves one connection on an ephemeral localhost port.

    `responses` maps an exact command string to the lines to answer with, or to
    a callable taking the command. Unknown commands get ERR 1003, as Darwin
    does. `chunk_size` splits every write into small pieces to exercise line
    reassembly across reads.
    """

    def __init__(
        self,
        responses: dict[str, Response] | None = None,
        push: Sequence[str] = (),
        chunk_size: int | None = None,
    ) -> None:
        self.responses: dict[str, Response] = dict(responses or {})
        self.push = list(push)
        self.chunk_size = chunk_size
        self.received: list[str] = []
        #: Lines to inject just before the next response, simulating a pushed
        #: update landing between a command and its reply.
        self.inject_before_next: list[str] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stopping = threading.Event()

    def __enter__(self) -> "FakeDarwin":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _write(self, conn: socket.socket, lines: Sequence[str]) -> None:
        data = "".join(f"{line}\r\n" for line in lines).encode("latin-1")
        if not self.chunk_size:
            conn.sendall(data)
            return
        for start in range(0, len(data), self.chunk_size):
            conn.sendall(data[start : start + self.chunk_size])
            time.sleep(0.002)

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            if self.push:
                self._write(conn, self.push)
            buffer = b""
            while not self._stopping.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    command = raw.decode("latin-1").strip()
                    if not command:
                        continue
                    self.received.append(command)
                    if self.inject_before_next:
                        self._write(conn, self.inject_before_next)
                        self.inject_before_next = []
                    self._write(conn, self._answer(command))

    def _answer(self, command: str) -> Sequence[str]:
        response = self.responses.get(command)
        if response is None:
            return [f"ERR;{command.split()[0]};1003"]
        if callable(response):
            return response(command)
        return response
