# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Anteprima dei documenti originali.

- PDF: render della pagina con PyMuPDF e termini cercati evidenziati
- Immagini: l'immagine (il fotogramma giusto per i TIFF multipagina)
- Word / PowerPoint: convertiti in PDF con Office (COM) in una cartella temporanea di Windows,
  trattati come PDF e cancellati appena si passa a un altro documento o si chiude il programma.
  Nessuna copia dei documenti resta sul disco.
- Altri formati: testo
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import unicodedata
from pathlib import Path

from . import detect as dt
from .config import CACHE_DIR, AppConfig

# file temporanei delle anteprime (PDF convertiti, immagini estratte): una cartella per processo
TEMP_ROOT = Path(tempfile.gettempdir()) / "DocIndexer_anteprime"
TEMP_DIR = TEMP_ROOT / str(os.getpid())


def temp_path(name: str) -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR / name


def delete_temp(paths) -> None:
    """Cancella file temporanei di anteprima (se un file è ancora aperto si riprova alla chiusura)."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_temp(all_processes: bool = False) -> None:
    """Alla chiusura: cancella la cartella temporanea di questo processo. All'avvio (all_processes): anche
    quelle lasciate da processi chiusi male e le vecchie anteprime in cache\\ delle versioni precedenti."""
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    if not all_processes:
        return
    try:
        import psutil

        for d in TEMP_ROOT.iterdir() if TEMP_ROOT.is_dir() else []:
            if not (d.name.isdigit() and psutil.pid_exists(int(d.name))):
                shutil.rmtree(d, ignore_errors=True)
    except (ImportError, OSError):
        pass
    for old in ("anteprime", "immagini"):
        shutil.rmtree(CACHE_DIR / old, ignore_errors=True)

WORD_TYPES = {dt.DOCX, dt.DOC, dt.RTF, dt.ODT}
_EMBEDDED_LABEL = re.compile(r"\(([^()\\/]+\.(?:png|jpe?g|jfif|bmp|gif|tiff?|webp))\)$", re.IGNORECASE)


def embedded_image(path: Path, doc_type: str, label: str | None, sha256: str, tag: str = "") -> Path | None:
    """Per un risultato trovato con l'OCR di un'immagine incollata in un documento Office/ODF/EPUB
    (etichetta "immagine N (image2.jpeg)"): estrae quell'immagine nella cache e ne ritorna il percorso.
    Così l'anteprima mostra l'immagine con le parole trovate evidenziate."""
    import zipfile

    from .extractors import EMBEDDED_MEDIA

    m = _EMBEDDED_LABEL.search(label or "")
    if not m or doc_type not in EMBEDDED_MEDIA:
        return None
    name = m.group(1).lower()
    out = temp_path(f"{tag}{sha256[:16]}_{m.group(1)}")
    if out.is_file():
        return out
    try:
        with zipfile.ZipFile(path) as zf:
            member = next((i for i in zf.infolist() if i.filename.lower().startswith(EMBEDDED_MEDIA[doc_type])
                           and i.filename.lower().rsplit("/", 1)[-1] == name), None)
            if member is None:
                return None
            out.write_bytes(zf.read(member))
            return out
    except (zipfile.BadZipFile, OSError):
        return None
SLIDE_TYPES = {dt.PPTX, dt.PPT}
SHEET_TYPES = {dt.XLSX, dt.XLS, dt.ODS}
_office_lock = threading.Lock()

_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def search_terms(query: str) -> list[str]:
    """Termini della ricerca da evidenziare (senza operatori FTS5). Il testo tra virgolette è una
    frase: si evidenzia solo dove le parole sono una dopo l'altra."""
    out, seen = [], set()
    for phrase, word in re.findall(r'"([^"]*)"|([^\W_]+)', query.replace("*", " ")):
        if phrase:
            words = re.findall(r"[^\W_]+", phrase)
            term = " ".join(words)
            if not words or (len(words) == 1 and len(term) < 2):
                continue
        else:
            term = word
            if len(term) < 2 or term in _OPERATORS:
                continue
        if term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


