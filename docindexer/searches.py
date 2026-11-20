# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Ricerche: esecuzione condivisa (pagina Ricerca, ricerca rapida, ricerche salvate) e ricerche salvate.

Una ricerca è descritta da un dizionario (spec) con le stesse opzioni della pagina Ricerca:
  query, regex, advanced, exact, in_names, any_order, fuzzy (0-4), important, sources, folders
  (vuoto = tutte le cartelle monitorate) e filters (testi come scritti nei campi: date_from, date_to,
  size_min, size_max, date_field, lang).

Ricerche salvate (saved_searches.json nella cartella dei dati): nome, spec, avviso attivo o no,
file già visti e file nuovi. Con l'avviso attivo DocIndexer le riesegue in background quando l'indice
cambia e segnala (notifica vicino all'orologio) i file nuovi che corrispondono.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import time
import uuid

from .config import DATA_DIR
from .i18n import tr

SAVED_PATH = DATA_DIR / "saved_searches.json"

DEFAULT_SPEC = {"query": "", "regex": False, "advanced": False, "exact": False, "in_names": False,
                "any_order": False, "fuzzy": 0, "important": False, "sources": ["text", "archive", "db"],
                "folders": [], "filters": {}}


# ------------------------------------------------------------------ filtri
def parse_filters(raw: dict | None) -> dict:
    """Filtri scritti dall'utente -> valori per Database._folder_filter. ValueError se non validi."""
    raw = raw or {}

    def day(text: str, end: bool):
        text = (text or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m", "%Y"):
            try:
                d = _dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(tr("Data non valida: {text} (usa aaaa-mm-gg)", text=text))
        if end:   # "al": fino alla fine di quel giorno / mese / anno
            if fmt == "%Y":
                d = d.replace(year=d.year + 1)
            elif fmt == "%Y-%m":
                d = (d.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
            else:
                d += _dt.timedelta(days=1)
        return d.timestamp()

    def mb(text: str):
        text = (text or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            return int(float(text) * 1048576)
        except ValueError:
            raise ValueError(tr("Dimensione non valida: {text} (in MB, es. 0.5)", text=text)) from None

    return {"date_field": "created" if raw.get("date_field") == "created" else "modified",
            "date_from": day(raw.get("date_from", ""), False), "date_to": day(raw.get("date_to", ""), True),
            "size_min": mb(raw.get("size_min", "")), "size_max": mb(raw.get("size_max", "")),
            "lang": raw.get("lang") or None}


# --------------------------------------------------------------- esecuzione
class Result:
    def __init__(self, rows: list[tuple[dict, list]], groups: list, fuzzy_used: int = 0):
        self.rows = rows              # [(riga, termini da evidenziare)]
        self.groups = groups
        self.fuzzy_used = fuzzy_used  # 1: nessun risultato esatto, mostrati quelli con 1 errore (auto_fuzzy)


def iter_regex(db, spec: dict, folders_all: list[str], limit: int = 500):
    """Ricerca regex di `spec` un risultato per volta (usata dal processo separato, vedi regexworker)."""
    spec = {**DEFAULT_SPEC, **spec}
    q = spec["query"].strip()
    folders = [f for f in (spec["folders"] or folders_all) if f in folders_all] or folders_all
    if not q or not folders:
        return
    pattern = " ".join(x.strip() for x in q.splitlines() if x.strip())
    yield from db.iter_regex(pattern, folders, limit=limit, important=bool(spec["important"]),
                             filters=parse_filters(spec["filters"]), sources=tuple(spec["sources"]) or ("text",))


def run(db, spec: dict, folders_all: list[str], limit: int = 500, stop: threading.Event | None = None,
        auto_fuzzy: bool = False) -> Result:
    """Esegue una ricerca. auto_fuzzy: se non trova nulla riprova tollerando 1 errore (battitura o OCR)."""
    from . import preview
    from . import query as query_mod

    spec = {**DEFAULT_SPEC, **spec}
    q = spec["query"].strip()
    folders = [f for f in (spec["folders"] or folders_all) if f in folders_all] or folders_all
    if not q or not folders:
        return Result([], [])
    filters = parse_filters(spec["filters"])
    sources = tuple(spec["sources"]) or ("text",)
    imp = bool(spec["important"])
    if spec["regex"]:
        pattern = " ".join(x.strip() for x in q.splitlines() if x.strip())
        rows = db.search_regex(pattern, folders, limit=limit, stop=stop, important=imp, filters=filters,
                               sources=sources)
        return Result([(r, [r.get("matches") or []]) for r in rows], [])

    def once(fuzzy: int) -> Result:
        plan = query_mod.build(q, advanced=spec["advanced"], exact=spec["exact"], any_order=spec["any_order"],
                               fuzzy=fuzzy, vocabulary=db.vocabulary, whole_line_phrase=spec["exact"])
        rows = db.search_fts(plan.fts, folders, limit=limit, important=imp, filters=filters, sources=sources)
        if spec["in_names"]:
            rows = db.search_names([preview.term_patterns(g) for g in plan.groups], folders,
                                   important=imp, filters=filters) + rows
        return Result([(r, plan.groups) for r in rows], plan.groups)

    res = once(int(spec["fuzzy"] or 0))
    if not res.rows and auto_fuzzy and not spec["fuzzy"] and not spec["advanced"]:
        alt = once(1)
        if alt.rows:
            alt.fuzzy_used = 1
            return alt
    return res


def describe(spec: dict) -> str:
    """Riassunto di una ricerca in una riga (per l'elenco delle ricerche salvate)."""
    spec = {**DEFAULT_SPEC, **spec}
    q = " | ".join(x.strip() for x in spec["query"].splitlines() if x.strip())
    opts = []
    if spec["regex"]:
        opts.append(tr("Regex"))
    if spec["advanced"]:
        opts.append(tr("Avanzata"))
    if spec["exact"]:
        opts.append(tr("Cerca frase esatta"))
    if spec["fuzzy"]:
        opts.append(f"fuzzy {spec['fuzzy']}")
    if spec["important"]:
        opts.append(tr("Solo documenti importanti"))
    return q + (f"   ({', '.join(opts)})" if opts else "")


# ----------------------------------------------------------- ricerche salvate
class SavedSearches:
    """Elenco delle ricerche salvate, letto e scritto in saved_searches.json (thread-safe)."""

    def __init__(self):
        self._lock = threading.RLock()
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                data = json.loads(SAVED_PATH.read_text(encoding="utf-8"))
                self.items = [d for d in data if isinstance(d, dict) and d.get("spec")]
            except (OSError, ValueError):
                self.items = []

    def save(self) -> None:
        with self._lock:
            SAVED_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SAVED_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(SAVED_PATH)

    def add(self, name: str, spec: dict, alert: bool) -> dict:
        with self._lock:
            item = {"id": uuid.uuid4().hex[:12], "name": name.strip() or tr("Ricerca"), "spec": spec,
                    "alert": bool(alert), "seen": None, "new": [], "created": time.time(), "checked": None}
            self.items.append(item)
            self.save()
            return item

    def get(self, sid: str) -> dict | None:
        return next((i for i in self.items if i["id"] == sid), None)

    def remove(self, sid: str) -> None:
        with self._lock:
            self.items = [i for i in self.items if i["id"] != sid]
            self.save()

    def update(self, sid: str, **fields) -> None:
        with self._lock:
            item = self.get(sid)
            if item:
                item.update(fields)
                self.save()

    def new_count(self) -> int:
        return sum(len(i.get("new") or []) for i in self.items if i.get("alert"))

    def check(self, db, folders_all: list[str], stop: threading.Event | None = None) -> list[tuple[dict, int]]:
        """Riesegue le ricerche con avviso: ritorna [(ricerca, quanti file nuovi)].
        La prima volta i file trovati diventano "già visti" senza avviso."""
        found = []
        with self._lock:
            todo = [dict(i) for i in self.items if i.get("alert")]
        for item in todo:
            if stop is not None and stop.is_set():
                break
            try:
                res = run(db, item["spec"], folders_all, limit=5000, stop=stop)
            except Exception:  # noqa: BLE001  ricerca non più valida: si salta
                continue
            ids = {r["id"] for r, _g in res.rows}
            with self._lock:
                cur = self.get(item["id"])
                if cur is None:
                    continue
                if cur.get("seen") is None:
                    cur["seen"] = sorted(ids)
                    cur["checked"] = time.time()
                    continue
                seen = set(cur["seen"])
                fresh = ids - seen - set(cur.get("new") or [])
                cur["seen"] = sorted(seen | ids)
                cur["checked"] = time.time()
                if fresh:
                    cur["new"] = sorted(set(cur.get("new") or []) | fresh)
                    found.append((cur, len(fresh)))
        with self._lock:
            self.save()
        return found

    def mark_seen(self, sid: str) -> None:
        self.update(sid, new=[])


_NAME_RX = re.compile(r"\s+")


def suggest_name(spec: dict) -> str:
    q = _NAME_RX.sub(" ", spec.get("query", "")).strip()
    return q[:40] + ("…" if len(q) > 40 else "")
