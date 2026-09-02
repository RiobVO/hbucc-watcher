"""Оркестрация одного прогона.

Здесь и только здесь живёт сквозной инвариант системы:

    СНАПШОТ ПРОДВИГАЕТСЯ ВПЕРЁД РОВНО ТОГДА, КОГДА ВСЕ СОБЫТИЯ ПРОГОНА
    ПОДТВЕРЖДЁННО ДОСТАВЛЕНЫ. Во всех остальных случаях он остаётся
    прежним, и следующий прогон пересчитает диф заново.

Из одной этой строки бесплатно получается идемпотентный ретрай: журнал
доставок отфильтрует уже отправленное, а недоставленное повторится само.
Поэтому в системе нет очереди — она добавила бы то, что уже работает.

Второе правило, такое же важное:

    ПИНГ ВНЕШНЕМУ WATCHDOG ОТПРАВЛЯЕТСЯ ТОЛЬКО ПОСЛЕ ЧИСТОГО ПРОГОНА.

Внутренний отказ — сломанный парсер, недоступный источник — таким образом
становится ещё и внешне видимым. Упавший процесс не может сообщить о своей
смерти сам; молчание пинга сообщает за него.

Коды возврата: 0 — прогон чистый, 1 — что-то пошло не так.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import httpx

from watcher.analyze import (
    PROBE_OK,
    PROBE_QUOTA,
    PROBE_UNREACHABLE,
    AnalysisFailed,
    analyze,
    probe_model,
)
from watcher.config import STATE_DIR, PROMPTS_DIR, Config, ConfigError, Secrets, setup_logging
from watcher.deliver import DeliveryFailed, deliver, render, send_alert
from watcher.detect import (
    check_assumptions,
    check_diff_scale,
    check_structure,
    diff_documents,
    flag_injections,
)
from watcher.source import ParseError, SourceUnavailable, fetch, parse
from watcher.state import (
    GitError,
    Heartbeat,
    Snapshot,
    StateCorrupted,
    StateStore,
    utcnow,
)

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: Config, secrets: Secrets, store: StateStore):
        self.cfg = cfg
        self.secrets = secrets
        self.store = store
        self.heartbeat: Heartbeat = store.load_heartbeat()

    # ---------------------------------------------------------------- утилиты

    def alert(self, *lines: str, signature: str | None = None) -> None:
        """Сообщить о сбое, но не повторять один и тот же алерт бесконечно."""
        if signature is not None:
            if self.heartbeat.last_violation_signature == signature:
                log.info("алерт с той же подписью уже отправлялся — молчу")
                return
            self.heartbeat.last_violation_signature = signature
        send_alert(
            lines,
            bot_token=self.secrets.telegram_bot_token,
            chat_id=self.secrets.telegram_chat_id,
        )

    def ping_watchdog(self) -> None:
        """Сообщить внешнему наблюдателю, что прогон был чистым."""
        if not self.secrets.healthcheck_url:
            return
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
                client.get(self.secrets.healthcheck_url)
            log.info("watchdog: пинг отправлен")
        except httpx.HTTPError as exc:
            # Не критично: пропущенный пинг вызовет ложную тревогу, а не
            # пропущенную. Ошибаться безопаснее в эту сторону.
            log.warning("watchdog: пинг не прошёл: %s", exc)

    def probe_model_if_due(self) -> bool:
        """Проверить доступность модели на прогоне, где её не будили.

        Это заплата на слепом пятне второго инварианта. Прогон без
        изменений на сайте до модели не доходит и пингует сторожа как
        чистый — значит кончившаяся квота или отозванный ключ иначе
        обнаружились бы только на первом настоящем событии.

        Возвращает False только при приговоре аккаунту: тогда пинг не
        уходит, и сторож сообщает о поломке снаружи. Временная помеха
        (сеть, лимит частоты, 5xx) прогон не портит и метку не двигает —
        проба не состоялась, а не провалилась.
        """
        model_cfg = self.cfg.section("model")
        if not self.heartbeat.due_for_probe(model_cfg["probe_interval_hours"]):
            return True

        outcome = probe_model(api_key=self.secrets.openai_api_key, model=model_cfg["name"])
        if outcome == PROBE_UNREACHABLE:
            log.warning("проба модели не состоялась — повторю следующим прогоном")
            return True

        self.heartbeat.last_model_probe = utcnow()
        if outcome == PROBE_OK:
            log.info("проба модели: ключ жив, квота есть")
            return True

        if outcome == PROBE_QUOTA:
            self.alert(
                "На аккаунте провайдера кончилась квота.",
                "Разборы не придут, пока баланс не пополнен. Ключ при этом валиден.",
                "",
                "Найдено пробой по расписанию: сайт не менялся, и без неё это",
                "выяснилось бы только на первом настоящем изменении.",
                signature="model_quota",
            )
        else:
            self.alert(
                f"Модель недоступна: {outcome}.",
                "Ключ отозван, сменился или потерял доступ к модели из конфига.",
                "Разборы не придут, пока это не починено.",
                signature=f"model_{outcome}",
            )
        return False

    def finish(self, ok: bool, *, model_used: bool = False) -> int:
        """Сохранить heartbeat, при необходимости зафиксировать, пингануть."""
        self.heartbeat.last_run = utcnow()
        self.heartbeat.runs_total += 1

        # Только на чистом прогоне без модели: на грязном алерт уже ушёл, а
        # после настоящего разбора доступность модели только что доказана.
        if ok and not model_used:
            ok = self.probe_model_if_due()

        interval = self.cfg.get("state", "heartbeat_commit_interval_hours")
        if self.heartbeat.due_for_commit(interval):
            self.heartbeat.last_committed = utcnow()

        self.store.save_heartbeat(self.heartbeat)

        try:
            if self.heartbeat.last_committed == self.heartbeat.last_run:
                # Коммит heartbeat — это ещё и защита от отключения
                # scheduled workflow за 60 дней без активности в репозитории.
                self.store.commit_and_push(
                    [self.store.heartbeat_path], "chore(state): heartbeat"
                )
        except GitError as exc:
            log.error("не удалось зафиксировать heartbeat: %s", exc)
            ok = False

        if ok:
            self.ping_watchdog()
        return 0 if ok else 1

    # ------------------------------------------------------------- сценарии

    def bootstrap(self) -> int:
        """Первый запуск: записать базовую линию, НЕ рассылая разборы.

        Без этого режима первый прогон отправил бы 127 разборов подряд.
        Режим намеренно ручной (workflow_dispatch), а не автоматический при
        отсутствии снапшота: автоматика здесь означала бы, что случайно
        удалённый снапшот приводит к лавине сообщений.
        """
        src = self.cfg.section("source")
        result = fetch(
            src["url"],
            timeout_seconds=src["timeout_seconds"],
            retries=src["retries"],
            backoff_seconds=src["retry_backoff_seconds"],
            user_agent=src["user_agent"],
        )
        if result.html is None:
            self.alert("bootstrap: сервер ответил 304, нечего разбирать")
            return self.finish(ok=False)

        doc = parse(result.html)
        snapshot = Snapshot.from_document(
            doc,
            url=src["url"],
            etag=result.etag,
            last_modified=result.last_modified,
            raw_bytes=result.raw_bytes,
        )
        self.store.save_snapshot(snapshot)
        self.store.save_raw_html(result.html)
        self.store.save_ledger(self.store.load_ledger())
        self.store.commit_and_push(
            [self.store.snapshot_path, self.store.ledger_path, self.store.raw_path],
            "chore(state): bootstrap baseline",
        )

        send_alert(
            [
                "Базовая линия установлена.",
                f"Частей: {len(doc.parts)}",
                f"Советов: {doc.tips_count}",
                f"Символов: {doc.text_chars}",
                "",
                "Разборы по существующему содержимому НЕ рассылались.",
                "Дальше приходят только изменения.",
            ],
            bot_token=self.secrets.telegram_bot_token,
            chat_id=self.secrets.telegram_chat_id,
        )
        return self.finish(ok=True)

    def run(self) -> int:
        src = self.cfg.section("source")
        detect_cfg = self.cfg.section("detect")

        # --- состояние ---
        try:
            snapshot = self.store.load_snapshot()
        except StateCorrupted as exc:
            self.alert(
                "Состояние повреждено, прогон остановлен.",
                str(exc),
                "",
                "Автовосстановление намеренно отсутствует: пересоздание снапшота "
                "означало бы разбор всех 127 советов заново.",
                signature="state_corrupted",
            )
            return self.finish(ok=False)

        if snapshot is None:
            self.alert(
                "Снапшота нет — система не знает, от чего считать изменения.",
                "Запусти workflow вручную с bootstrap=true, чтобы записать базовую линию.",
                signature="no_snapshot",
            )
            return self.finish(ok=False)

        # --- уровень 1 каскада: условный GET ---
        try:
            result = fetch(
                src["url"],
                etag=snapshot.source.get("etag"),
                last_modified=snapshot.source.get("last_modified"),
                timeout_seconds=src["timeout_seconds"],
                retries=src["retries"],
                backoff_seconds=src["retry_backoff_seconds"],
                user_agent=src["user_agent"],
            )
        except SourceUnavailable as exc:
            self.heartbeat.consecutive_source_failures += 1
            threshold = self.cfg.get("alerts", "source_failures_before_alert")
            log.warning(
                "источник недоступен, подряд: %d", self.heartbeat.consecutive_source_failures
            )
            if self.heartbeat.consecutive_source_failures >= threshold:
                self.alert(
                    f"Источник недоступен {self.heartbeat.consecutive_source_failures} прогонов подряд.",
                    str(exc),
                    signature="source_down",
                )
            return self.finish(ok=False)

        self.heartbeat.consecutive_source_failures = 0

        if result.status == 304:
            log.info("изменений нет (304), модель не будим")
            return self.finish(ok=True)

        if result.raw_bytes < detect_cfg["min_body_bytes"]:
            self.alert(
                f"Тело ответа {result.raw_bytes} байт — меньше порога "
                f"{detect_cfg['min_body_bytes']}. Похоже на заглушку или капчу.",
                signature="body_too_small",
            )
            return self.finish(ok=False)

        # Сырой HTML сохраняем ДО разбора: если парсер упадёт, файл нужен
        # именно для вскрытия этого падения.
        self.store.save_raw_html(result.html or "")

        # --- парсинг ---
        try:
            doc = parse(result.html or "")
        except ParseError as exc:
            self.alert(
                "Парсер не смог разобрать документ — вероятно, сайт перевёрстан.",
                str(exc),
                "",
                f"Сырой HTML сохранён: {self.store.raw_path.name}",
                "Состояние не тронуто, модель не вызывалась.",
                signature=f"parse_error:{exc}",
            )
            return self.finish(ok=False)

        # --- уровень 2 каскада: хеш контента ---
        if doc.content_hash == snapshot.source.get("content_hash"):
            log.info("контент не изменился (хеш совпал), модель не будим")
            self._refresh_source_meta(snapshot, result)
            return self.finish(ok=True)

        # --- инварианты до дифа ---
        violations = check_structure(
            doc,
            previous_shape=snapshot.shape,
            min_parts_absolute=detect_cfg["min_parts_absolute"],
            max_text_shrink_pct=detect_cfg["max_text_shrink_pct"],
        )
        if violations:
            self.alert(
                "Обнаружение сломалось — проверки целостности не прошли.",
                *[f"- {v}" for v in violations],
                "",
                f"Сырой HTML: {self.store.raw_path.name}",
                "Состояние не тронуто, модель не вызывалась, разборы не отправлялись.",
                signature="structure:" + ";".join(v.code for v in violations),
            )
            return self.finish(ok=False)

        # --- уровни 3 и 4: диф ---
        events = diff_documents(
            snapshot.parts, doc.parts, threshold=detect_cfg["match_threshold"]
        )

        scale_violations = check_diff_scale(
            events,
            previous_blocks_count=snapshot.blocks_count,
            max_changed_pct=detect_cfg["max_changed_blocks_pct"],
        )
        if scale_violations:
            self.alert(
                "Слишком масштабное изменение — это перевёрстка, а не правки.",
                *[f"- {v}" for v in scale_violations],
                "",
                f"Сырой HTML: {self.store.raw_path.name}",
                "Разборы НЕ отправлялись, состояние не тронуто.",
                signature="scale:" + ";".join(v.code for v in scale_violations),
            )
            return self.finish(ok=False)

        # Допущения — не поломка, а сообщение «то, на чём построена
        # система, изменилось».
        for note in check_assumptions(doc, snapshot.assumptions):
            self.alert("Допущение перестало выполняться.", note, signature=f"assumption:{note}")

        # Успешный разбор структуры — снимаем прошлую подпись нарушения.
        self.heartbeat.last_violation_signature = ""

        ledger = self.store.load_ledger()
        fresh = [e for e in events if e.event_id not in ledger.ids]
        log.info("событий: %d, из них новых: %d", len(events), len(fresh))

        if not fresh:
            # Контент изменился, но всё содержательное уже доставлено
            # (например, поменялась только служебная разметка). Двигаем
            # снапшот: иначе диф будет пересчитываться вечно.
            self._advance(snapshot, doc, result, note="без новых событий")
            return self.finish(ok=True)

        flag_injections(fresh)

        cap = detect_cfg["max_events_per_run"]
        overflow = len(fresh) - cap
        batch = fresh[:cap]
        if overflow > 0:
            log.warning("событий больше лимита: %d, переношу %d", len(fresh), overflow)

        delivered, failed = self._deliver_batch(batch, snapshot, ledger, overflow)

        if failed:
            threshold = self.cfg.get("alerts", "model_failures_before_alert")
            self.heartbeat.consecutive_model_failures += 1
            if self.heartbeat.consecutive_model_failures >= threshold:
                self.alert(
                    f"Разбор или доставка падают {self.heartbeat.consecutive_model_failures} прогонов подряд.",
                    *failed[:5],
                    "",
                    "Состояние не двигается — недоставленное повторится следующим прогоном.",
                    signature="delivery_failing",
                )
            return self.finish(ok=False)

        self.heartbeat.consecutive_model_failures = 0

        # Инвариант: снапшот вперёд только при полной доставке.
        if overflow > 0:
            log.info("остаток %d событий — снапшот не двигаю", overflow)
            return self.finish(ok=True, model_used=True)

        self._advance(snapshot, doc, result, note=f"доставлено {delivered} событий")
        return self.finish(ok=True, model_used=True)

    # ------------------------------------------------------------ внутреннее

    def _deliver_batch(self, batch, snapshot, ledger, overflow: int):
        """Разобрать и доставить события по одному.

        Журнал пишется и ПУШИТСЯ после каждой доставки, а не в конце цикла.
        Раннер эфемерный: файл, записанный на диск, но не доехавший до
        удалённой ветки, равен потерянному, — а журнал это единственная
        гарантия против повторной отправки уже разобранного.
        """
        model_cfg = self.cfg.section("model")
        tg_cfg = self.cfg.section("telegram")
        system_prompt = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
        task_template = (PROMPTS_DIR / "analysis.md").read_text(encoding="utf-8")
        keep = self.cfg.get("state", "ledger_keep")

        delivered = 0
        failed: list[str] = []

        for index, event in enumerate(batch, 1):
            log.info("событие %d/%d: %s", index, len(batch), event.headline)
            try:
                analysis = analyze(
                    event,
                    snapshot.parts,
                    api_key=self.secrets.openai_api_key,
                    cfg_model=model_cfg,
                    system_prompt=system_prompt,
                    task_template=task_template,
                    user_agent=self.cfg.get("source", "user_agent"),
                )
            except AnalysisFailed as exc:
                failed.append(f"{event.headline}: разбор не получен — {exc}")
                break

            text = render(analysis, event)
            if overflow > 0 and index == len(batch):
                text += (
                    f"\n\nЕщё {overflow} изменений в очереди — придут следующим прогоном."
                )

            try:
                messages = deliver(
                    text,
                    bot_token=self.secrets.telegram_bot_token,
                    chat_id=self.secrets.telegram_chat_id,
                    chunk_chars=tg_cfg["chunk_chars"],
                    parse_mode=tg_cfg["parse_mode"],
                )
            except DeliveryFailed as exc:
                failed.append(f"{event.headline}: не доставлено — {exc}")
                break

            ledger.add(event.event_id, event.kind, event.part_number, event.bid, messages)
            ledger.trim(keep)
            self.store.save_ledger(ledger)
            try:
                self.store.commit_and_push(
                    [self.store.ledger_path],
                    f"chore(state): delivered {event.kind} part-{event.part_number}",
                )
            except GitError as exc:
                # Доставлено, но факт доставки не сохранён. Продолжать
                # опасно: следующий прогон продублирует всё, что уедет
                # дальше в этом цикле.
                failed.append(f"журнал доставок не сохранён: {exc}")
                break

            delivered += 1

        return delivered, failed

    def _advance(self, snapshot: Snapshot, doc, result, *, note: str) -> None:
        """Продвинуть снапшот. Вызывается только при полной доставке."""
        src = self.cfg.section("source")
        new_snapshot = Snapshot.from_document(
            doc,
            url=src["url"],
            etag=result.etag,
            last_modified=result.last_modified,
            raw_bytes=result.raw_bytes,
        )
        new_snapshot.assumptions = {**snapshot.assumptions, "last_verified": utcnow()}
        self.store.save_snapshot(new_snapshot)
        self.heartbeat.last_change_detected = utcnow()
        self.store.commit_and_push(
            [self.store.snapshot_path, self.store.raw_path],
            f"chore(state): snapshot advanced ({note})",
        )
        log.info("снапшот продвинут: %s", note)

    def _refresh_source_meta(self, snapshot: Snapshot, result) -> None:
        """Обновить ETag без продвижения содержимого.

        Сайт пересобрали, текст не изменился. Новый ETag надо запомнить,
        иначе условный GET перестанет давать 304 и мы будем качать и
        разбирать документ каждые шесть часов вместо одного HEAD-подобного
        запроса.
        """
        if result.etag == snapshot.source.get("etag"):
            return
        snapshot.source["etag"] = result.etag
        snapshot.source["last_modified"] = result.last_modified
        snapshot.source["fetched_at"] = utcnow()
        self.store.save_snapshot(snapshot)
        try:
            self.store.commit_and_push(
                [self.store.snapshot_path], "chore(state): refresh etag"
            )
        except GitError as exc:
            log.warning("не удалось сохранить обновлённый ETag: %s", exc)


def main() -> int:
    setup_logging(verbose=os.environ.get("VERBOSE") == "1")
    try:
        cfg = Config.load()
        secrets = Secrets.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    store = StateStore(STATE_DIR, git_enabled=os.environ.get("NO_GIT") != "1")
    runner = Runner(cfg, secrets, store)

    if os.environ.get("BOOTSTRAP") == "1":
        log.info("режим bootstrap: записываю базовую линию без рассылки разборов")
        return runner.bootstrap()

    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
