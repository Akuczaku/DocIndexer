# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Lingua e alfabeto di un testo, senza librerie esterne.

  script(text)  alfabeto prevalente: latin, cyrillic, greek, arabic, hangul, thai, devanagari, tamil,
                telugu, cjk
  detect(text)  codice della lingua (it, en, ru, el...) o dell'alfabeto se la lingua non si capisce

Prima si conta a quale alfabeto appartengono le lettere; per gli alfabeti condivisi da più lingue
(latino, cirillico) si contano le parole più frequenti di ogni lingua ("il", "the", "der"...).
Serve a mostrare la lingua dei documenti, a filtrare la ricerca e a scegliere il modello OCR.
"""
from __future__ import annotations

import re
from collections import Counter

# intervalli Unicode -> alfabeto
_RANGES = (
    (0x0041, 0x024F, "latin"), (0x1E00, 0x1EFF, "latin"),
    (0x0370, 0x03FF, "greek"), (0x1F00, 0x1FFF, "greek"),
    (0x0400, 0x052F, "cyrillic"),
    (0x0600, 0x06FF, "arabic"), (0x0750, 0x077F, "arabic"), (0xFB50, 0xFDFF, "arabic"), (0xFE70, 0xFEFF, "arabic"),
    (0x0900, 0x097F, "devanagari"),
    (0x0B80, 0x0BFF, "tamil"),
    (0x0C00, 0x0C7F, "telugu"),
    (0x0E00, 0x0E7F, "thai"),
    (0x1100, 0x11FF, "hangul"), (0xAC00, 0xD7AF, "hangul"), (0x3130, 0x318F, "hangul"),
    (0x3040, 0x30FF, "cjk"), (0x4E00, 0x9FFF, "cjk"), (0x3400, 0x4DBF, "cjk"),
)

# alfabeto -> modello OCR (ocr.MODELS) che lo riconosce
SCRIPT_MODEL = {"latin": "multi", "cjk": "multi", "cyrillic": "cyrillic", "greek": "el", "arabic": "arabic",
                "hangul": "korean", "thai": "th", "devanagari": "devanagari", "tamil": "ta", "telugu": "te"}

# parole più frequenti (già senza accenti, come il testo normalizzato)
_STOP = {
    "it": "il lo la i gli le di da del della dei delle che non per con una uno sono come anche questo questa "
          "alla nel nella si ma piu essere stato hanno",
    "en": "the and of to in is that for it with as was on be by this are from at or have an which not but "
          "they you were has",
    "fr": "le la les de des du et est un une que qui dans pour pas sur au aux avec ce cette sont par plus "
          "ne se il elle nous vous",
    "de": "der die das und ist nicht ein eine zu den von mit sich des auf fur im dem auch es an werden aus "
          "er sie wir oder",
    "es": "el la los las de del y que en un una es por con no se para al lo como mas pero sus le ya o este "
          "esta son",
    "pt": "o a os as de do da dos das e que em um uma para com nao no na por se mais como mas foi ao sao "
          "esta voce",
    "sq": "dhe te ne per nga me nje eshte qe se si por edhe jane do ka u i e ky kjo jo mund ishte",
    "tr": "ve bir bu da de icin ile ne var olan gibi daha cok kadar ama sonra ben sen o biz siz olarak "
          "degil",
    "nl": "de het een en van in is dat op te zijn voor met die niet aan er ook als bij door maar om ze",
    "ro": "si de la in cu pe un o care nu este din ca sa mai sunt ce fost pentru au dar acest aceasta",
    "pl": "i w z na nie sie do jest to ze o jak a od po tak za co ale juz przez dla",
    "hr": "i je u na da se za su od ne kao sa iz ali bi ili sam smo ste to koji koja",
    "ru": "и в не на что с он я как а по это из у к все она так его но за то же вы",
    "uk": "і в не на що з він я як а по це із у до та але ти вони ми ви за його її",
    "bg": "и в на не да се за от с че е по са като това но той тя ще ли",
    "sr": "и у на да се за је су од не као са из али би или сам смо сте то који",
    "el": "και το να η της του τα με σε που την των για δεν στο ειναι ο οι στην απο θα",
}
_STOP_SETS = {k: set(v.split()) for k, v in _STOP.items()}
_LATIN = ("it", "en", "fr", "de", "es", "pt", "sq", "tr", "nl", "ro", "pl", "hr")
_CYRILLIC = ("ru", "uk", "bg", "sr")

LANG_NAMES = {
    "it": "Italiano", "en": "Inglese", "fr": "Francese", "de": "Tedesco", "es": "Spagnolo", "pt": "Portoghese",
    "sq": "Albanese", "tr": "Turco", "nl": "Olandese", "ro": "Rumeno", "pl": "Polacco", "hr": "Croato",
    "ru": "Russo", "uk": "Ucraino", "bg": "Bulgaro", "sr": "Serbo", "el": "Greco",
    "latin": "Alfabeto latino", "cyrillic": "Cirillico", "greek": "Greco", "arabic": "Arabo", "hangul": "Coreano",
    "thai": "Thailandese", "devanagari": "Devanagari", "tamil": "Tamil", "telugu": "Telugu",
    "cjk": "Cinese / giapponese",
}
_SAMPLE = 20000   # caratteri esaminati: bastano per decidere e restano veloci
_WORD = re.compile(r"[^\W\d_]+")


def script_counts(text: str) -> Counter:
    counts: Counter = Counter()
    for ch in text[:_SAMPLE]:
        if not ch.isalpha():
            continue
        o = ord(ch)
        for lo, hi, name in _RANGES:
            if lo <= o <= hi:
                counts[name] += 1
                break
    return counts


def script(text: str, min_letters: int = 20) -> str | None:
    counts = script_counts(text)
    if sum(counts.values()) < min_letters:
        return None
    return counts.most_common(1)[0][0]


def detect(text: str) -> str | None:
    """Codice della lingua, oppure l'alfabeto se non si riconosce la lingua; None se c'è troppo poco testo."""
    from .textnorm import fold

    sc = script(text)
    if sc is None:
        return None
    candidates = _LATIN if sc == "latin" else _CYRILLIC if sc == "cyrillic" else ("el",) if sc == "greek" else ()
    if not candidates:
        return sc
    words = Counter(_WORD.findall(fold(text[:_SAMPLE])))
    total = sum(words.values())
    if total < 8:
        return sc
    scores = {lang: sum(n for w, n in words.items() if w in _STOP_SETS[lang]) for lang in candidates}
    best = max(scores, key=scores.get)
    # almeno il 4% delle parole tra quelle frequenti della lingua, altrimenti solo l'alfabeto
    return best if scores[best] >= max(3, total * 0.04) else sc


def name(code: str | None) -> str:
    from .i18n import tr

    return tr(LANG_NAMES.get(code, code)) if code else ""
