# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Costruzione della ricerca: dalla textarea alla query FTS5 e ai modelli per l'evidenziazione.

Ogni riga è una ricerca a sé (righe in OR). In una riga:
  parola            la parola (anche come inizio parola, o intera con "parola esatta")
  "frase di parole" le parole una dopo l'altra
Opzioni:
  any_order  le parole di una frase possono stare in qualsiasi ordine ("uno due tre" trova anche
             "due tre uno"), sempre una accanto all'altra
  fuzzy 1-4  ricerca approssimata: ogni parola trova anche le parole del testo che distano al massimo
             N modifiche (distanza di Levenshtein: lettera aggiunta, tolta o cambiata), tollerando anche
             gli scambi tipici dell'OCR (0/o, 1/l, rn/m, cl/d...)
Modalità avanzata: la riga è passata a FTS5 così com'è (AND, OR, NOT, NEAR, "frase", radice*).

Il risultato contiene la query FTS5 e, per ogni riga, i termini da evidenziare: stringhe (gestite
da preview.term_patterns) oppure espressioni regolari già pronte (frasi permutate, varianti fuzzy).
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Callable

from .textnorm import fold

_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
MAX_ORDER_WORDS = 6        # oltre, le permutazioni sarebbero troppe (720)
MAX_COMBINATIONS = 400     # limite alle frasi generate (permutazioni x varianti fuzzy)
MAX_VARIANTS = 60          # varianti fuzzy per parola


@dataclass
class QueryPlan:
    fts: str
    groups: list[list] = field(default_factory=list)   # per riga: termini (str o re.Pattern)
    variants: dict[str, list[str]] = field(default_factory=dict)  # parola -> varianti fuzzy trovate


def parse_line(line: str) -> list[tuple[str, list[str]]]:
    """[("word", [w]) | ("phrase", [w1, w2, ...])] con le parole normalizzate."""
    items = []
    for phrase, word in re.findall(r'"([^"]*)"|([^\W_]+)', line):
        if phrase:
            words = [fold(w) for w in re.findall(r"[^\W_]+", phrase)]
            if words:
                items.append(("phrase" if len(words) > 1 else "word", words))
        elif word:
            items.append(("word", [fold(word)]))
    return items


def _fold_advanced(line: str) -> str:
    return re.sub(r"\w+", lambda m: m.group(0) if m.group(0) in _FTS_OPERATORS else fold(m.group(0)), line)


def _phrase_regex(seqs: list[list[str]], whole_last: bool) -> re.Pattern:
    alts = [r"\W+".join(re.escape(w) for w in seq) for seq in seqs]
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + ")" + (r"(?!\w)" if whole_last else ""))


def build(query: str, *, advanced: bool = False, exact: bool = False, any_order: bool = False,
          fuzzy: int = 0, vocabulary: Callable[[], list[str]] | None = None,
          whole_line_phrase: bool = False) -> QueryPlan:
    """whole_line_phrase ("Cerca frase esatta"): ogni riga è una frase unica di parole intere,
    anche senza virgolette."""
    from .preview import search_terms  # import qui per evitare il ciclo preview <-> query

    plan = QueryPlan(fts="")
    star = "" if exact else "*"
    variants_cache: dict[str, list[str]] = {}
    vocab: list[str] | None = None

    def variants(w: str) -> list[str]:
        nonlocal vocab
        if not fuzzy:
            return [w]
        if w not in variants_cache:
            if vocab is None:
                vocab = vocabulary() if vocabulary else []
            variants_cache[w] = fuzzy_matches(w, vocab, fuzzy) or [w]
        return variants_cache[w]

    lines_fts = []
    for line in query.splitlines():
        line = line.strip()
        if not line:
            continue
        if advanced:
            lines_fts.append(f"({_fold_advanced(line)})")
            plan.groups.append(search_terms(line))
            continue
        fts_items, hl = [], []
        items = parse_line(line)
        if whole_line_phrase:
            words = [w for _k, ws in items for w in ws]
            items = [("phrase" if len(words) > 1 else "word", words)] if words else []
        for kind, words in items:
            if kind == "word" and not fuzzy:
                fts_items.append(f'"{words[0]}"{star}')
                hl.append(words[0])
                continue
            orders = [list(p) for p in itertools.permutations(words)] if (
                any_order and kind == "phrase" and len(words) <= MAX_ORDER_WORDS) else [words]
            seqs = []
            for order in orders:
                for combo in itertools.product(*(variants(w) for w in order)):
                    seqs.append(list(combo))
                    if len(seqs) >= MAX_COMBINATIONS:
                        break
                if len(seqs) >= MAX_COMBINATIONS:
                    break
            # le varianti fuzzy sono parole intere del testo: niente prefisso
            last = "" if fuzzy else star
            fts_items.append("(" + " OR ".join('"' + " ".join(s) + '"' + last for s in seqs) + ")")
            hl.append(_phrase_regex(seqs, whole_last=bool(fuzzy) or exact))
        if fts_items:
            lines_fts.append("(" + " ".join(fts_items) + ")")
            plan.groups.append(hl)
    plan.fts = " OR ".join(lines_fts)
    plan.variants = {w: v for w, v in variants_cache.items()}
    return plan


