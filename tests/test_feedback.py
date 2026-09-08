"""Тесты обратной связи читателя.

Кнопки не годятся: Telegram ждёт ответа бота на нажатие за секунды, а
прогон идёт раз в шесть часов. Годятся реакции: эмодзи на карточке
следующий прогон читает из getUpdates и пишет в журнал доставок.

Сдвиг offset фиксируется только записью журнала: не запушилось — следующий
прогон перечитает те же обновления. Идемпотентность здесь бесплатна, и
тесты её охраняют.
"""

from __future__ import annotations

import json

import httpx
import pytest

from watcher.feedback import FeedbackFailed, fetch_reactions

CHAT = "7000001"


def reaction_update(uid: int, message_id: int, emojis: list[str], chat_id: int | None = None):
    return {
        "update_id": uid,
        "message_reaction": {
            "chat": {"id": chat_id if chat_id is not None else int(CHAT), "type": "private"},
            "message_id": message_id,
            "user": {"id": 5, "is_bot": False, "first_name": "R"},
            "date": 1722400000,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": e} for e in emojis],
        },
    }


def telegram(items: list[dict], seen: list[dict] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": items})

    return httpx.MockTransport(handler)


def test_reactions_are_read_and_offset_advances():
    states, offset = fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=None,
        transport=telegram([
            reaction_update(10, 42, ["👍"]),
            reaction_update(11, 43, []),
        ]),
    )
    assert states == {42: ["👍"], 43: []}
    assert offset == 12


def test_last_state_wins_for_the_same_message():
    """Поставил и передумал между прогонами — журналу важно последнее."""
    states, _ = fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=None,
        transport=telegram([
            reaction_update(10, 42, ["👍"]),
            reaction_update(11, 42, ["❤"]),
        ]),
    )
    assert states == {42: ["❤"]}


def test_foreign_chat_is_ignored():
    states, offset = fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=None,
        transport=telegram([reaction_update(10, 42, ["👍"], chat_id=999)]),
    )
    assert states == {}
    assert offset == 11, "offset двигается и по чужим: их не надо перечитывать"


def test_no_updates_keep_the_offset():
    states, offset = fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=77, transport=telegram([])
    )
    assert states == {}
    assert offset == 77


def test_request_asks_only_for_reactions_and_confirms_nothing_new():
    seen: list[dict] = []
    fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=77, transport=telegram([], seen)
    )
    assert seen[0]["allowed_updates"] == ["message_reaction"]
    assert seen[0]["offset"] == 77
    assert seen[0]["timeout"] == 0


def test_custom_emoji_stays_a_fact():
    """Неизвестный тип реакции не исчезает, а попадает в журнал строкой."""
    update = reaction_update(10, 42, [])
    update["message_reaction"]["new_reaction"] = [
        {"type": "custom_emoji", "custom_emoji_id": "5312"}
    ]
    states, _ = fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=None, transport=telegram([update])
    )
    assert states == {42: ["custom:5312"]}


def test_network_failure_raises_feedback_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна")

    with pytest.raises(FeedbackFailed):
        fetch_reactions(
            bot_token="t", chat_id=CHAT, offset=None,
            transport=httpx.MockTransport(handler),
        )


def test_telegram_error_raises_feedback_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "description": "conflict"})

    with pytest.raises(FeedbackFailed):
        fetch_reactions(
            bot_token="t", chat_id=CHAT, offset=None,
            transport=httpx.MockTransport(handler),
        )


def test_broken_json_raises_feedback_failed_not_valueerror():
    """Находка Codex: 200 с битым телом ронял прогон мимо FeedbackFailed.

    Исключение, не являющееся FeedbackFailed, вылетает из
    _collect_reactions(), а значит — из Runner.run() до finish(): отказ
    необязательного контура гасил бы пинг сторожа.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>прокси вмешался</html>")

    with pytest.raises(FeedbackFailed):
        fetch_reactions(
            bot_token="t", chat_id=CHAT, offset=None,
            transport=httpx.MockTransport(handler),
        )


def test_unexpected_json_shape_raises_feedback_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json="строка вместо объекта")

    with pytest.raises(FeedbackFailed):
        fetch_reactions(
            bot_token="t", chat_id=CHAT, offset=None,
            transport=httpx.MockTransport(handler),
        )


def test_garbage_updates_are_skipped_not_fatal():
    """Мусорный элемент в result не имеет права ронять чтение соседей."""
    states, offset = fetch_reactions(
        bot_token="t", chat_id=CHAT, offset=None,
        transport=telegram([
            "мусор",
            {"update_id": 10, "message_reaction": "не объект"},
            reaction_update(11, 42, ["👍"]),
        ]),
    )
    assert states == {42: ["👍"]}
    assert offset == 12


def test_token_never_appears_in_the_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(FeedbackFailed) as caught:
        fetch_reactions(
            bot_token="secret-token", chat_id=CHAT, offset=None,
            transport=httpx.MockTransport(handler),
        )
    assert "secret-token" not in str(caught.value)
