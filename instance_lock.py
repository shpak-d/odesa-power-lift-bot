"""Міжпроцесні блокування; файл залишається, OS-блокування зникає після аварії."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class InstanceLock:
    def __init__(self, token: str, database_path: Path):
        digest = hashlib.sha256(token.encode()).hexdigest()[:24]
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths = [
            Path(tempfile.gettempdir()) / f"odesa-power-{digest}.lock",
            database_path.parent / "monitor.instance.lock",
        ]
        self.files = []

    def __enter__(self) -> InstanceLock:
        try:
            for path in self.paths:
                stream = path.open("a+b")
                self.files.append(stream)
                if os.name == "nt":
                    import msvcrt

                    if path.stat().st_size == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.__exit__(None, None, None)
            raise RuntimeError(
                "Інший процес уже використовує цей токен або каталог SQLite"
            ) from exc
        return self

    def __exit__(self, *_: object) -> None:
        for stream in reversed(self.files):
            stream.close()
        self.files.clear()