# scambi tipici dell'OCR: lettere e cifre che si confondono. Si confrontano anche le parole "ridotte"
# a una forma comune, così "c0ntratto", "contrafto" o "rnodulo" trovano "contratto" e "modulo"
_OCR_SEQ = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("ii", "u"), ("ri", "n"))
_OCR_CHARS = str.maketrans({"0": "o", "1": "l", "i": "l", "j": "l", "7": "l", "f": "t", "5": "s", "8": "b",
                            "3": "e", "4": "a", "9": "g", "q": "g"})
_ocr_cache: dict[int, tuple[object, list[str]]] = {}


def ocr_form(word: str) -> str:
    for a, b in _OCR_SEQ:
        word = word.replace(a, b)
    return word.translate(_OCR_CHARS)


def _ocr_vocab(vocab: list[str]) -> list[str]:
    """Forme OCR del vocabolario, calcolate una volta per vocabolario."""
    cached = _ocr_cache.get(id(vocab))
    if cached is None or cached[0] is not vocab:
        _ocr_cache.clear()
        cached = (vocab, [ocr_form(t) for t in vocab])
        _ocr_cache[id(vocab)] = cached
    return cached[1]


def fuzzy_matches(word: str, vocab: list[str], max_dist: int, ocr: bool = True) -> list[str]:
    """Parole del vocabolario a distanza di Levenshtein <= max_dist (la più vicina per prima).
    ocr=True: anche le parole che, ridotte alla forma comune degli scambi OCR, distano al massimo
    max_dist - 1 (una lettera letta male non conta come errore)."""
    from rapidfuzz import process
    from rapidfuzz.distance import Levenshtein

    # parole molto corte: una tolleranza alta trova di tutto, la si limita
    max_dist = min(max_dist, max(1, len(word) // 2))
    cands = [t for t in vocab if abs(len(t) - len(word)) <= max_dist + 1]
    found = {t: d for t, d, _i in process.extract(word, cands, scorer=Levenshtein.distance,
                                                   score_cutoff=max_dist, limit=MAX_VARIANTS)}
    if ocr and len(word) >= 4:
        forms = _ocr_vocab(vocab)
        target = ocr_form(word)
        idx = [i for i, f in enumerate(forms) if abs(len(f) - len(target)) <= max_dist]
        for _f, d, k in process.extract(target, [forms[i] for i in idx], scorer=Levenshtein.distance,
                                        score_cutoff=max_dist - 1, limit=MAX_VARIANTS):
            t = vocab[idx[k]]
            found[t] = min(found.get(t, 99), d + 0.5)
    return [t for t, _d in sorted(found.items(), key=lambda x: (x[1], x[0]))][:MAX_VARIANTS]
