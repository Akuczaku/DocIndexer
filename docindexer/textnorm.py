# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Normalizzazione per ricerca ed evidenziazione indipendenti da maiuscole e accenti.

fold() conserva la lunghezza (un carattere -> un carattere), così le posizioni trovate nel testo
normalizzato valgono anche nel testo originale. Esempi: DOĞAN -> dogan, Yıldırım -> yildirim,
İSTANBUL -> istanbul, Şahin Çelik -> sahin celik, Łódź -> lodz, Øre -> ore.
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache

# lettere che non si scompongono con NFKD (la decomposizione Unicode non toglie il segno)
_SPECIAL = {
    "ı": "i", "ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ħ": "h", "Ħ": "h",
    "ŧ": "t", "Ŧ": "t", "ß": "s", "ẞ": "s", "æ": "a", "Æ": "a", "œ": "o", "Œ": "o", "þ": "t",
    "Þ": "t", "ð": "d", "Ð": "d", "ŀ": "l", "Ŀ": "l", "ĸ": "k", "ſ": "s",
}


@lru_cache(maxsize=4096)
def fold_char(c: str) -> str:
    if c in _SPECIAL:
        return _SPECIAL[c]
    if unicodedata.category(c) == "Co":  # simboli dei font (frecce Wingdings ecc.): separatori
        return " "
    base = unicodedata.normalize("NFKD", c)[:1] or c
    low = base.lower()[:1] or base
    return _SPECIAL.get(low, low)


def fold(text: str) -> str:
    """Minuscolo e senza accenti/diacritici, stessa lunghezza dell'originale."""
    return "".join(fold_char(c) for c in text)
