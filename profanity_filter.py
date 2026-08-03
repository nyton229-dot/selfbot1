"""Фильтр нецензурной лексики для VK-бота.

Задача модуля — по тексту сообщения понять, есть ли в нем мат, и вернуть
версию текста с зацензуренными словами (звездочки вместо мата).

Фильтр устойчив к типовым способам маскировки:
- латинские буквы-двойники и цифры вместо русских (xуй, п0шел, 6ля, cyka);
- спецсимволы внутри слова (х@й, б*лядь, п.и.з.д.е.ц);
- полностью скрытые буквы (х*й, с**а) — сверка со словарем по маске;
- растягивание букв (сууукаааа) и написание по буквам (х у й).
"""

from __future__ import annotations

import re
from functools import lru_cache

# --- Нормализация -----------------------------------------------------------

# Латинские буквы-двойники, цифры и символы, которыми маскируют русские буквы.
HOMOGLYPH_MAP = {
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "п",
    "o": "о", "p": "р", "r": "г", "s": "с", "t": "т", "u": "у", "v": "в",
    "w": "ш", "x": "х", "y": "у", "z": "з",
    "0": "о", "3": "з", "4": "ч", "6": "б",
    "ё": "е",
}

CYRILLIC_LETTERS = "абвгдежзийклмнопрстуфхцчшщъыьэюя"

# Символы, которыми обычно "заклеивают" буквы внутри слова (х*й, с#ка, х@й).
MASK_CHARS = "*#@$%^&_~+=|/\\"
# Обычная пунктуация по краям слова — просто отбрасывается.
STRIP_CHARS = ".,:;!?\"'`()[]{}<>«»„“-—…"


def normalize_char(ch: str) -> str:
    ch = ch.lower()
    return HOMOGLYPH_MAP.get(ch, ch)


def normalize_text(text: str) -> str:
    """Понижает регистр и заменяет буквы-двойники на кириллицу."""
    return "".join(normalize_char(ch) for ch in text)


def collapse_repeats(word: str) -> str:
    """сууукааа -> сука (для проверки, не для вывода)."""
    return re.sub(r"(.)\1+", r"\1", word)


def letters_only(word: str) -> str:
    return "".join(ch for ch in word if ch in CYRILLIC_LETTERS)


# --- Словарь корней ----------------------------------------------------------

# Регулярные выражения ищутся ВНУТРИ нормализованного слова (без не-букв,
# с уже схлопнутыми повторами, ё заменена на е).
PROFANITY_ROOT_PATTERNS = [
    # "ю" вынесена отдельно: иначе ловятся "плохую", "тихую" и т.п.
    r"ху[ейяи]",
    r"^хую",
    r"[нп][ао]хую",
    r"пизд",
    r"^бля$",
    r"^бля[дтц]",
    r"бляд",
    # семейство "еб-": гласная + еб + гласная (заеб*, уеб*, наеб*, выеб*...)
    r"[ауоеиыэюя]еб[аеиоуыя]",
    r"^еб[аеиоуыя]",
    r"^еб$",
    r"[ауоеиыэюя]еб$",
    r"[ъь]еб",
    # "ебл" только в начале слова или после гласной ("неблагодарный" не ловим).
    r"^ебл[аеоияю]",
    r"[ауоеиыэюя]ебл",
    r"^ебну",
    r"ебуч|ебун|ебарь?|ебац",
    r"пид[оа]р|пидр",
    r"педераст|педик",
    r"г[ао]ндон",
    r"мудак|мудач|мудил|мудо[зж]",
    r"залуп",
    r"^манд[аоыуе]",
    r"мандавош",
    r"шлюх|шлюш",
    r"дроч",
    r"^сук[аиу]",
    r"сцук",
    r"сучар|сучк",
    r"говн",
    r"мраз[ьи]",
]

PROFANITY_ROOT_RE = re.compile("|".join(f"(?:{p})" for p in PROFANITY_ROOT_PATTERNS))

# Обычные слова, которые случайно содержат "плохие" корни. Если слово содержит
# такой фрагмент — оно считается нормальным.
FALSE_POSITIVE_FRAGMENTS = (
    "хлеб", "греб", "погреб", "колеб", "стебл", "мебл",
    "учеб", "лечеб", "волшеб", "враждеб", "судеб", "служеб", "хвалеб", "целеб",
    "треб", "потреб", "употреб", "истреб",
    "скипидар", "психу", "барсуч", "педикюр",
    "команд", "мандарин", "мандат", "мандраж", "мандол", "норманд",
    "ебау",  # "ebay" после нормализации латиницы
)

