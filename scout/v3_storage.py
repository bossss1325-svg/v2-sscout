from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Generator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class V3StateStore:
    def __init__(self, path: str | Path | None = None) -> None:
        default_path = Path.home() / ".auren-scout-v3" / "state.json"
        self.path = Path(path or os.getenv("SCOUT_V3_STATE_PATH") or default_path)
        self._lock = RLock()
        self._state: dict[str, Any] = {"tasks": {}, "messages": {}, "opt_outs": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Não foi possível ler o estado V3: {self.path}") from error
        if not isinstance(loaded, dict) or any(
            not isinstance(loaded.get(name), dict)
            for name in ("tasks", "messages", "opt_outs")
        ):
            raise RuntimeError(f"Formato inválido no estado V3: {self.path}")
        self._state = loaded

    def create_task(self, task_id: str, record: dict[str, Any]) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            if task_id in self._state["tasks"]:
                return False
            self._state["tasks"][task_id] = self._copy(record)
            self._write()
            return True

    def save_task(self, task_id: str, record: dict[str, Any]) -> None:
        with self._lock, self._state_file_lock():
            self._reload()
            self._state["tasks"][task_id] = self._copy(record)
            self._write()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._state_file_lock():
            self._reload()
            value = self._state["tasks"].get(task_id)
            return self._copy(value) if isinstance(value, dict) else None

    def create_message(self, message_id: str, record: dict[str, Any]) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            if message_id in self._state["messages"]:
                return False
            self._state["messages"][message_id] = self._copy(record)
            self._write()
            return True

    def update_message(self, message_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._state_file_lock():
            self._reload()
            record = self._state["messages"].get(message_id)
            if not isinstance(record, dict):
                raise KeyError(message_id)
            record.update(self._copy(updates))
            self._write()
            return self._copy(record)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock, self._state_file_lock():
            self._reload()
            record = self._state["messages"].get(message_id)
            return self._copy(record) if isinstance(record, dict) else None

    def message_history(self, recipient_id: str) -> list[dict[str, Any]]:
        with self._lock, self._state_file_lock():
            self._reload()
            return self._copy([
                record for record in self._state["messages"].values()
                if record.get("recipient_id") == recipient_id
            ])

    def message_history_for_all(self) -> list[dict[str, Any]]:
        with self._lock, self._state_file_lock():
            self._reload()
            return self._copy(list(self._state["messages"].values()))

    def has_duplicate(self, recipient_id: str, body: str) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            return any(
                record.get("recipient_id") == recipient_id
                and record.get("body") == body
                and record.get("status") in {"draft", "sending", "sent", "send_uncertain"}
                for record in self._state["messages"].values()
            )

    def record_opt_out(self, recipient_id: str, reason: str) -> dict[str, Any]:
        with self._lock, self._state_file_lock():
            self._reload()
            record = {
                "recipient_id": recipient_id,
                "reason": reason,
                "recorded_at": _utc_now(),
            }
            self._state["opt_outs"][recipient_id] = record
            self._write()
            return self._copy(record)

    def is_opted_out(self, recipient_id: str) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            return recipient_id in self._state["opt_outs"]

    def latest_send_time(self) -> str | None:
        with self._lock, self._state_file_lock():
            self._reload()
            sent = [
                record.get("sent_at")
                for record in self._state["messages"].values()
                if record.get("status") == "sent" and isinstance(record.get("sent_at"), str)
            ]
            return max(sent) if sent else None

    def sent_since(self, seconds: int) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        with self._lock, self._state_file_lock():
            self._reload()
            results = []
            for record in self._state["messages"].values():
                if record.get("status") != "sent" or not isinstance(record.get("sent_at"), str):
                    continue
                try:
                    sent_at = datetime.fromisoformat(record["sent_at"])
                except ValueError:
                    continue
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                if sent_at >= cutoff:
                    results.append(self._copy(record))
            return results

    @contextmanager
    def _state_file_lock(self) -> Generator[None, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with open(lock_path, "a+b") as lock_file:
            if hasattr(os, "chmod"):
                os.chmod(lock_path, 0o600)
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                if hasattr(os, "chmod"):
                    os.chmod(temporary_path, 0o600)
                json.dump(self._state, temporary_file, ensure_ascii=False, separators=(",", ":"))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

    @staticmethod
    def _copy(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))