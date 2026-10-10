import json
import os
import importlib
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Generator, cast

from scout.models import Lead


class LeadStore:
    """Thread-safe in-memory store; data is cleared when the process exits."""

    def __init__(self) -> None:
        self._leads: dict[str, Lead] = {}
        self._lock = Lock()

    def save(self, lead: Lead) -> None:
        with self._lock:
            self._leads[lead.id] = lead

    def get(self, lead_id: str) -> Lead | None:
        with self._lock:
            return self._leads.get(lead_id)


class V2StateStore:
    """Durable JSON state for V2 task progress and normalized leads."""

    def __init__(self, path: str | Path | None = None) -> None:
        default_path = Path.home() / ".auren-scout-v2" / "state.json"
        self.path = Path(path or os.getenv("SCOUT_V2_STATE_PATH") or default_path)
        self._lock = RLock()
        self._state: dict[str, Any] = {
            "tasks": {},
            "leads": {},
            "operator_tasks": {},
            "result_packages": {},
        }
        self._reload()

    def _reload(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Não foi possível ler o estado V2: {self.path}") from error
            if (
                not isinstance(loaded, dict)
                or not isinstance(loaded.get("tasks"), dict)
                or not isinstance(loaded.get("leads"), dict)
            ):
                raise RuntimeError(f"Formato inválido no estado V2: {self.path}")
            loaded.setdefault("operator_tasks", {})
            loaded.setdefault("result_packages", {})
            if not isinstance(loaded["operator_tasks"], dict) or not isinstance(
                loaded["result_packages"], dict
            ):
                raise RuntimeError(f"Formato inválido no estado V2: {self.path}")
            self._state = loaded

    def get_task_record(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._state_file_lock():
            self._reload()
            record = self._state["tasks"].get(task_id)
            return self._copy_json(record) if isinstance(record, dict) else None

    def task_exists(self, task_id: str) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            return task_id in self._state["tasks"]

    def create_task_record(self, task_id: str, record: dict[str, Any]) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            if task_id in self._state["tasks"]:
                return False
            self._state["tasks"][task_id] = self._copy_json(record)
            self._write()
            return True

    def load_task_records(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._state_file_lock():
            self._reload()
            return self._copy_json(self._state["tasks"])

    def load_leads(self) -> list[dict[str, Any]]:
        with self._lock, self._state_file_lock():
            self._reload()
            return self._copy_json(list(self._state["leads"].values()))

    def save_task_record(self, task_id: str, record: dict[str, Any]) -> None:
        with self._lock, self._state_file_lock():
            self._reload()
            self._state["tasks"][task_id] = self._copy_json(record)
            self._write()

    def replace_leads(self, leads: list[dict[str, Any]]) -> None:
        indexed: dict[str, dict[str, Any]] = {}
        for lead in leads:
            lead_id = lead.get("lead_id") if isinstance(lead, dict) else None
            if not isinstance(lead_id, str) or not lead_id:
                raise ValueError("Cada lead persistido deve ter lead_id")
            indexed[lead_id] = self._copy_json(lead)
        with self._lock, self._state_file_lock():
            self._reload()
            self._state["leads"] = indexed
            self._write()

    def create_operator_task(self, task_id: str, record: dict[str, Any]) -> bool:
        with self._lock, self._state_file_lock():
            self._reload()
            if task_id in self._state["operator_tasks"]:
                return False
            self._state["operator_tasks"][task_id] = self._copy_json(record)
            self._write()
            return True

    def save_operator_task(self, task_id: str, record: dict[str, Any]) -> None:
        with self._lock, self._state_file_lock():
            self._reload()
            self._state["operator_tasks"][task_id] = self._copy_json(record)
            self._write()

    def get_operator_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._state_file_lock():
            self._reload()
            record = self._state["operator_tasks"].get(task_id)
            return self._copy_json(record) if isinstance(record, dict) else None

    def load_operator_tasks(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._state_file_lock():
            self._reload()
            return self._copy_json(self._state["operator_tasks"])

    def save_result_package(self, task_id: str, package: dict[str, Any]) -> None:
        package_id = package.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            raise ValueError("Pacote de resultado exige package_id")
        with self._lock, self._state_file_lock():
            self._reload()
            self._state["result_packages"][task_id] = self._copy_json(package)
            self._write()

    def get_result_package(self, task_id_or_package_id: str) -> dict[str, Any] | None:
        with self._lock, self._state_file_lock():
            self._reload()
            package = self._state["result_packages"].get(task_id_or_package_id)
            if package is None:
                package = next((
                    item for item in self._state["result_packages"].values()
                    if item.get("package_id") == task_id_or_package_id
                ), None)
            return self._copy_json(package) if isinstance(package, dict) else None

    def list_result_packages(
        self,
        delivery_status: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock, self._state_file_lock():
            self._reload()
            packages = list(self._state["result_packages"].values())
            if delivery_status is not None:
                packages = [
                    package for package in packages
                    if package.get("delivery", {}).get("status") == delivery_status
                ]
            return self._copy_json(packages)

    def record_package_delivery(
        self,
        package_id: str,
        status: str,
        *,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"delivered", "failed"}:
            raise ValueError("delivery status deve ser delivered ou failed")
        with self._lock, self._state_file_lock():
            self._reload()
            package = next((
                item for item in self._state["result_packages"].values()
                if item.get("package_id") == package_id
            ), None)
            if package is None:
                return None
            delivery = package.setdefault("delivery", {
                "status": "pending",
                "attempts": 0,
                "last_attempt_at": None,
                "delivered_at": None,
                "error": None,
            })
            if delivery.get("status") == "delivered":
                return self._copy_json(package)
            delivery["attempts"] = int(delivery.get("attempts", 0)) + 1
            delivery["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            delivery["status"] = status
            delivery["error"] = detail if status == "failed" else None
            if status == "delivered":
                delivery["delivered_at"] = delivery["last_attempt_at"]
            self._write()
            return self._copy_json(package)

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
                msvcrt = cast(Any, importlib.import_module("msvcrt"))

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
    def _copy_json(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))