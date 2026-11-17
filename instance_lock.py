"""Process lifetime lock without stale-PID or lock-file deletion races."""
from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        if self.handle is not None:
            raise RuntimeError("Блокировка этого экземпляра уже установлена.")
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            raise RuntimeError(f"Не удалось открыть файл блокировки: {exc}") from exc
        try:
            # Windows locks a byte range; keep at least one byte in the file.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("DM Hunter уже запущен в этой папке. Закройте другой экземпляр.") from exc
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        # Leave the file in place: deleting it creates an inode race on POSIX.
        return False