# un colore per ogni riga della ricerca (sfondo dell'evidenziazione)
PALETTE = ["#ffd400", "#4dd2ff", "#7ee36b", "#ff8fc7", "#ffa64d", "#b99bff", "#ff6b6b", "#3ee0c0",
           "#c8e64c", "#9ab8ff"]


def color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def term_groups(query: str) -> list[list[str]]:
    """Termini da evidenziare per ogni riga non vuota della ricerca (stesso ordine dei colori)."""
    return [search_terms(line) for line in query.splitlines() if line.strip()]


from .textnorm import fold  # noqa: E402  minuscolo senza accenti, stessa lunghezza (Ğ = G, ı = i, ...)


# "Cerca parola esatta": se attivo un termine deve essere una parola intera (eran sì, erano no).
# Lo imposta la schermata di ricerca prima di ogni ricerca.
exact_words = False


class RawRegex:
    """Espressione regolare scritta dall'utente (ricerca Regex), usata per evidenziare: si cerca nel testo
    così com'è, come fa la ricerca, e in un solo passaggio si trovano tutte le occorrenze.
    (Prima si evidenziavano al massimo 50 testi trovati, uno per volta: con migliaia di codici fiscali
    diversi molti restavano senza colore e su testi lunghi la finestra restava ferma a lungo.)"""

    def __init__(self, pattern: str):
        self.pattern = pattern
        self.rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)

    def __eq__(self, other):
        return isinstance(other, RawRegex) and other.pattern == self.pattern

    def __hash__(self):
        return hash(("RawRegex", self.pattern))


def term_patterns(terms: list[str], exact: bool | None = None) -> list[re.Pattern]:
    """Un termine corrisponde a inizio parola (come la ricerca per prefisso di FTS5), oppure alla
    parola intera se è attiva la ricerca esatta; maiuscole e accenti sono ignorati e si
    evidenziano solo i caratteri del termine."""
    exact = exact_words if exact is None else exact
    tail = r"(?!\w)" if exact else ""
    pats = []
    for t in terms:
        if isinstance(t, (re.Pattern, RawRegex)):  # già pronto (frasi in qualsiasi ordine, fuzzy, regex)
            pats.append(t)
            continue
        words = fold(t).split()
        if words:
            # frase: parole consecutive separate da spazi o punteggiatura (anche " | " tra celle)
            pats.append(re.compile(r"(?<!\w)" + r"\W+".join(re.escape(w) for w in words) + tail))
    return pats


def find_spans(text: str, pats: list[re.Pattern]) -> list[tuple[int, int]]:
    folded = None
    out = []
    for p in pats:
        if isinstance(p, RawRegex):
            out += [(m.start(), m.end()) for m in p.rx.finditer(text) if m.end() > m.start()]
            continue
        if folded is None:
            folded = fold(text)
        out += [(m.start(), m.end()) for m in p.finditer(folded)]
    return out


