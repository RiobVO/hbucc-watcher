"""Настройка и проверка секретов.

    python tools/setup.py check        проверить все секреты из окружения
    python tools/setup.py chat-id      найти chat_id (напиши боту перед этим)
    python tools/setup.py secrets      залить секреты в GitHub через gh
    python tools/setup.py test-message отправить тестовое сообщение

Секреты вводятся интерактивно или берутся из окружения — НИКОГДА из
аргументов командной строки: argv виден в списке процессов и оседает в
истории шелла.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher.analyze import (  # noqa: E402
    PROBE_OK,
    PROBE_QUOTA,
    PROBE_UNREACHABLE,
    probe_model,
)
from watcher.config import Config, setup_logging  # noqa: E402

SECRET_NAMES = (
    "OPENAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "HEALTHCHECK_URL",
    "REPORTS_TOKEN",
)

OK = "OK  "
BAD = "FAIL"
SKIP = "--  "


def ask(name: str, *, secret: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    prompt = f"{name}: "
    return (getpass.getpass(prompt) if secret else input(prompt)).strip()


def check_telegram(token: str, chat_id: str) -> bool:
    try:
        with httpx.Client(timeout=15.0) as client:
            me = client.get(f"https://api.telegram.org/bot{token}/getMe").json()
    except httpx.HTTPError as exc:
        print(f"{BAD} Telegram: сеть недоступна: {exc}")
        return False
    if not me.get("ok"):
        print(f"{BAD} Telegram: токен отвергнут ({me.get('description')})")
        return False
    print(f"{OK} Telegram: бот @{me['result']['username']}")

    if not chat_id:
        print(f"{SKIP} chat_id не задан — запусти 'python tools/setup.py chat-id'")
        return False
    print(f"{OK} chat_id: {chat_id}")
    return True


def check_openai(api_key: str) -> bool:
    """Проверка ключа и доступности модели из конфига.

    Список моделей бесплатен и не тратит токены, а заодно отвечает на
    вопрос поважнее живости ключа: доступна ли этому аккаунту именно та
    модель, которая прописана в config.toml. Ключ, у которого нет доступа
    к модели, выглядит рабочим ровно до первого настоящего разбора.
    """
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        print(f"{BAD} OpenAI: сеть недоступна: {exc}")
        return False
    if response.status_code != 200:
        print(f"{BAD} OpenAI: HTTP {response.status_code} — {response.text[:200]}")
        return False

    available = {m["id"] for m in response.json().get("data", [])}
    wanted = Config.load().get("model", "name")
    if wanted not in available:
        print(f"{BAD} OpenAI: ключ рабочий, но модель {wanted} этому аккаунту недоступна")
        return False

    # Список моделей отдаётся с 200 и при нулевом балансе — проверено. Поэтому
    # одного его недостаточно: нужен настоящий, пусть и крошечный, запрос на
    # генерацию. Иначе команда рапортует «ключ рабочий» про мёртвый аккаунт.
    # Та же проба, что раннер делает по расписанию: держать вторую копию
    # этой проверки значит однажды получить два разных вердикта об одном
    # аккаунте.
    outcome = probe_model(api_key=api_key, model=wanted)
    if outcome == PROBE_OK:
        print(f"{OK} OpenAI: ключ рабочий, модель {wanted} отвечает")
        return True
    if outcome == PROBE_QUOTA:
        print(f"{BAD} OpenAI: на аккаунте нет квоты — пополни баланс. Ключ при этом валиден")
    elif outcome == PROBE_UNREACHABLE:
        print(f"{BAD} OpenAI: проба не дошла — сеть, лимит частоты или сбой провайдера")
    else:
        print(f"{BAD} OpenAI: проба отвергнута — {outcome}")
    return False


def check_healthcheck(url: str) -> bool:
    if not url:
        print(f"{SKIP} HEALTHCHECK_URL не задан — внешнего детекта смерти НЕТ")
        return False
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        print(f"{BAD} healthcheck: {exc}")
        return False
    if response.status_code == 200:
        print(f"{OK} healthcheck: пинг принят")
        return True
    print(f"{BAD} healthcheck: HTTP {response.status_code}")
    return False


def check_reports(pat: str, repo: str) -> bool:
    """Есть ли у токена право писать в репозиторий отчётов.

    Проверяется именно право на запись, а не факт существования токена:
    PAT без доступа к чужому репозиторию выглядит рабочим ровно до первой
    настоящей публикации — и та тихо деградирует в полный текст.
    """
    if not pat:
        print(f"{SKIP} REPORTS_TOKEN не задан — страницы разборов публиковаться не будут")
        return False
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"https://api.github.com/repos/{repo}",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json",
                },
            )
    except httpx.HTTPError as exc:
        print(f"{BAD} отчёты: сеть недоступна: {exc}")
        return False
    if response.status_code != 200:
        print(f"{BAD} отчёты: {repo} недоступен — HTTP {response.status_code}")
        return False
    if not response.json().get("permissions", {}).get("push"):
        print(f"{BAD} отчёты: токен видит {repo}, но права на запись у него нет")
        return False
    print(f"{OK} отчёты: {repo} доступен на запись")
    return True


def cmd_check() -> int:
    token = ask("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    api_key = ask("OPENAI_API_KEY")
    hc = os.environ.get("HEALTHCHECK_URL", "").strip()
    reports = os.environ.get("REPORTS_TOKEN", "").strip()

    results = [
        check_telegram(token, chat_id),
        check_openai(api_key),
        check_healthcheck(hc),
        check_reports(reports, Config.load().get("reports", "repo")),
    ]
    print()
    if all(results):
        print("Всё на месте. Можно запускать bootstrap.")
        return 0
    print("Не всё готово — см. строки FAIL/-- выше.")
    return 1


def cmd_chat_id() -> int:
    """Найти chat_id.

    Бот не может написать первым: пока ты не отправишь ему сообщение,
    getUpdates пуст, и chat_id взять неоткуда.
    """
    token = ask("TELEGRAM_BOT_TOKEN")
    with httpx.Client(timeout=15.0) as client:
        data = client.get(f"https://api.telegram.org/bot{token}/getUpdates").json()

    if not data.get("ok"):
        print(f"{BAD} {data.get('description')}")
        return 1

    chats = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")

    if not chats:
        print("Обновлений нет. Напиши боту любое сообщение и запусти снова.")
        print("Важно: getUpdates отдаёт только свежие апдейты — если сообщение")
        print("было давно, отправь ещё одно.")
        return 1

    for cid, name in chats.items():
        print(f"{OK} chat_id={cid}  ({name})")
    return 0


def cmd_test_message() -> int:
    token = ask("TELEGRAM_BOT_TOKEN")
    chat_id = ask("TELEGRAM_CHAT_ID", secret=False)
    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Наблюдатель на связи. Это проверка канала доставки.",
            },
        )
    if response.status_code == 200:
        print(f"{OK} сообщение отправлено")
        return 0
    print(f"{BAD} HTTP {response.status_code}: {response.text[:300]}")
    return 1


def cmd_secrets() -> int:
    """Залить секреты в GitHub Secrets через gh.

    Значения передаются gh через stdin, а не аргументом: аргумент попал бы
    в список процессов и в историю шелла.
    """
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        print(f"{BAD} gh не авторизован. Выполни: gh auth login")
        return 1

    written = 0
    for name in SECRET_NAMES:
        value = os.environ.get(name, "").strip()
        if not value:
            value = getpass.getpass(f"{name} (пусто — пропустить): ").strip()
        if not value:
            print(f"{SKIP} {name} пропущен")
            continue
        result = subprocess.run(
            ["gh", "secret", "set", name], input=value, text=True, capture_output=True
        )
        if result.returncode == 0:
            print(f"{OK} {name} записан")
            written += 1
        else:
            print(f"{BAD} {name}: {result.stderr.strip()}")
    print(f"\nЗаписано секретов: {written}")
    return 0 if written else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["check", "chat-id", "secrets", "test-message"])
    args = ap.parse_args()
    setup_logging()

    return {
        "check": cmd_check,
        "chat-id": cmd_chat_id,
        "secrets": cmd_secrets,
        "test-message": cmd_test_message,
    }[args.command]()


if __name__ == "__main__":
    sys.exit(main())
