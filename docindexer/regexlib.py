# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Espressioni regolari "leggibili": pattern pronti, blocchi da comporre e un editor con prova dal vivo.

Nell'editor l'utente non scrive per forza la regex a mano: sceglie pattern pronti (e-mail, telefono,
IBAN...) e blocchi (cifra, lettera, ripetizioni, "oppure"...) che vengono inseriti nel punto del
cursore, e sotto vede la spiegazione in parole della regex e i risultati su un testo di prova.
"""
from __future__ import annotations

import json
import re
import tkinter as tk

import customtkinter as ctk

from . import theme
from . import dialogs as messagebox   # avvisi nel tema
from .dialogs import decorate
from .config import DATA_DIR
from .i18n import tr

PATTERNS_FILE = DATA_DIR / "regex_patterns.json"   # pattern pronti dell'utente (modificabili)

# (nome, regex, esempio) — i nomi passano da tr() quando sono mostrati
PATTERNS: list[tuple[str, str, str]] = [
    ("Indirizzo e-mail", r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}", "mario.rossi@example.com"),
    ("Telefono italiano", r"(?:\+39[ .]?)?(?:3\d{2}|0\d{1,3})[ .-]?\d{3,4}[ .-]?\d{3,4}", "+39 333 123 4567"),
    ("Telefono internazionale", r"\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}", "+90 532 123 45 67"),
    ("IBAN", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b", "IT60 X054 2811 1010 0000 0123 456"),
    ("Codice fiscale", r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", "RSSMRA80A01H501U"),
    ("Partita IVA", r"\b(?:IT ?)?\d{11}\b", "IT01234567890"),
    ("Targa italiana", r"\b[A-Z]{2} ?\d{3} ?[A-Z]{2}\b", "AB 123 CD"),
    ("Indirizzo IP (v4)", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b", "192.168.1.10"),
    ("Indirizzo web (URL)", r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", "https://www.osintinfo.net"),
    ("Data (gg/mm/aaaa)", r"\b(?:0?[1-9]|[12]\d|3[01])[/.-](?:0?[1-9]|1[0-2])[/.-](?:\d{4}|\d{2})\b", "02/03/2026"),
    ("Data (aaaa-mm-gg)", r"\b\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b", "2026-03-02"),
    ("Ora (hh:mm)", r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b", "17:24:01"),
    ("Carta di credito", r"\b(?:\d{4}[ -]?){3}\d{4}\b", "4111 1111 1111 1111"),
    ("Importo in euro", r"(?:€ ?\d[\d.]*(?:,\d{2})?|(?<![\d.])\d[\d.]*(?:,\d{2})? ?(?:€|euro|EUR))", "€ 1.250,00"),
    ("Indirizzo Bitcoin", r"\b(?:bc1[a-z0-9]{25,59}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b",
     "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"),
    ("IMEI", r"\b\d{15}\b", "356938035643809"),
    ("Passaporto / documento", r"\b[A-Z]{1,2}\d{6,9}\b", "YA1234567"),
    ("Coordinate GPS", r"-?\d{1,2}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}", "41.9028, 12.4964"),
    ("Hashtag", r"#\w+", "#osint"),
    ("Nome utente (@)", r"(?<!\w)@\w{2,}", "@utente"),
]



# Pattern predefiniti corretti: le versioni vecchie ripartivano da ogni carattere di una lunga fila di
# puntini o cifre (indici, tabelle) e su quei testi impiegavano minuti. Il (?<!...) davanti fa provare
# solo dall'inizio della fila; trovano le stesse cose. Si aggiornano anche nei pattern già salvati.
FASTER = {
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}":
        r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}",
    r"(?:€ ?\d[\d.]*(?:,\d{2})?|\d[\d.]*(?:,\d{2})? ?(?:€|euro|EUR))":
        r"(?:€ ?\d[\d.]*(?:,\d{2})?|(?<![\d.])\d[\d.]*(?:,\d{2})? ?(?:€|euro|EUR))",
}


def load_patterns() -> list[dict]:
    """Pattern pronti: quelli salvati dall'utente, oppure i predefiniti la prima volta.
    Ogni pattern è {"name", "regex", "example"}; i nomi dei predefiniti si traducono con tr()."""
    try:
        data = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
        return [dict(p, regex=FASTER.get(p["regex"], p["regex"]))
                for p in data if isinstance(p, dict) and p.get("name") and p.get("regex")]
    except (OSError, ValueError):
        return default_patterns()


def default_patterns() -> list[dict]:
    return [{"name": n, "regex": r, "example": e} for n, r, e in PATTERNS]


def save_patterns(patterns: list[dict]) -> None:
    PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATTERNS_FILE.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")


class PatternDialog(ctk.CTkToplevel):
    """Crea o modifica un pattern pronto: nome, espressione regolare, esempio (per la prova)."""

    def __init__(self, master, fonts, pattern: dict | None, on_save):
        super().__init__(master, fg_color=theme.BG)
        self.on_save = on_save
        decorate(self, tr("Modifica pattern") if pattern else tr("Nuovo pattern"), minimize=False, maximize=False)
        self.geometry("640x420")
        self.minsize(520, 340)
        try:
            self.transient(master.winfo_toplevel())
        except tk.TclError:
            pass
        p = pattern or {}
        self.vars = {}
        for i, (key, label) in enumerate((("name", "Nome"), ("regex", "Espressione regolare"),
                                          ("example", "Esempio (facoltativo)"))):
            theme.caption(self, tr(label), fonts).pack(anchor="w", padx=16, pady=(14 if i == 0 else 8, 4))
            v = tk.StringVar(value=tr(p.get(key, "")) if key == "name" else p.get(key, ""))
            e = ctk.CTkEntry(self, textvariable=v, height=32,
                             font=ctk.CTkFont(family=theme.mono_font() if key == "regex" else theme.ui_font(),
                                              size=13))
            e.pack(fill="x", padx=16)
            v.trace_add("write", lambda *_: self.check())
            self.vars[key] = v
        self.msg = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=600, font=fonts.small)
        self.msg.pack(fill="x", padx=16, pady=(8, 0))
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", side="bottom", padx=16, pady=14)
        ctk.CTkButton(btns, text=tr("Salva"), command=self.save).pack(side="right")
        ctk.CTkButton(btns, text=tr("Annulla"), fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER,
                      command=self.destroy).pack(side="right", padx=8)
        self.check()
        self.after(150, self.grab_set)

    def check(self) -> bool:
        rx, ex = self.vars["regex"].get().strip(), self.vars["example"].get()
        err = validate(rx) if rx else tr("La regex è vuota")
        if err:
            self.msg.configure(text=tr("Regex non valida: {err}", err=err), text_color=theme.ERR)
            return False
        if ex:
            ok = re.search(rx, ex, re.IGNORECASE) is not None
            self.msg.configure(text=(tr("L'esempio corrisponde alla regex.") if ok
                                     else tr("Attenzione: l'esempio non corrisponde alla regex.")),
                               text_color=theme.OK if ok else theme.WARN)
        else:
            self.msg.configure(text=explain(rx), text_color=theme.TEXT_DIM)
        return True

    def save(self):
        name = self.vars["name"].get().strip()
        if not name:
            self.msg.configure(text=tr("Scrivi un nome per il pattern."), text_color=theme.ERR)
            return
        if not self.check():
            return
        self.on_save({"name": name, "regex": self.vars["regex"].get().strip(),
                      "example": self.vars["example"].get().strip()})
        self.destroy()


# blocchi per comporre una regex: (etichetta, testo da inserire)
BLOCKS: list[tuple[str, str]] = [
    ("una cifra", r"\d"),
    ("una lettera", r"[A-Za-zÀ-ÿ]"),
    ("lettera o cifra", r"\w"),
    ("uno spazio", r"\s"),
    ("qualsiasi carattere", "."),
    ("una o più volte", "+"),
    ("zero o più volte", "*"),
    ("facoltativo", "?"),
    ("esattamente N volte", "{3}"),
    ("da N a M volte", "{2,4}"),
    ("oppure", "|"),
    ("gruppo ( )", "(?:)"),
    ("uno tra [ ]", "[]"),
    ("inizio/fine parola", r"\b"),
    ("punto letterale", r"\."),
]

# spiegazione dei pezzi più comuni di una regex, dal più lungo al più corto
_EXPLAIN = [
    (r"\d", "cifra"), (r"\D", "non cifra"), (r"\w", "lettera/cifra"), (r"\W", "non lettera"),
    (r"\s", "spazio"), (r"\S", "non spazio"), (r"\b", "confine di parola"), (r"\.", "punto"),
    (r"\+", "segno +"), (r"\-", "trattino"), (r"\(", "parentesi ("), (r"\)", "parentesi )"),
    ("(?:", "gruppo:"), ("(?<!", "non preceduto da:"), ("(?<=", "preceduto da:"), ("(?=", "seguito da:"),
    ("(?!", "non seguito da:"), ("+", "una o più volte"), ("*", "zero o più volte"), ("?", "facoltativo"),
    ("|", "OPPURE"), (".", "qualsiasi carattere"), ("^", "inizio riga"), ("$", "fine riga"),
]


def explain(pattern: str) -> str:
    """Traduzione approssimata in parole della regex (aiuta a capire cosa si sta cercando)."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        m = re.match(r"\{(\d+)(?:,(\d*))?\}", pattern[i:])
        if m:
            a, b = m.group(1), m.group(2)
            out.append(tr("{a} volte", a=a) if b is None else
                       tr("da {a} a {b} volte", a=a, b=b) if b else tr("almeno {a} volte", a=a))
            i += m.end()
            continue
        if ch == "[":
            j = pattern.find("]", i + 1)
            if j > i:
                body = pattern[i + 1:j]
                neg = body.startswith("^")
                out.append((tr("nessuno tra") if neg else tr("uno tra")) + f" [{body.lstrip('^')}]")
                i = j + 1
                continue
        for tok, words in _EXPLAIN:
            if pattern.startswith(tok, i):
                out.append(tr(words))
                i += len(tok)
                break
        else:
            if ch == "\\" and i + 1 < len(pattern):
                out.append(f"«{pattern[i + 1]}»")
                i += 2
            elif ch == ")":
                out.append(tr("fine gruppo"))
                i += 1
            else:
                j = i
                while j < len(pattern) and pattern[j] not in "\\[](){}|.*+?^$":
                    j += 1
                out.append(f"«{pattern[i:j]}»" if j > i else ch)
                i = max(j, i + 1)
    return " · ".join(out)


