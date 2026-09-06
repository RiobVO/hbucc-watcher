"""Тесты автопроверки разборов.

Зачем она нужна. Правка кода проверяется тестом за секунду; правка промта
— живым прогоном за $0.16 и полторы минуты, поэтому её делают один раз и
считают сделанной. Починка `pitfalls` подтверждена ровно одним прогоном,
то есть статистически это анекдот.

Проверка кодом превращает каждое живое событие в бесплатные данные о том,
слушается ли модель промта. Она ничего не блокирует: разбор уходит
читателю в любом случае, а замечания ложатся в журнал доставок.

Проверяется ровно то, что промт требует буквально, а не то, что кажется
разумным: ссылки только в `sources`, ориентиры по объёму, запрет на
канцелярит и маркетинговую лексику, «не удалось подтвердить» вместе с
записью в `unconfirmed`.
"""

from __future__ import annotations

from watcher.analyze import Analysis, Layers, Verdict, Windows
from watcher.quality import check

DOMAINS = ["x.com", "docs.claude.com", "code.claude.com"]


def make_analysis(**overrides) -> Analysis:
    data = {
        "headline": "Хоткей на запуск Claude Code через лончер",
        "what_it_is": "Совет про то, как быстрее открывать сессию.",
        "how_it_works": "Лончер держит команду запуска и выполняет её в новом окне.",
        "example": "Нажми сочетание клавиш, назначенное в свойствах ярлыка.",
        "how_to_verify": "Откроется новое окно терминала с запущенным claude.",
        "pitfalls": ["Сочетание может быть занято системой."],
        "related": ["AutoHotkey"],
        "layers": Layers(
            original_author="В посте только про сам биндинг.",
            site_author="Связку с параллельными сессиями дописал автор сайта.",
            official_docs="Документация про лончеры не говорит.",
            my_conclusion="Полезно ровно настолько, насколько бесит искать окно.",
        ),
        "windows": Windows(status="works", detail="Путей и хоткеев тут нет."),
        "verdict": Verdict(worth_it="maybe", why="Смотря сколько у тебя окон."),
        "action": "Назначь сочетание в свойствах ярлыка.",
        "sources": ["https://x.com/example/status/1234567890"],
        "unconfirmed": ["Связка с параллельными сессиями — обобщение автора сайта."],
        "anomalies": [],
    }
    data.update(overrides)
    return Analysis(**data)


def test_clean_analysis_has_no_complaints():
    """Разбор, написанный по промту, замечаний не собирает.

    Иначе проверка обесценится за три прогона: шумящий сигнал перестают
    читать раньше, чем он успевает поймать первую настоящую ошибку.
    """
    assert check(make_analysis(), allowed_domains=DOMAINS) == []


def test_link_inside_a_text_field_is_reported():
    """«Адреса живут только в sources» — прямая цитата из промта."""
    found = check(
        make_analysis(how_it_works="Подробности на https://docs.claude.com/en/hooks."),
        allowed_domains=DOMAINS,
    )
    assert any("how_it_works" in note and "ссылк" in note for note in found)


def test_markdown_link_is_reported_too():
    found = check(
        make_analysis(what_it_is="Смотри [документацию](https://docs.claude.com)."),
        allowed_domains=DOMAINS,
    )
    assert any("what_it_is" in note for note in found)


def test_pitfall_repeating_a_claim_is_reported():
    """Один факт не стоит одновременно в pitfalls и unconfirmed.

    Живой разбор Part 22/6: из четырёх ловушек две были не ловушками, а
    дублем — тот же факт стоял в разборе четырежды.
    """
    claim = "Совместимость Opus 5 с Auto Mode не подтверждена документацией."
    found = check(
        make_analysis(pitfalls=[claim], unconfirmed=[claim]),
        allowed_domains=DOMAINS,
    )
    assert any("дубл" in note for note in found)


def test_close_but_distinct_wording_is_not_reported():
    """Похожие темы — не дубль. Порог обязан оставлять место разным мыслям."""
    found = check(
        make_analysis(
            pitfalls=["На Windows сочетание перехватывает сама система."],
            unconfirmed=["Что хоткей помогает вести несколько сессий параллельно."],
        ),
        allowed_domains=DOMAINS,
    )
    assert found == []


def test_headline_over_the_limit_is_reported():
    found = check(make_analysis(headline="я" * 81), allowed_domains=DOMAINS)
    assert any("заголовок" in note for note in found)


def test_slop_from_the_forbidden_list_is_reported():
    """Промт перечисляет запрещённые обороты поимённо — их и ищем."""
    found = check(
        make_analysis(what_it_is="Claude Code является мощным инструментом."),
        allowed_domains=DOMAINS,
    )
    assert any("является" in note for note in found)
    assert any("мощный инструмент" in note for note in found)


def test_source_outside_the_whitelist_is_reported():
    """Модель не могла прочитать страницу вне белого списка — значит не читала."""
    found = check(
        make_analysis(sources=["https://medium.com/@someone/post"]),
        allowed_domains=DOMAINS,
    )
    assert any("medium.com" in note for note in found)


def test_unconfirmed_docs_without_a_claim_is_reported():
    """Сказал «не удалось подтвердить» — утверждение обязано быть в unconfirmed."""
    found = check(
        make_analysis(
            layers=Layers(
                original_author="В посте про биндинг.",
                site_author="Автор сайта ничего не дописывал.",
                official_docs="Не удалось подтвердить по официальной документации.",
                my_conclusion="Проверять нечего.",
            ),
            unconfirmed=[],
        ),
        allowed_domains=DOMAINS,
    )
    assert any("unconfirmed" in note for note in found)


def test_too_many_pitfalls_is_reported():
    """«pitfalls до четырёх пунктов» — ориентир по объёму из промта."""
    found = check(
        make_analysis(pitfalls=[f"Ловушка номер {i}." for i in range(6)]),
        allowed_domains=DOMAINS,
    )
    assert any("pitfalls" in note and "6" in note for note in found)
