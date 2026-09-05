"""Тесты состояния.

Проверяют три вещи, каждая из которых при отказе ломает конкретный
критерий готовности:

    атомарная запись   -> повреждённое состояние = остановка системы
    журнал доставок    -> «одно изменение не приходит дважды»
    heartbeat          -> GitHub не отключит workflow за 60 дней тишины
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_block, make_document, make_part

from watcher.state import Heartbeat, Ledger, Snapshot, StateCorrupted, StateStore


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state", git_enabled=False)


# --------------------------------------------------------------------------
# Снапшот
# --------------------------------------------------------------------------


def test_snapshot_roundtrip_preserves_blocks(store, simple_parts):
    doc = make_document(simple_parts)
    snapshot = Snapshot.from_document(
        doc, url="https://example.test/", etag='"abc"', last_modified=None, raw_bytes=1234
    )
    store.save_snapshot(snapshot)
    loaded = store.load_snapshot()

    assert loaded is not None
    assert [p.number for p in loaded.parts] == [p.number for p in simple_parts]
    assert loaded.parts[0].blocks[0].text == simple_parts[0].blocks[0].text
    assert loaded.parts[0].blocks[0].hash == simple_parts[0].blocks[0].hash
    assert loaded.source["content_hash"] == doc.content_hash


def test_missing_snapshot_returns_none(store):
    assert store.load_snapshot() is None


def test_corrupted_snapshot_raises_instead_of_rebuilding(store):
    """Повреждённое состояние обязано ОСТАНОВИТЬ систему.

    Молча пересоздать снапшот означало бы объявить все 127 советов новыми
    и завалить Telegram. Правильная реакция — упасть с внятным сообщением;
    починка это git checkout одного файла.
    """
    store.snapshot_path.write_text("{ это не json", encoding="utf-8")
    with pytest.raises(StateCorrupted, match="git checkout"):
        store.load_snapshot()


def test_unknown_schema_version_raises(store, simple_parts):
    payload = Snapshot.from_document(
        make_document(simple_parts), url="u", etag=None, last_modified=None, raw_bytes=1
    ).to_dict()
    payload["schema_version"] = 999
    store.snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StateCorrupted, match="миграц"):
        store.load_snapshot()


def test_snapshot_json_is_sorted_for_readable_diffs(store, simple_parts):
    """Ключи отсортированы: иначе git diff состояния нечитаем."""
    store.save_snapshot(
        Snapshot.from_document(
            make_document(simple_parts), url="u", etag=None, last_modified=None, raw_bytes=1
        )
    )
    raw = store.snapshot_path.read_text(encoding="utf-8")
    assert raw.index('"assumptions"') < raw.index('"parts"') < raw.index('"schema_version"')


def test_write_leaves_no_temp_file(store, simple_parts):
    store.save_snapshot(
        Snapshot.from_document(
            make_document(simple_parts), url="u", etag=None, last_modified=None, raw_bytes=1
        )
    )
    assert list(store.dir.glob("*.tmp")) == []


# --------------------------------------------------------------------------
# Журнал доставок
# --------------------------------------------------------------------------


def test_ledger_roundtrip_and_ids(store):
    ledger = Ledger()
    ledger.add("sha256:aaa", "block_added", 22, "b-1", messages=2)
    ledger.add("sha256:bbb", "block_edited", 13, "b-2", messages=1)
    store.save_ledger(ledger)

    loaded = store.load_ledger()
    assert loaded.ids == {"sha256:aaa", "sha256:bbb"}
    assert loaded.delivered[0]["part"] == 22
    assert loaded.delivered[0]["sent_at"].endswith("Z")


def test_ledger_records_why_nothing_was_sent(store):
    """Правка ниже порога значимости попадает в журнал с причиной.

    Без записи она не оставила бы следа вообще, и на вопрос «почему по
    этому совету ничего не пришло» отвечало бы только чтение кода. С
    записью ответ лежит в `git diff state/delivered.json`.
    """
    ledger = Ledger()
    ledger.add(
        "sha256:ccc", "block_edited", 22, "b-3", messages=0,
        note="правка ниже порога значимости (ratio 0.993) — модель не вызывалась",
    )
    store.save_ledger(ledger)

    entry = store.load_ledger().delivered[0]
    assert entry["messages"] == 0
    assert "0.993" in entry["note"]


def test_ledger_trim_keeps_newest(store):
    ledger = Ledger()
    for i in range(10):
        ledger.add(f"sha256:{i}", "block_added", 1, f"b-{i}", messages=1)
    ledger.trim(keep=3)
    assert [e["event_id"] for e in ledger.delivered] == ["sha256:7", "sha256:8", "sha256:9"]


def test_minor_records_never_evict_a_delivered_id(store):
    """Мелочь не имеет права вытеснить единственную защиту от дубля.

    Сценарий: событие доставлено, но снапшот застрял (переполнение лимита
    или упавшая доставка), и пока он стоит, сайт правит опечатки. Общий
    срез по последним keep записям выбросил бы id доставленного события —
    и следующий прогон отправил бы его второй раз.
    """
    ledger = Ledger()
    ledger.add("sha256:delivered", "block_edited", 22, "b-1", messages=2)
    for i in range(600):
        ledger.add(f"sha256:minor{i}", "block_edited", 1, f"b-{i}", messages=0, note="мелочь")
    ledger.trim(keep=500)

    assert "sha256:delivered" in ledger.ids
    assert "sha256:minor599" in ledger.ids
    assert "sha256:minor0" not in ledger.ids


def test_missing_ledger_is_empty_not_error(store):
    assert store.load_ledger().ids == set()


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------


def test_heartbeat_first_run_is_due():
    assert Heartbeat().due_for_commit(interval_hours=20) is True


def test_heartbeat_not_due_right_after_commit():
    hb = Heartbeat(last_committed=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hb.due_for_commit(interval_hours=20) is False


def test_heartbeat_due_after_interval():
    """Ключевая защита от 60-дневного отключения workflow.

    Если этот тест начнёт врать, репозиторий перестанет получать коммиты в
    тихие недели, GitHub выключит расписание, и система умрёт молча.
    """
    stale = datetime.now(timezone.utc) - timedelta(hours=21)
    hb = Heartbeat(last_committed=stale.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hb.due_for_commit(interval_hours=20) is True


def test_heartbeat_broken_timestamp_forces_commit():
    """Нечитаемая метка — коммитим. Ошибаться безопаснее в сторону активности."""
    assert Heartbeat(last_committed="не дата").due_for_commit(interval_hours=20) is True


def test_model_probe_first_run_is_due():
    assert Heartbeat().due_for_probe(interval_hours=24) is True


def test_model_probe_not_due_right_after():
    """Проба стоит денег и запроса — на каждом прогоне она не нужна."""
    hb = Heartbeat(last_model_probe=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hb.due_for_probe(interval_hours=24) is False


def test_model_probe_due_after_interval():
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    hb = Heartbeat(last_model_probe=stale.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hb.due_for_probe(interval_hours=24) is True


def test_heartbeat_roundtrip(store):
    hb = Heartbeat(last_run="2026-07-28T06:00:00Z", consecutive_source_failures=2, runs_total=428)
    store.save_heartbeat(hb)
    loaded = store.load_heartbeat()
    assert loaded.consecutive_source_failures == 2
    assert loaded.runs_total == 428


# --------------------------------------------------------------------------
# Сырой HTML для вскрытия сломанного парсера
# --------------------------------------------------------------------------


def test_raw_html_roundtrip_is_compressed(store):
    html = "<html><body>" + ("<p>tip</p>" * 5000) + "</body></html>"
    store.save_raw_html(html)
    assert store.load_raw_html() == html
    # Сжатие обязано работать: файл коммитится в репозиторий каждый прогон.
    assert store.raw_path.stat().st_size < len(html) / 10


def test_missing_raw_html_returns_none(store):
    assert store.load_raw_html() is None
