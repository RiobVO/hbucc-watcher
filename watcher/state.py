"""Состояние: снапшот, журнал доставок, heartbeat — и запись их в git.

Почему git, а не БД или KV: состояние здесь одновременно является
хранилищем, историей и инструментом отладки. `git diff HEAD~1
state/snapshot.json` показывает ровно то, что система сочла изменением;
`git checkout HEAD~1 state/snapshot.json` — это вся процедура
восстановления из повреждённого состояния.

ДОЛГОВЕЧНОСТЬ У ФАЙЛОВ РАЗНАЯ, и это не случайность:

    ledger    пушится ПОСЛЕ КАЖДОЙ доставки. Раннер эфемерный: файл,
              записанный на диск но не запушенный, равен потерянному.
              А ledger — единственная гарантия против дублей, поэтому
              его нельзя копить до конца прогона.
    snapshot  пушится ОДИН РАЗ в конце и только если доставлено всё.
              Это и есть сквозной инвариант системы.
    heartbeat пушится не чаще раза в ~20 часов. Его задача — не дать
              GitHub отключить scheduled workflow за 60 дней без
              активности в репозитории.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from watcher.source import Document, Part

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Коммиты делаются от имени бота, но БЕЗ изменения глобального git-конфига:
# параметры передаются флагом -c на конкретную команду. Так скрипт не
# оставляет следов в окружении и не зависит от того, настроен ли identity
# в раннере.
_COMMIT_IDENTITY = (
    "-c", "user.name=hbucc-watcher[bot]",
    "-c", "user.email=hbucc-watcher@users.noreply.github.com",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _due(stamp: str, interval_hours: int) -> bool:
    """Прошло ли interval_hours с метки. Метки нет или она битая — да, пора.

    Ошибаться безопаснее в сторону действия: пропущенный коммит heartbeat
    приближает отключение workflow, пропущенная проба продлевает слепоту
    к кончившейся квоте.
    """
    if not stamp:
        return True
    try:
        previous = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True
    elapsed = (datetime.now(timezone.utc) - previous.replace(tzinfo=timezone.utc)).total_seconds()
    return elapsed >= interval_hours * 3600


class StateCorrupted(RuntimeError):
    """Состояние на диске непригодно. Автовосстановления НЕТ намеренно.

    Молча пересоздать снапшот означало бы отрапортовать «все 127 советов
    новые» и завалить Telegram. Правильная реакция — остановиться и
    сообщить; починка это `git checkout` одного файла.
    """


class GitError(RuntimeError):
    """Не удалось зафиксировать состояние в git."""


# --------------------------------------------------------------------------
# Модели состояния
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    """Разобранное состояние сайта на момент последней успешной доставки."""

    schema_version: int
    source: dict[str, Any]
    shape: dict[str, Any]
    assumptions: dict[str, Any]
    parts: list[Part]

    @property
    def parts_by_number(self) -> dict[int, Part]:
        return {p.number: p for p in self.parts}

    @property
    def blocks_count(self) -> int:
        return sum(len(p.blocks) for p in self.parts)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "shape": self.shape,
            "assumptions": self.assumptions,
            "parts": [p.to_dict() for p in self.parts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise StateCorrupted(
                f"snapshot.json имеет schema_version={version}, "
                f"код рассчитан на {SCHEMA_VERSION}. Нужна явная миграция, "
                f"автоматически читать нельзя."
            )
        return cls(
            schema_version=version,
            source=data.get("source", {}),
            shape=data.get("shape", {}),
            assumptions=data.get("assumptions", {}),
            parts=[Part.from_dict(p) for p in data.get("parts", [])],
        )

    @classmethod
    def from_document(
        cls, doc: Document, *, url: str, etag: str | None,
        last_modified: str | None, raw_bytes: int,
    ) -> "Snapshot":
        return cls(
            schema_version=SCHEMA_VERSION,
            source={
                "url": url,
                "etag": etag,
                "last_modified": last_modified,
                "fetched_at": utcnow(),
                "content_hash": doc.content_hash,
                "raw_bytes": raw_bytes,
            },
            shape={
                "parts_count": len(doc.parts),
                "blocks_count": doc.blocks_count,
                "tips_count": doc.tips_count,
                "text_chars": doc.text_chars,
                "parser_profile": doc.parser_profile,
            },
            assumptions={
                # Наблюдения, которые могут перестать выполняться. Система
                # обязана заметить это, а не работать на устаревшем допущении.
                "rss_absent": True,
                "single_document": True,
                "new_parts_appended_at_end": True,
                "last_verified": utcnow(),
            },
            parts=doc.parts,
        )


@dataclass
class Ledger:
    """Журнал доставленных событий — единственная гарантия против дублей."""

    delivered: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {entry["event_id"] for entry in self.delivered}

    def add(self, event_id: str, kind: str, part: int, bid: str, messages: int) -> None:
        self.delivered.append(
            {
                "event_id": event_id,
                "kind": kind,
                "part": part,
                "bid": bid,
                "sent_at": utcnow(),
                "messages": messages,
            }
        )

    def trim(self, keep: int) -> None:
        if len(self.delivered) > keep:
            self.delivered = self.delivered[-keep:]

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "delivered": self.delivered}

    @classmethod
    def from_dict(cls, data: dict) -> "Ledger":
        return cls(delivered=list(data.get("delivered", [])))


@dataclass
class Heartbeat:
    """Признаки жизни и счётчики подряд идущих отказов."""

    last_run: str = ""
    last_change_detected: str = ""
    last_committed: str = ""
    consecutive_source_failures: int = 0
    consecutive_model_failures: int = 0
    runs_total: int = 0
    # Подпись последнего нарушенного инварианта. Нужна, чтобы не слать
    # одинаковый алерт каждые 6 часов, пока парсер не починен: о поломке
    # сообщаем на переходе «работало -> сломалось», а дальше об этом
    # молчит Telegram и говорит внешний watchdog отсутствием пинга.
    last_violation_signature: str = ""
    # Когда последний раз проверяли, что ключ жив и квота не кончилась.
    # Отдельно от last_run: прогонов четыре в сутки, а проба нужна одна.
    last_model_probe: str = ""

    def to_dict(self) -> dict:
        return {
            "last_run": self.last_run,
            "last_change_detected": self.last_change_detected,
            "last_committed": self.last_committed,
            "consecutive_source_failures": self.consecutive_source_failures,
            "consecutive_model_failures": self.consecutive_model_failures,
            "runs_total": self.runs_total,
            "last_violation_signature": self.last_violation_signature,
            "last_model_probe": self.last_model_probe,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Heartbeat":
        return cls(**{k: data.get(k, v) for k, v in cls().to_dict().items()})

    def due_for_commit(self, interval_hours: int) -> bool:
        """Пора ли фиксировать heartbeat в git.

        Слишком часто — история репозитория превращается в 120 коммитов
        «touch» в месяц. Слишком редко — GitHub отключает scheduled workflow
        после 60 дней без активности, и система умирает тихо.
        """
        return _due(self.last_committed, interval_hours)

    def due_for_probe(self, interval_hours: int) -> bool:
        """Пора ли проверять, что модель вообще доступна.

        Каждый прогон проверять незачем: это лишний запрос каждые шесть
        часов ради состояния, которое меняется раз в месяцы.
        """
        return _due(self.last_model_probe, interval_hours)


# --------------------------------------------------------------------------
# Хранилище
# --------------------------------------------------------------------------


class StateStore:
    """Чтение/запись состояния и его фиксация в git."""

    def __init__(self, state_dir: Path, *, git_enabled: bool = True, push_retries: int = 3):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.push_retries = push_retries
        self.git_enabled = git_enabled and self._is_git_repo()
        if git_enabled and not self.git_enabled:
            log.warning(
                "каталог не является git-репозиторием — состояние пишется "
                "только на диск. В CI это означало бы потерю состояния."
            )

    # --- пути ---
    @property
    def snapshot_path(self) -> Path:
        return self.dir / "snapshot.json"

    @property
    def ledger_path(self) -> Path:
        return self.dir / "delivered.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.dir / "heartbeat.json"

    @property
    def raw_path(self) -> Path:
        return self.dir / "last_raw.html.gz"

    # --- атомарная запись ---
    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        """Записать JSON атомарно.

        Через временный файл и os.replace: процесс, убитый посреди записи,
        иначе оставил бы обрезанный JSON, а это по нашим правилам —
        остановка системы с ручной починкой. Дешевле не допустить.
        sort_keys для стабильных диффов в git.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateCorrupted(
                f"{path.name} не разбирается как JSON: {exc}. "
                f"Починка: git checkout HEAD~1 -- {path.as_posix()}"
            ) from exc

    # --- снапшот ---
    def load_snapshot(self) -> Snapshot | None:
        if not self.snapshot_path.exists():
            return None
        return Snapshot.from_dict(self._read_json(self.snapshot_path))

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self._write_json(self.snapshot_path, snapshot.to_dict())

    # --- журнал ---
    def load_ledger(self) -> Ledger:
        if not self.ledger_path.exists():
            return Ledger()
        return Ledger.from_dict(self._read_json(self.ledger_path))

    def save_ledger(self, ledger: Ledger) -> None:
        self._write_json(self.ledger_path, ledger.to_dict())

    # --- heartbeat ---
    def load_heartbeat(self) -> Heartbeat:
        if not self.heartbeat_path.exists():
            return Heartbeat()
        return Heartbeat.from_dict(self._read_json(self.heartbeat_path))

    def save_heartbeat(self, heartbeat: Heartbeat) -> None:
        self._write_json(self.heartbeat_path, heartbeat.to_dict())

    # --- сырой HTML для вскрытия сломанного парсера ---
    def save_raw_html(self, html: str) -> None:
        import gzip

        tmp = self.raw_path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, self.raw_path)

    def load_raw_html(self) -> str | None:
        import gzip

        if not self.raw_path.exists():
            return None
        with gzip.open(self.raw_path, "rt", encoding="utf-8") as fh:
            return fh.read()

    # --- git ---
    def _is_git_repo(self) -> bool:
        result = self._git("rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.dir.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def commit_and_push(self, paths: Iterable[Path], message: str) -> bool:
        """Зафиксировать файлы и обязательно довести их до удалённой ветки.

        Здесь закрывается дыра, из-за которой ledger мог не пережить прогон:
        push способен упасть на non-fast-forward, если ветка уехала (правка
        конфига через веб-интерфейс, параллельный ручной запуск). Раньше это
        означало бы «журнал доставок не сохранён» и повторную рассылку всех
        событий следующим прогоном.

        Лечение: commit -> (pull --rebase --autostash -> push) с повторами.
        Если push не прошёл и после повторов — это ошибка, а не warning:
        вызывающий код обязан поднять алерт, потому что тихий провал здесь
        ломает единственную гарантию против дублей.
        """
        if not self.git_enabled:
            log.info("git отключён — пропускаю коммит «%s»", message)
            return False

        relative = [p.relative_to(self.dir.parent).as_posix() for p in paths]
        self._git("add", "--", *relative)

        staged = self._git("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            log.debug("нечего коммитить: %s", message)
            return False

        self._git(*_COMMIT_IDENTITY, "commit", "-m", message)

        last_error = ""
        for attempt in range(1, self.push_retries + 1):
            pull = self._git(
                *_COMMIT_IDENTITY, "pull", "--rebase", "--autostash", check=False
            )
            if pull.returncode != 0:
                last_error = pull.stderr.strip()
                # Нет remote/upstream — локальный прогон. Коммит сделан,
                # этого достаточно, дальше пушить некуда.
                if "no such remote" in last_error.lower() or "no upstream" in last_error.lower():
                    log.info("удалённой ветки нет — остаюсь на локальном коммите")
                    return True
                log.warning("pull --rebase не прошёл (попытка %d): %s", attempt, last_error)
                self._git("rebase", "--abort", check=False)
            else:
                push = self._git("push", check=False)
                if push.returncode == 0:
                    log.info("состояние зафиксировано: %s", message)
                    return True
                last_error = push.stderr.strip()
                log.warning("push не прошёл (попытка %d): %s", attempt, last_error)

            if attempt < self.push_retries:
                time.sleep(2 * attempt)

        raise GitError(
            f"не удалось запушить «{message}» за {self.push_retries} попыток: {last_error}. "
            f"Журнал доставок не сохранён на удалённой ветке — следующий прогон "
            f"может продублировать уже отправленные разборы."
        )