def validate(pattern: str) -> str | None:
    """None se la regex è valida, altrimenti il messaggio d'errore."""
    try:
        re.compile(pattern)
        return None
    except re.error as exc:
        return str(exc)


class RegexEditor(ctk.CTkToplevel):
    """Editor di regex: pattern pronti e blocchi a sinistra, regex e spiegazione al centro, prova sotto."""

    def __init__(self, master, initial: str, on_ok, fonts: theme.Fonts):
        super().__init__(master, fg_color=theme.BG)
        decorate(self, tr("Editor di espressioni regolari"))
        self.geometry("1100x760")
        self.minsize(900, 600)
        self.on_ok = on_ok
        self.fonts = fonts
        try:
            self.transient(master.winfo_toplevel())
        except tk.TclError:
            pass

        left = ctk.CTkFrame(self, fg_color=theme.PANEL, width=330, corner_radius=4,
                            border_width=1, border_color=theme.BORDER)
        left.pack(side="left", fill="y", padx=(14, 8), pady=14)
        left.pack_propagate(False)
        theme.caption(left, tr("Pattern pronti"), fonts).pack(anchor="w", padx=12, pady=(12, 4))
        self.patterns = load_patterns()
        self.lst = ctk.CTkScrollableFrame(left, fg_color="transparent", height=300)
        self.lst.pack(fill="x", padx=6)
        prow = ctk.CTkFrame(left, fg_color="transparent")
        prow.pack(fill="x", padx=8, pady=(4, 0))
        ctk.CTkButton(prow, text="+ " + tr("Nuovo pattern"), height=26, font=fonts.small,
                      command=self.new_pattern).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(prow, text=tr("Predefiniti"), height=26, font=fonts.small, fg_color=theme.NEUTRAL,
                      hover_color=theme.NEUTRAL_HOVER, command=self.reset_patterns).pack(side="left", padx=(4, 0))
        self._fill_patterns()
        theme.caption(left, tr("Blocchi"), fonts).pack(anchor="w", padx=12, pady=(12, 4))
        grid = ctk.CTkFrame(left, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for i, (label, tok) in enumerate(BLOCKS):
            ctk.CTkButton(grid, text=tr(label), height=24, font=fonts.small,
                          fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER,
                          command=lambda t=tok: self.insert(t)).grid(row=i // 2, column=i % 2, sticky="ew",
                                                                    padx=2, pady=2)
        grid.grid_columnconfigure((0, 1), weight=1)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=(0, 14), pady=14)
        theme.caption(body, tr("Espressione regolare"), fonts).pack(anchor="w")
        self.entry = ctk.CTkTextbox(body, height=70, font=ctk.CTkFont(family=theme.mono_font(), size=14),
                                    wrap="char", border_width=1, border_color=theme.BORDER)
        self.entry.pack(fill="x", pady=(4, 6))
        self.entry.insert("1.0", initial or "")
        self.entry.bind("<KeyRelease>", lambda e: self.update_view())
        self.explain_lbl = ctk.CTkLabel(body, text="", anchor="w", justify="left", wraplength=680,
                                        text_color=theme.TEXT_SOFT, font=fonts.body)
        self.explain_lbl.pack(fill="x")
        self.err_lbl = ctk.CTkLabel(body, text="", anchor="w", text_color=theme.ERR, font=fonts.small)
        self.err_lbl.pack(fill="x")

        theme.caption(body, tr("Prova su un testo"), fonts).pack(anchor="w", pady=(12, 4))
        self.sample = ctk.CTkTextbox(body, height=200, wrap="word", border_width=1, border_color=theme.BORDER)
        self.sample.pack(fill="both", expand=True)
        self.sample.insert("1.0", tr("Scrivi o incolla qui un testo di prova: i risultati si evidenziano mentre "
                                     "componi la regex."))
        self.sample.bind("<KeyRelease>", lambda e: self.update_view())
        self.count_lbl = ctk.CTkLabel(body, text="", anchor="w", text_color=theme.TEXT_DIM, font=fonts.small)
        self.count_lbl.pack(fill="x", pady=(4, 0))

        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(btns, text=tr("Usa nella ricerca"), width=170, command=self.ok).pack(side="right")
        ctk.CTkButton(btns, text=tr("Annulla"), width=110, fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER,
                      command=self.destroy).pack(side="right", padx=8)
        ctk.CTkButton(btns, text=tr("Svuota"), width=110, fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER,
                      command=lambda: (self.entry.delete("1.0", "end"), self.update_view())).pack(side="left")
        self.update_view()
        self.after(150, self.grab_set)
        self.entry.focus_set()

    # ------------------------------------------------- pattern pronti modificabili
    def _fill_patterns(self):
        """Ogni riga: nome (clic = inserisci), ✎ modifica, ✕ elimina."""
        for w in self.lst.winfo_children():
            w.destroy()
        for i, p in enumerate(self.patterns):
            row = ctk.CTkFrame(self.lst, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkButton(row, text=tr(p["name"]), anchor="w", height=26, fg_color="transparent",
                          hover_color=theme.HOVER, text_color=theme.TEXT,
                          command=lambda p=p: self.insert(p["regex"], p.get("example") or None)).pack(
                side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="✎", width=26, height=24, fg_color="transparent", hover_color=theme.HOVER,
                          text_color=theme.TEXT_SOFT, command=lambda i=i: self.edit_pattern(i)).pack(side="left")
            ctk.CTkButton(row, text="✕", width=26, height=24, fg_color="transparent", hover_color=theme.HOVER,
                          text_color=theme.ERR, command=lambda i=i: self.delete_pattern(i)).pack(side="left")

    def _store(self):
        save_patterns(self.patterns)
        self._fill_patterns()

    def new_pattern(self):
        """Nuovo pattern; se nel campo c'è già una regex la si propone (così si salva quella composta)."""
        def add(p):
            self.patterns.append(p)
            self._store()

        PatternDialog(self, self.fonts, {"name": "", "regex": self.pattern(), "example": ""}
                      if self.pattern() else None, add)

    def edit_pattern(self, i: int):
        def upd(p):
            self.patterns[i] = p
            self._store()

        PatternDialog(self, self.fonts, self.patterns[i], upd)

    def delete_pattern(self, i: int):
        if messagebox.askyesno(tr("Elimina pattern"),
                               tr("Eliminare il pattern \"{name}\"?", name=tr(self.patterns[i]["name"])), parent=self):
            del self.patterns[i]
            self._store()

    def reset_patterns(self):
        if messagebox.askyesno(tr("Predefiniti"), tr("Ripristinare i pattern predefiniti? I pattern creati o "
                                                     "modificati da te andranno persi."), parent=self):
            self.patterns = default_patterns()
            self._store()

    def pattern(self) -> str:
        return self.entry.get("1.0", "end").strip().replace("\n", "")

    def insert(self, token: str, sample: str | None = None):
        """Inserisce il pezzo nel punto del cursore; un pattern pronto, se c'è già qualcosa, va in OR."""
        current = self.pattern()
        if sample is not None and current:
            token = "|" + token
        self.entry.insert("insert", token)
        if token.endswith(("(?:)", "[]")):
            self.entry.mark_set("insert", "insert-1c")   # cursore dentro le parentesi
        if sample:
            self.sample.insert("end", "\n" + sample)
        self.entry.focus_set()
        self.update_view()

    def update_view(self):
        pat = self.pattern()
        err = validate(pat) if pat else None
        self.err_lbl.configure(text=(tr("Regex non valida: {err}", err=err) if err else ""))
        self.explain_lbl.configure(text=explain(pat) if pat and not err else "")
        tb = self.sample._textbox
        tb.tag_remove("hit", "1.0", "end")
        tb.tag_configure("hit", background="#ffd400", foreground="#000000")
        n = 0
        if pat and not err:
            text = self.sample.get("1.0", "end-1c")
            for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
                if m.end() > m.start():
                    tb.tag_add("hit", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
                    n += 1
        self.count_lbl.configure(text=tr("{n} corrispondenze nel testo di prova", n=n))

    def ok(self):
        pat = self.pattern()
        err = validate(pat) if pat else tr("La regex è vuota")
        if err:
            self.err_lbl.configure(text=tr("Regex non valida: {err}", err=err))
            return
        self.on_ok(pat)
        self.destroy()
