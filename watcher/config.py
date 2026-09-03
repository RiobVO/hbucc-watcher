"""Загрузка конфигурации и секретов.

Разделение жёсткое и намеренное:

    config.toml  — поведение (пороги, лимиты, модель, whitelist). В git.
    окружение    — секреты. Никогда в git, никогда в снапшотах, никогда
                   в промте модели.

Секреты читаются один раз при старте и живут в отдельном объекте, который
никуда не передаётся кроме тех двух-трёх мест, где реально нужен. Это не
паранойя ради красоты: компонент анализа получает на вход недоверенный
текст с чужого сайта, и чем меньше поверхность, на которой секрет вообще
достижим, тем лучше.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Корень проекта — родитель пакета watcher/. Все пути в системе строятся
# от него, чтобы скрипт работал одинаково из любой рабочей директории
# (локально с Windows и в раннере GitHub Actions).
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
STATE_DIR = ROOT / "state"
PROMPTS_DIR = ROOT / "prompts"


class ConfigError(RuntimeError):
    """Конфигурация или окружение непригодны для запуска."""


@dataclass(frozen=True)
class Secrets:
    """Секреты из окружения.

    healthcheck_url опционален: система работает и без внешнего watchdog,
    просто теряет способность сообщить о собственной смерти. Остальные три
    обязательны — без них прогон бессмысленен, и падать надо сразу на
    старте с внятным сообщением, а не через две минуты в момент отправки.
    """

    openai_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    healthcheck_url: str | None
    # Отдельный токен на запись в репозиторий отчётов. Опционален по той
    # же логике, что и watchdog: без него система работает, просто шлёт
    # полный текст вместо карточки со ссылкой. У GITHUB_TOKEN в Actions
    # прав на чужой репозиторий нет, поэтому нужен именно свой PAT.
    reports_token: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        required = {
            "OPENAI_API_KEY": None,
            "TELEGRAM_BOT_TOKEN": None,
            "TELEGRAM_CHAT_ID": None,
        }
        missing = []
        for name in required:
            value = os.environ.get(name, "").strip()
            if not value:
                missing.append(name)
            required[name] = value

        if missing:
            raise ConfigError(
                "не заданы обязательные переменные окружения: "
                + ", ".join(missing)
                + ". Локально — через .env или set, в CI — через GitHub Secrets."
            )

        healthcheck = os.environ.get("HEALTHCHECK_URL", "").strip() or None
        if healthcheck is None:
            # Не ошибка, но должно быть заметно в логах: значит внешнего
            # детекта «задача перестала запускаться» сейчас нет.
            log.warning(
                "HEALTHCHECK_URL не задан — внешний watchdog отключён, "
                "смерть задачи останется незамеченной"
            )

        reports_token = os.environ.get("REPORTS_TOKEN", "").strip() or None
        if reports_token is None:
            log.warning(
                "REPORTS_TOKEN не задан — страницы разборов не публикуются, "
                "в Telegram уходит полный текст"
            )

        return cls(
            openai_api_key=required["OPENAI_API_KEY"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            healthcheck_url=healthcheck,
            reports_token=reports_token,
        )

    def __repr__(self) -> str:  # pragma: no cover - защита от случайного лога
        """Никогда не печатать значения.

        Обычный dataclass-repr вывалил бы ключи в traceback при первом же
        необработанном исключении, а логи GitHub Actions видны всем, у кого
        есть доступ к репозиторию.
        """
        return (
            f"Secrets(openai_api_key=<{len(self.openai_api_key)} chars>, "
            f"telegram_bot_token=<hidden>, telegram_chat_id=<hidden>, "
            f"healthcheck_url={'set' if self.healthcheck_url else 'unset'}, "
            f"reports_token={'set' if self.reports_token else 'unset'})"
        )


class Config:
    """Тонкая обёртка над разобранным TOML.

    Намеренно не превращаю конфиг в дерево dataclass-ов: полей около
    тридцати, они плоские, и каждое новое поле требовало бы правки в трёх
    местах. Доступ через section() с проверкой существования ключа даёт
    ровно ту защиту, которая нужна — падение на старте при опечатке в
    имени параметра, а не None посреди прогона.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        if not path.exists():
            raise ConfigError(f"конфиг не найден: {path}")
        with path.open("rb") as fh:
            return cls(tomllib.load(fh))

    def section(self, name: str) -> dict[str, Any]:
        try:
            return self._data[name]
        except KeyError as exc:
            raise ConfigError(f"в конфиге нет секции [{name}]") from exc

    def get(self, section: str, key: str) -> Any:
        sec = self.section(section)
        try:
            return sec[key]
        except KeyError as exc:
            raise ConfigError(f"в конфиге нет параметра {section}.{key}") from exc


def setup_logging(verbose: bool = False) -> None:
    """Единая настройка логов.

    Формат без цветов и без эмодзи: логи читаются в веб-интерфейсе
    GitHub Actions, где ANSI-последовательности выглядят мусором.

    Windows: консоль по умолчанию в cp1251, и любой русский текст в логе
    превращается в вопросительные знаки или роняет процесс на
    UnicodeEncodeError. В раннере Linux это не проявится — то есть баг был
    бы виден только на машине разработчика. Перенастраиваем потоки явно.
    """
    for stream in (sys.stdout, sys.stderr):
        # reconfigure есть у TextIOWrapper; при перенаправлении в pipe
        # объект может быть другим — тогда просто пропускаем.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # поток уже закрыт или не текстовый
                log.debug("не удалось перенастроить кодировку потока", exc_info=True)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)-18s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # httpx на INFO печатает каждый запрос с полным URL. Для нас это шум,
    # а в случае Telegram — ещё и токен бота в URL попал бы в лог.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
