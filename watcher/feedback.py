"""Обратная связь читателя: реакции на карточки в Telegram.

Система говорит «пропускай» и без этого модуля никогда не узнаёт, была ли
права. Кнопки не годятся: Telegram ждёт ответа бота на нажатие за
секунды, а прогон идёт раз в шесть часов. Реакция же просто лежит в
очереди getUpdates до 24 часов — прогону остаётся её прочитать и записать
в журнал доставок, рядом с вердиктом, который она оценивает.

Сдвиг offset фиксируется только записью журнала в git: не запушилось —
следующий прогон перечитает те же обновления и придёт к тому же журналу.
Идемпотентность здесь бесплатна, как и у доставки.

Отказ этого контура не делает прогон грязным: реакции — вспомогательный
контур, а не инвариант доставки. Пинг сторожу гасят только инварианты.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

__all__ = ["FeedbackFailed", "fetch_reactions"]


class FeedbackFailed(RuntimeError):
    """Реакции не прочитаны. Прогон продолжается, offset не двигается."""


def _emoji(item: dict) -> str:
    """Реакция строкой. Неизвестный тип не исчезает, а остаётся фактом."""
    kind = item.get("type", "")
    if kind == "emoji":
        return str(item.get("emoji", ""))
    if kind == "custom_emoji":
        return f"custom:{item.get('custom_emoji_id', '')}"
    return kind or "unknown"


def fetch_reactions(
    *,
    bot_token: str,
    chat_id: str,
    offset: int | None,
    timeout_seconds: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[int, list[str]], int | None]:
    """Прочитать реакции из очереди getUpdates.

    Возвращает последнее состояние реакций по message_id и offset для
    следующего чтения. Пустой список эмодзи — реакцию сняли. Состояние
    именно последнее: поставил и передумал между прогонами — журналу
    важно то, что стоит сейчас.

    offset двигается и по чужим обновлениям: перечитывать их незачем.
    Токен в сообщение об ошибке не попадает — он живёт только в адресе.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    payload: dict = {"timeout": 0, "allowed_updates": ["message_reaction"]}
    if offset is not None:
        payload["offset"] = offset

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds), transport=transport) as client:
            response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise FeedbackFailed(f"getUpdates не ответил: {type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise FeedbackFailed(
            f"getUpdates отверг запрос: HTTP {response.status_code} — {response.text[:200]}"
        )

    # Разбор тела — тоже недоверенная операция: 200 с битым JSON или телом
    # неожиданной формы (вмешался прокси) обязан стать FeedbackFailed, а не
    # ValueError, вылетающим из прогона мимо finish() и гасящим пинг.
    try:
        data = response.json()
    except ValueError as exc:
        raise FeedbackFailed(f"ответ getUpdates не разбирается как JSON: {exc}") from exc
    updates = data.get("result") if isinstance(data, dict) else None
    if not isinstance(updates, list):
        raise FeedbackFailed("ответ getUpdates неожиданной формы: нет списка result")

    states: dict[int, list[str]] = {}
    last_id = offset

    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            last_id = max(last_id or 0, update_id + 1)
        reaction = update.get("message_reaction")
        if not isinstance(reaction, dict):
            continue
        chat = reaction.get("chat")
        if str(chat.get("id", "") if isinstance(chat, dict) else "") != str(chat_id):
            continue
        message_id = reaction.get("message_id")
        if not isinstance(message_id, int):
            continue
        # Мусор вместо списка — не вердикт о состоянии: пустой список
        # означает «реакцию сняли», и журнал стёр бы настоящую реакцию
        # на основании мусора. Не знаем состояния — пропускаем.
        new_reaction = reaction.get("new_reaction")
        if not isinstance(new_reaction, list):
            continue
        states[message_id] = [
            _emoji(item) for item in new_reaction if isinstance(item, dict)
        ]

    if states:
        log.info("реакции: обновлений %d, сообщений %d", len(updates), len(states))
    return states, last_id