# Явный словарь матерных слов для сверки по маске (х*й, с**а, бл***).
MASKED_DICTIONARY = (
    "хуй", "хуйня", "хуйло", "хуя", "хуе", "хуево", "хуёво", "нахуй", "похуй",
    "охуел", "охуела", "охуенно", "нихуя",
    "пизда", "пиздец", "пизды", "пиздато", "пиздабол", "распиздяй",
    "бля", "блядь", "блять", "бляди",
    "ебать", "ебал", "ебало", "ебаный", "ебаный", "ебанутый", "ебнутый",
    "заебал", "заебала", "заебись", "уебок", "уебан", "долбоеб", "еблан",
    "наебал", "проебал", "съебись", "выебон",
    "пидор", "пидорас", "пидар", "пидр",
    "гандон", "гондон", "мудак", "мудила", "мудачье",
    "залупа", "шлюха", "сука", "суки", "сучка", "сучара",
    "дрочить", "дрочер", "говно", "говнюк", "мразь", "мрази",
    "манда", "мандавошка",
)


# --- Проверка одного слова ---------------------------------------------------

def _word_core(word: str) -> str:
    """Нормализованное слово: только буквы, повторы схлопнуты."""
    return collapse_repeats(letters_only(normalize_text(word)))


def _is_false_positive(core: str) -> bool:
    return any(fragment in core for fragment in FALSE_POSITIVE_FRAGMENTS)


@lru_cache(maxsize=4096)
def is_profane_word(word: str) -> bool:
    """Проверяет одно слово (токен, выделенный по пробелам)."""
    core = _word_core(word)
    if len(core) < 2:
        return False
    if _is_false_positive(core):
        return False
    if PROFANITY_ROOT_RE.search(core):
        return True
    return _matches_masked_dictionary(word)


def _matches_masked_dictionary(word: str) -> bool:
    """Ловит слова с полностью скрытыми буквами: х*й, с**а, пи**ец.

    Строим из токена шаблон, где каждый спецсимвол — одна любая буква,
    и сверяем его с явным словарем мата.
    """
    normalized = normalize_text(word).strip(STRIP_CHARS + " ")
    if not normalized:
        return False

    pattern_parts: list[str] = []
    visible_letters = 0
    masked_positions = 0
    for ch in normalized:
        if ch in CYRILLIC_LETTERS:
            pattern_parts.append(re.escape(ch))
            visible_letters += 1
        elif ch in MASK_CHARS:
            # Маска может как заменять букву, так и просто вставляться в слово.
            pattern_parts.append("[а-яе]?")
            masked_positions += 1
        else:
            # Неожиданный символ (цифра/эмодзи внутри слова) — не маска.
            return False

    # Нужен хотя бы один скрытый символ и достаточно видимых букв,
    # чтобы "***" или "?!" не считались матом.
    if masked_positions == 0 or visible_letters < 2:
        return False

    token_re = re.compile("^" + "".join(pattern_parts) + "$")
    return any(token_re.match(bad) for bad in MASKED_DICTIONARY)


# --- Проверка всего сообщения ------------------------------------------------

def _find_spaced_runs(tokens: list[str]) -> list[tuple[int, int]]:
    """Находит написанный по буквам мат: 'х у й', 'с у к а'.

    Возвращает диапазоны [start, end) подряд идущих коротких токенов,
    которые в склеенном виде дают матерное слово.
    """
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(tokens)
    while i < n:
        core = _word_core(tokens[i])
        if not (1 <= len(core) <= 2):
            i += 1
            continue
        j = i
        merged = ""
        while j < n:
            piece = _word_core(tokens[j])
            if not (1 <= len(piece) <= 2):
                break
            merged += piece
            j += 1
        if j - i >= 2:
            merged = collapse_repeats(merged)
            if not _is_false_positive(merged) and PROFANITY_ROOT_RE.search(merged):
                runs.append((i, j))
        i = max(j, i + 1)
    return runs


def censor_text(text: str) -> tuple[bool, str]:
    """Главная функция фильтра.

    Возвращает (нашелся_ли_мат, текст_с_цензурой). Матерные слова заменяются
    на звездочки той же длины, остальной текст не меняется.
    """
    if not text or not text.strip():
        return False, text

    tokens = text.split()
    bad_indexes: set[int] = set()

    for idx, token in enumerate(tokens):
        if is_profane_word(token):
            bad_indexes.add(idx)

    for start, end in _find_spaced_runs(tokens):
        bad_indexes.update(range(start, end))

    if not bad_indexes:
        return False, text

    censored_tokens = [
        "*" * max(len(token), 3) if idx in bad_indexes else token
        for idx, token in enumerate(tokens)
    ]
    return True, " ".join(censored_tokens)


def contains_profanity(text: str) -> bool:
    return censor_text(text)[0]


def text_core(text: str) -> str:
    """Нормализованное "ядро" текста: только буквы, без масок и повторов.

    Используется для сравнения с пользовательским списком запрещенных слов:
    "П р И в 3 т" и "привет" дают одинаковое ядро.
    """
    return collapse_repeats(letters_only(normalize_text(text)))
