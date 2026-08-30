"""Тесты нормализации текста.

Смысл нормализации ровно один: не дать косметическим изменениям сборщика
выглядеть как правка содержания. Каждый тест здесь — конкретный сценарий,
в котором без нормализации система соврала бы.
"""

from __future__ import annotations

from watcher.source import normalize_text, sha256_of


def test_smart_quotes_do_not_change_hash():
    """Сборщик включил умную типографику — содержание не изменилось.

    Это главный сценарий: без этого правила все 127 советов одновременно
    поменяли бы хеш, инвариант mass_change сработал бы, и система встала
    бы с алертом «перевёрстка» на пустом месте.
    """
    plain = 'Use the "plan mode" - it helps.'
    fancy = "Use the “plan mode” — it helps."
    assert normalize_text(plain) == normalize_text(fancy)
    assert sha256_of(normalize_text(plain)) == sha256_of(normalize_text(fancy))


def test_zero_width_characters_stripped():
    """Невидимые символы из копипаста не должны быть изменением."""
    assert normalize_text("plan​mode") == normalize_text("planmode")
    assert normalize_text("a﻿b­c") == "abc"


def test_whitespace_collapsed_across_newlines_and_nbsp():
    assert normalize_text("a  \n\t b c") == "a b c"


def test_nfkc_folds_ligatures_and_ellipsis():
    assert normalize_text("ﬁle") == "file"          # U+FB01 -> fi
    assert normalize_text("eﬃcient") == "efficient"  # U+FB03 -> ffi
    assert normalize_text("wait…") == "wait..."


def test_case_is_preserved():
    """Регистр значим: заголовки советов — часть содержания."""
    assert normalize_text("Plan Mode") != normalize_text("plan mode")


def test_all_dash_variants_fold_to_ascii_hyphen():
    for dash in "–—―−‒":
        assert normalize_text(f"a{dash}b") == "a-b"


def test_leading_and_trailing_space_removed():
    assert normalize_text("   text   ") == "text"


def test_empty_input_is_safe():
    assert normalize_text("") == ""
    assert normalize_text("   \n\t ") == ""