def find_hits(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """Posizioni (inizio, fine) dei termini trovati nel testo."""
    return sorted(find_spans(text, term_patterns(terms)))


# -------------------------------------------------------------- conversione
def preview_source(cfg: AppConfig, stored: Path, doc_type: str, sha256: str, tag: str = "") -> tuple[str, Path]:
    """Ritorna (tipo_anteprima, percorso) senza convertire: 'pdf', 'image', 'sheet', 'office' o 'text'.
    I fogli Excel non passano da Office (file enormi bloccavano Excel su "Pubblicazione in corso"):
    vengono disegnati direttamente come tabella."""
    if doc_type == dt.PDF:
        return "pdf", stored
    if doc_type == dt.IMAGE:
        return "image", stored
    if doc_type in (dt.XLSX, dt.XLS):
        return "sheet", stored
    if doc_type in WORD_TYPES | SLIDE_TYPES:
        return "office", temp_path(f"{tag}{sha256}.pdf")
    return "text", stored


def convert_to_pdf(src: Path, doc_type: str, out: Path) -> Path:
    """Converte un documento Office in PDF con l'applicazione Office installata."""
    if out.is_file():
        return out
    import pythoncom
    import win32com.client

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.pdf")
    src_s, tmp_s = str(src.resolve()), str(tmp.resolve())
    with _office_lock:
        pythoncom.CoInitialize()
        try:
            if doc_type in WORD_TYPES:
                app = win32com.client.DispatchEx("Word.Application")
                app.Visible = False
                app.DisplayAlerts = 0
                try:
                    doc = app.Documents.Open(src_s, ReadOnly=True, AddToRecentFiles=False,
                                             ConfirmConversions=False, Visible=False)
                    doc.ExportAsFixedFormat(OutputFileName=tmp_s, ExportFormat=17)  # 17 = PDF
                    doc.Close(SaveChanges=False)
                finally:
                    app.Quit()
            elif doc_type in SLIDE_TYPES:
                app = win32com.client.DispatchEx("PowerPoint.Application")
                pres = app.Presentations.Open(src_s, ReadOnly=True, Untitled=True, WithWindow=False)
                try:
                    pres.SaveAs(tmp_s, 32)  # 32 = PDF
                finally:
                    pres.Close()
                    if app.Presentations.Count == 0:
                        app.Quit()
            elif doc_type in SHEET_TYPES:
                app = win32com.client.DispatchEx("Excel.Application")
                app.Visible = False
                app.DisplayAlerts = False
                try:
                    wb = app.Workbooks.Open(src_s, ReadOnly=True)
                    wb.ExportAsFixedFormat(0, tmp_s)  # 0 = PDF
                    wb.Close(SaveChanges=False)
                finally:
                    app.Quit()
            else:
                raise ValueError(f"Conversione non prevista per {doc_type}")
        finally:
            pythoncom.CoUninitialize()
    tmp.replace(out)
    return out


# ------------------------------------------------------------------ render
def pdf_page_count(path: Path) -> int:
    import pymupdf

    with pymupdf.open(path) as doc:
        return doc.page_count


def ocr_marks(boxes: list, groups: list[list[str]]) -> list[tuple[int, tuple]]:
    """Riquadri OCR (coordinate 0..1) delle parole che iniziano con un termine, con l'indice del gruppo."""
    # le parole dell'OCR sono in ordine di lettura: si ricompone il testo per trovare anche le frasi,
    # poi ogni corrispondenza si riporta sui riquadri delle parole che tocca
    text, spans = "", []
    for b in boxes:
        if text:
            text += " "
        spans.append((len(text), len(text) + len(b[0]), b))
        text += b[0]
    marks = []
    for gi, terms in enumerate(groups):
        for s, e in find_spans(text, term_patterns(terms)):
            for w0, w1, b in spans:
                a, z = max(s, w0), min(e, w1)
                if a >= z:
                    continue
                word, (x0, y0, x1, y1) = b[0], b[1:5]
                n = len(word) or 1
                # l'OCR dà il riquadro della parola intera: si prende la parte dei caratteri trovati
                marks.append((gi, (x0 + (x1 - x0) * (a - w0) / n, y0, x0 + (x1 - x0) * (z - w0) / n, y1)))
    return marks


def draw_marks(img, rects: list[tuple[int, tuple]]):
    """Disegna i riquadri (in pixel) con il colore del gruppo, semitrasparenti."""
    from PIL import Image, ImageColor, ImageDraw

    if not rects:
        return img
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for gi, (x0, y0, x1, y1) in rects:
        rgb = ImageColor.getrgb(color(gi))
        box = [x0 - 2, y0 - 1, x1 + 2, y1 + 1]
        radius = max(2, int((box[3] - box[1]) * 0.35))
        draw.rounded_rectangle(box, radius=radius, fill=rgb + (120,), outline=rgb + (255,))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def render_pdf_page(path: Path, index: int, width: int, groups: list[list[str]], boxes: list | None = None):
    """Render della pagina `index` (0-based) larga `width` pixel; i termini di ogni gruppo
    (riga della ricerca) sono evidenziati con il colore di quella riga."""
    import pymupdf
    from PIL import Image, ImageColor, ImageDraw

    with pymupdf.open(path) as doc:
        index = max(0, min(index, doc.page_count - 1))
        page = doc[index]
        zoom = max(0.1, width / page.rect.width)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        marks = pdf_page_marks(page, groups, boxes)
    W, H = img.size
    rects = [(gi, (b[0] * W, b[1] * H, b[2] * W, b[3] * H)) for gi, b in marks]
    return draw_marks(img, rects), len(rects)


def pdf_page_marks(page, groups: list[list[str]], boxes: list | None = None) -> list[tuple[int, tuple]]:
    """Occorrenze di una pagina PDF in coordinate 0..1: dai riquadri OCR salvati se la pagina
    è stata letta con OCR, altrimenti dalle parole del testo del PDF (normalizzate, senza accenti)."""
    if boxes:
        return ocr_marks(boxes, groups)
    pw, ph = page.rect.width, page.rect.height
    lines = []  # (testo della riga, riquadro di ogni carattere)
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            # i segni diacritici separati (combinanti) si scartano: restano lettere base allineate ai riquadri
            chars = [ch for span in line.get("spans", []) for ch in span.get("chars", [])
                     if not unicodedata.combining(ch["c"][:1] or " ")]
            if chars:
                lines.append(("".join(ch["c"] for ch in chars), [ch["bbox"] for ch in chars]))
    marks = []
    for gi, terms in enumerate(groups):
        pats = term_patterns(terms)
        for text, bboxes in lines:
            for s, e in find_spans(text, pats):
                bb = bboxes[s:e]
                marks.append((gi, (min(b[0] for b in bb) / pw, min(b[1] for b in bb) / ph,
                                   max(b[2] for b in bb) / pw, max(b[3] for b in bb) / ph)))
    return marks


def reading_order(marks: list[tuple[int, tuple]]) -> list[tuple[int, tuple]]:
    """Ordina le occorrenze dall'alto in basso e da sinistra a destra (righe con tolleranza)."""
    return sorted(marks, key=lambda m: (round(m[1][1] * 80), m[1][0]))


def image_frame_count(path: Path) -> int:
    from PIL import Image

    with Image.open(path) as im:
        return getattr(im, "n_frames", 1)


def render_image(path: Path, index: int, width: int, groups: list[list[str]] | None = None,
                 boxes: list | None = None):
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(max(0, min(index, im.n_frames - 1)))
        img = ImageOps.exif_transpose(im.convert("RGB"))
    ratio = width / img.width
    img = img.resize((max(1, width), max(1, int(img.height * ratio))), Image.LANCZOS)
    rects = []
    if boxes and groups:
        W, H = img.size
        rects = [(gi, (b[0] * W, b[1] * H, b[2] * W, b[3] * H)) for gi, b in ocr_marks(boxes, groups)]
    return draw_marks(img, rects), len(rects)


# ------------------------------------------------------------- testo lungo
def wrap_for_display(text: str, width: int = 160) -> str:
    """Il testo salvato è su una sola riga: Tk diventa lentissimo con righe molto lunghe.
    Per la visualizzazione si sostituisce uno spazio ogni ~width caratteri con un a capo,
    senza cambiare la lunghezza (le posizioni delle evidenziazioni restano valide)."""
    chars = list(text)
    last = 0
    for i, ch in enumerate(chars):
        if ch == "\n":                  # a capo già presenti (metadati, una voce per riga): si riparte da qui
            last = i
        elif ch == " " and i - last >= width:
            chars[i] = "\n"
            last = i
    return "".join(chars)


# ------------------------------------------------------------------ fogli
SHEET_MAX_ROWS = 50000      # righe lette per foglio (oltre si tronca)
SHEET_MAX_COLS = 60
SHEET_PAGE_ROWS = 150       # righe per "pagina" dell'anteprima: i fogli lunghi si sfogliano a pagine
_sheet_cache: dict = {}


def _fmt_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    # a capo dentro la cella -> spazio; NFC per le lettere accentate scritte in forma scomposta
    return unicodedata.normalize("NFC", " ".join(str(v).split()))


def load_sheets(path: Path, doc_type: str) -> list[tuple[str, list[list[str]], int]]:
    """Legge i fogli (nome, righe non vuote, righe totali) direttamente dal file, senza Office.
    Il risultato resta in cache per non rileggere il file a ogni zoom o cambio pagina."""
    key = (str(path), path.stat().st_mtime)
    if key in _sheet_cache:
        return _sheet_cache[key]
    sheets = []
    if doc_type == dt.XLSX:
        import openpyxl

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                rows, total = [], 0
                for row in ws.iter_rows(values_only=True):
                    cells = [_fmt_cell(v) for v in row[:SHEET_MAX_COLS]]
                    if not any(cells):
                        continue
                    total += 1
                    if len(rows) < SHEET_MAX_ROWS:
                        rows.append(cells)
                sheets.append((ws.title, rows, total))
        finally:
            wb.close()
    elif doc_type == dt.XLS:
        import os
        import xlrd
        import xlrd.book

        strict = xlrd.book.unicode
        xlrd.book.unicode = lambda b, enc: b.decode(enc, "replace")
        try:
            with open(os.devnull, "w") as null:
                book = xlrd.open_workbook(str(path), on_demand=True, logfile=null)
        finally:
            xlrd.book.unicode = strict
        try:
            for i in range(book.nsheets):
                sh = book.sheet_by_index(i)
                rows, total = [], 0
                for r in range(sh.nrows):
                    cells = [_fmt_cell(v) for v in sh.row_values(r)[:SHEET_MAX_COLS]]
                    if not any(cells):
                        continue
                    total += 1
                    if len(rows) < SHEET_MAX_ROWS:
                        rows.append(cells)
                sheets.append((sh.name, rows, total))
        finally:
            book.release_resources()
    else:
        raise ValueError("formato foglio non supportato")
    _sheet_cache.clear()
    _sheet_cache[key] = sheets
    return sheets


def sheet_pages(path: Path, doc_type: str) -> list[tuple[int, int]]:
    """Pagine dell'anteprima: (indice foglio, prima riga) a blocchi di SHEET_PAGE_ROWS righe."""
    pages = []
    for si, (_name, rows, _total) in enumerate(load_sheets(path, doc_type)):
        for start in range(0, max(1, len(rows)), SHEET_PAGE_ROWS):
            pages.append((si, start))
    return pages


def sheet_first_page(path: Path, doc_type: str, sheet_index: int) -> int:
    for i, (si, _start) in enumerate(sheet_pages(path, doc_type)):
        if si == sheet_index:
            return i
    return 0


def sheet_page_label(path: Path, doc_type: str, page: int) -> str:
    pages = sheet_pages(path, doc_type)
    si, start = pages[max(0, min(page, len(pages) - 1))]
    name, rows, _total = load_sheets(path, doc_type)[si]
    return f"{name}: righe {start + 1}-{min(start + SHEET_PAGE_ROWS, len(rows))} di {len(rows)}"


def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_sheet(path: Path, doc_type: str, page: int, width: int, groups: list[list[str]]):
    """Disegna una pagina del foglio come tabella; il testo trovato ha lo sfondo
    del colore della riga di ricerca."""
    from PIL import Image

    full, _marks = _sheet_drawing(path, doc_type, page, groups)
    if width and abs(width - full.width) > 2:
        return full.resize((max(1, width), max(1, int(full.height * width / full.width))), Image.LANCZOS)
    return full


_sheet_img_cache: dict = {}


def _sheet_drawing(path: Path, doc_type: str, page: int, groups: list[list[str]]):
    key = (str(path), page, tuple(tuple(g) for g in groups), exact_words)
    hit = _sheet_img_cache.get(key)
    if hit is None:
        hit = _draw_sheet(path, doc_type, page, groups)
        if len(_sheet_img_cache) > 40:
            _sheet_img_cache.clear()
        _sheet_img_cache[key] = hit
    return hit


def sheet_marks(path: Path, doc_type: str, page: int, groups: list[list[str]]) -> list[tuple[int, tuple]]:
    return _sheet_drawing(path, doc_type, page, groups)[1]


def sheet_page_has_hits(path: Path, doc_type: str, page: int, groups: list[list[str]]) -> bool:
    """Controllo veloce (senza disegnare) se una pagina del foglio contiene termini cercati."""
    pages = sheet_pages(path, doc_type)
    si, start = pages[page]
    rows = load_sheets(path, doc_type)[si][1][start:start + SHEET_PAGE_ROWS]
    pats = [p for terms in groups for p in term_patterns(terms)]
    return any(find_spans(" | ".join(r), pats) for r in rows)


def _draw_sheet(path: Path, doc_type: str, page: int, groups: list[list[str]]):
    from PIL import Image, ImageDraw

    pages = sheet_pages(path, doc_type)
    si, start = pages[max(0, min(page, len(pages) - 1))]
    name, all_rows, total = load_sheets(path, doc_type)[si]
    rows = all_rows[start:start + SHEET_PAGE_ROWS]
    font = _font(13)
    pad, row_h, max_w, num_w = 6, 22, 260, 54
    ncols = max((len(r) for r in all_rows[:2000]), default=1)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    col_w = [40] * ncols
    for r in all_rows[:150] + rows:
        for ci, v in enumerate(r[:ncols]):
            if v:
                col_w[ci] = min(max_w, max(col_w[ci], int(measure.textlength(v[:60], font=font)) + 2 * pad))
    head_h = 26
    note = f"{name}  —  righe {start + 1}-{start + len(rows)} di {len(all_rows)}"
    if total > len(all_rows):
        note += f" (lette le prime {len(all_rows)} su {total})"
    W = max(num_w + sum(col_w) + 1, 400)
    H = head_h + row_h * max(1, len(rows)) + 1
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, head_h], fill="#e8e8e8")
    d.text((pad, 5), note, fill="#222222", font=font)
    group_pats = [term_patterns(terms) for terms in groups]
    marks = []
    y = head_h
    for ri, r in enumerate(rows):
        cells = [(r[ci] if ci < len(r) else "") for ci in range(ncols)]
        # numero di riga
        d.rectangle([0, y, num_w, y + row_h], fill="#f2f2f2", outline="#c8c8c8")
        d.text((pad, y + 3), str(start + ri + 1), fill="#777777", font=font)
        # testo della riga come nel testo estratto ("cella | cella"), così una frase che continua
        # nella cella accanto viene trovata e colorata in entrambe le celle
        offsets, text, x = [], "", num_w
        for ci, v in enumerate(cells):
            if text:
                text += " | "
            offsets.append((len(text), len(text) + len(v), x, ci))
            text += v
            x += col_w[ci]
        for gi, pats in enumerate(group_pats):
            for s, e in find_spans(text, pats):
                for c0, c1, cx, ci in offsets:
                    a, b = max(s, c0), min(e, c1)
                    if a >= b:
                        continue
                    v = cells[ci]
                    limit = cx + col_w[ci] - pad
                    hx0 = cx + pad + (measure.textlength(v[:a - c0], font=font) if a > c0 else 0)
                    if hx0 >= limit:
                        continue  # parte non visibile (cella troncata)
                    hx1 = min(limit, cx + pad + measure.textlength(v[:b - c0], font=font))
                    hb = (hx0 - 1, y + 3, hx1 + 1, y + row_h - 3)
                    d.rounded_rectangle(hb, radius=5, fill=color(gi))
                    marks.append((gi, hb))
        x = num_w
        for ci, v in enumerate(cells):
            d.rectangle([x, y, x + col_w[ci], y + row_h], outline="#c8c8c8")
            if v:
                # taglio iniziale grossolano (le celle possono avere migliaia di caratteri), poi rifinitura
                txt = v[: max(1, (col_w[ci] - 2 * pad) // 5)]
                while txt and measure.textlength(txt, font=font) > col_w[ci] - 2 * pad:
                    txt = txt[:-2]
                d.text((x + pad, y + 3), txt if len(txt) == len(v) else txt + "…", fill="#111111", font=font)
            x += col_w[ci]
        y += row_h
    marks = [(gi, (b[0] / W, b[1] / H, b[2] / W, b[3] / H)) for gi, b in marks]
    return img, marks
