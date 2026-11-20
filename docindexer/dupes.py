# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Duplicati e quasi-duplicati.

Identici: stesso contenuto (stesso hash SHA-256) in più posti; il db li conosce già.
Simili:   documenti con testo quasi uguale: una versione corretta, lo stesso testo in PDF e in Word,
          una scansione rifatta. Spesso il testo viene dall'OCR e due copie dello stesso foglio non
          sono mai uguali lettera per lettera (rumore, intestazioni lette diversamente).

Si lavora in due passaggi:
  1. impronta SimHash di 64 bit per documento, calcolata all'indicizzazione sulle coppie di parole
     (solo parole di almeno 3 lettere: le cifre e i frammenti dell'OCR non contano). Due testi
     diversi differiscono in circa 32 bit su 64, due versioni dello stesso testo in pochi bit.
     Il confronto di tutte le coppie si fa con numpy (XOR e conteggio dei bit), a blocchi.
  2. le coppie candidate (impronte vicine) si verificano confrontando davvero i testi (rapidfuzz,
     somiglianza tra 0 e 100%): la percentuale mostrata è questa.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .textnorm import fold

VERSION = "2"          # cambia se cambia il modo di calcolare l'impronta (il db la ricalcola)
MIN_CHARS = 300        # testi più corti: impronta poco affidabile, non si calcola
MAX_CHARS = 400_000    # basta l'inizio dei documenti molto lunghi
CANDIDATE_DIST = 14    # bit diversi al massimo per verificare una coppia (testi diversi: ~32)
COMPARE_CHARS = 8000   # caratteri confrontati nella verifica
DEFAULT_MIN = 85       # somiglianza minima predefinita (%)
_WORD = re.compile(r"[^\W\d_]{3,}")


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text[:MAX_CHARS]))


def simhash(text: str) -> int | None:
    """Impronta SimHash a 64 bit (con segno, per SQLite) sull'insieme delle coppie di parole consecutive."""
    if len(text) < MIN_CHARS:
        return None
    import numpy as np

    w = words(text)
    if len(w) < 20:
        return None
    pairs = {w[i] + " " + w[i + 1] for i in range(len(w) - 1)}
    hashes = np.frombuffer(b"".join(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest() for s in pairs),
                           dtype=np.uint8).reshape(-1, 8)
    bits = np.unpackbits(hashes, axis=1).astype(np.int64)            # (n, 64), 1 = bit acceso
    acc = (bits * 2 - 1).sum(axis=0)                                  # +1 se acceso, -1 se spento
    value = 0
    for b in acc:
        value = (value << 1) | int(b > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


def distance(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def text_similarity(a: str, b: str) -> float:
    """Somiglianza 0-100 tra due testi già normalizzati (parole separate da spazi)."""
    from rapidfuzz import fuzz

    return float(fuzz.ratio(a[:COMPARE_CHARS], b[:COMPARE_CHARS]))


def similar_groups(items: list[tuple[str, int]], get_text, min_similarity: int = DEFAULT_MIN, stop=None,
                   progress=None) -> list[list[tuple[str, int]]]:
    """items = [(sha256, simhash)], get_text(sha256) -> testo del documento.
    Ritorna i gruppi di contenuti simili (almeno 2) come [(sha256, somiglianza % con il più vicino)]."""
    import numpy as np

    n = len(items)
    if n < 2:
        return []
    vals = np.array([h for _s, h in items], dtype=np.int64).view(np.uint64)
    cands: list[tuple[int, int]] = []
    step = 1000
    for start in range(0, n, step):
        if stop is not None and stop.is_set():
            return []
        if progress:
            progress(start / n * 0.3)
        block = vals[start:start + step]
        d = np.bitwise_count(block[:, None] ^ vals[None, start:])     # solo le coppie (i, j>i)
        rows, cols = np.nonzero(d <= CANDIDATE_DIST)
        cands += [(start + r, start + c) for r, c in zip(rows.tolist(), cols.tolist()) if c > r]

    cache: dict[int, str] = {}

    def norm(i: int) -> str:
        if i not in cache:
            cache[i] = " ".join(words(get_text(items[i][0]) or ""))
        return cache[i]

    parent = list(range(n))
    best = [0.0] * n

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for k, (i, j) in enumerate(cands):
        if stop is not None and stop.is_set():
            return []
        if progress and k % 50 == 0:
            progress(0.3 + 0.7 * k / len(cands))
        sim = text_similarity(norm(i), norm(j))
        if sim >= min_similarity:
            best[i] = max(best[i], sim)
            best[j] = max(best[j], sim)
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        if best[i] >= min_similarity:
            groups[find(i)].append(i)
    return [[(items[i][0], round(best[i])) for i in g] for g in groups.values() if len(g) > 1]


SHORT_MIN = 10         # sotto questi caratteri un testo non dice nulla (pagina vuota, una sigla)
SHORT_MAX_DOCS = 30000


def _short_norm(text: str) -> str:
    # testi corti: si tengono anche numeri e parole brevi ("Prova 123" e "Prova 1234" sono quasi uguali)
    return " ".join(fold(text).split())[:COMPARE_CHARS]


def short_groups(items: list[tuple[str, str]], min_similarity: int = DEFAULT_MIN, stop=None,
                 progress=None) -> list[list[tuple[str, int]]]:
    """Documenti con poco testo (senza impronta): confronto diretto di tutte le coppie con rapidfuzz.cdist
    (in C, su tutti i core), a blocchi. items = [(sha256, testo)]."""
    import numpy as np
    from rapidfuzz import fuzz, process

    items = [(s, _short_norm(t)) for s, t in items[:SHORT_MAX_DOCS]]
    items = [(s, t) for s, t in items if len(t) >= SHORT_MIN]
    n = len(items)
    if n < 2:
        return []
    texts = [t for _s, t in items]
    parent = list(range(n))
    best = [0] * n

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    step = 1000
    for start in range(0, n, step):
        if stop is not None and stop.is_set():
            return []
        if progress:
            progress(start / n)
        m = process.cdist(texts[start:start + step], texts[start:], scorer=fuzz.ratio, score_cutoff=min_similarity,
                          dtype=np.uint8, workers=-1)
        rows, cols = np.nonzero(m)
        for r, c in zip(rows.tolist(), cols.tolist()):
            i, j = start + r, start + c
            if j <= i:
                continue
            sim = int(m[r, c])
            best[i], best[j] = max(best[i], sim), max(best[j], sim)
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        if best[i] >= min_similarity:
            groups[find(i)].append(i)
    return [[(items[i][0], best[i]) for i in g] for g in groups.values() if len(g) > 1]
