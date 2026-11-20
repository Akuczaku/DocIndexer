# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Estrazione del testo: sceglie lo strumento giusto in base al tipo rilevato."""
from __future__ import annotations

import math
import os
import re
import threading
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import detect as dt
from . import exiftool, ocr, sevenzip
from .config import AppConfig
from .i18n import tr


@dataclass
class Page:
    page_no: int
    text: str
    method: str          # text | ocr | table
    label: str = ""      # es. nome del foglio Excel
    # parole riconosciute dall'OCR con il loro riquadro, in coordinate relative alla pagina (0..1):
    # [[parola, x0, y0, x1, y1], ...]  -> servono a evidenziare i termini sull'anteprima
    boxes: list | None = None


@dataclass
class ArchivePage:
    inner_path: str      # percorso del file dentro l'archivio
    page_no: int
    text: str
    method: str
    label: str = ""


@dataclass
class DbTable:
    tool: str            # sqlite | dbf | access
    table: str
    text: str


@dataclass
class Extraction:
    doc_type: str
    pages: list[Page]
    page_count: int
    meta: list = field(default_factory=list)          # (tool, "Gruppo · Tag", valore) -> tabella meta_items
    meta_read: bool = False                           # True se i metadati sono stati letti (anche vuoti)
    archive: list = field(default_factory=list)       # ArchivePage -> tabella archive_items
    tables: list = field(default_factory=list)        # DbTable -> tabella db_items

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.method == "ocr")


class ExtractionError(Exception):
    pass


# xlrd scrive avvisi su stdout, che nell'exe senza console non esiste
_NULL_LOG = open(os.devnull, "w")

_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
                            "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
# caratteri di controllo (tranne \t \n) e carattere sostitutivo: indicano glifi senza mappatura Unicode
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f�]")


def _quad_w(q) -> float:
    return math.dist(q[0], q[1])


def _quad_h(q) -> float:
    return math.dist(q[1], q[2])


def _decode(data: bytes) -> str:
    """Testo da byte: UTF-16 (con o senza BOM), UTF-8, Windows-1252."""
    encodings = ["utf-16"] if data[:2] in (b"\xff\xfe", b"\xfe\xff") else []
    guessed = dt.utf16_without_bom(data[:4096])
    if guessed and not encodings:
        encodings = [guessed]
    for enc in encodings + ["utf-8-sig", "cp1252"]:
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("latin-1")


def _html_to_text(html: str) -> str:
    """Testo visibile di una pagina HTML (senza script e stili)."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript"):
                self.skip += 1
            elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "td", "th"):
                self.parts.append("\n")
            for k, v in attrs:
                if k in ("title", "alt") and v:
                    self.parts.append(f" {v} ")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    p = P()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001  (HTML rotto: si tiene quanto letto)
        pass
    return "".join(p.parts)


def _xml_text(data: bytes, attributes: bool = True) -> str:
    """Testo e valori degli attributi di un XML; se l'XML è rotto, il testo senza i tag."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return re.sub(r"<[^>]+>", " ", _decode(data))
    parts = []
    for el in root.iter():
        if attributes:
            parts += [str(v) for k, v in el.attrib.items() if v and not k.endswith("}schemaLocation")]
        if el.text and el.text.strip():
            parts.append(el.text.strip())
        if el.tail and el.tail.strip():
            parts.append(el.tail.strip())
    return "\n".join(parts)


def _mail_text(msg) -> str:
    """Intestazioni principali, corpo (testo o HTML) e nomi degli allegati di un'e-mail."""
    head = [f"{h}: {msg.get(h, '')}" for h in ("From", "To", "Cc", "Date", "Subject") if msg.get(h)]
    body, names = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        if fname:
            names.append(fname)
            continue
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html"):
            try:
                txt = part.get_content()
            except Exception:  # noqa: BLE001
                txt = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            body.append(_html_to_text(txt) if ctype == "text/html" else txt)
    att = ("\n" + tr("Allegati") + ": " + ", ".join(names)) if names else ""
    return "\n".join(head) + "\n\n" + "\n".join(body) + att


def access_driver() -> str | None:
    """Nome del driver ODBC di Access (con Office o "Microsoft Access Database Engine"), se c'è."""
    try:
        import pyodbc

        return next((d for d in pyodbc.drivers() if "Access Driver" in d and "accdb" in d), None)
    except Exception:  # noqa: BLE001
        return None


def _register_heif() -> None:
    """Foto HEIC/HEIF/AVIF (iPhone e molti Android) aperte con Pillow tramite pillow-heif, se c'è."""
    global _HEIF
    if _HEIF is None:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _HEIF = True
        except ImportError:
            _HEIF = False


_HEIF = None


def bad_char_ratio(text: str) -> float:
    stripped = text.strip()
    return len(_BAD_CHARS.findall(stripped)) / len(stripped) if stripped else 0.0


def _clean(text: str) -> str:
    """Normalizza il testo salvato: legature espanse, caratteri di controllo rimossi,
    a capo / tabulazioni / spazi multipli sostituiti da un solo spazio."""
    # NFC: lettere accentate in forma composta (alcuni Excel/Word scrivono "g" + accento separato,
    # che impedirebbe di evidenziare DOĞAN cercando DOGAN)
    text = unicodedata.normalize("NFC", text).translate(_LIGATURES)
    text = _BAD_CHARS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# cartelle dentro lo zip dove i formati Office / OpenDocument / EPUB tengono le immagini incollate
EMBEDDED_MEDIA = {
    dt.DOCX: ("word/media/",), dt.XLSX: ("xl/media/",), dt.PPTX: ("ppt/media/",),
    dt.ODT: ("pictures/",), dt.ODS: ("pictures/",), dt.ODP: ("pictures/",),
    dt.EPUB: ("oebps/images/", "images/", "oebps/image/", "ops/images/"),
}
EMBEDDED_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".jfif", ".bmp", ".gif", ".tif", ".tiff", ".webp")
MIN_EMBEDDED_BYTES = 8 * 1024     # sotto: icone e decorazioni
MIN_EMBEDDED_SIDE = 120           # pixel del lato corto
MAX_EMBEDDED_IMAGES = 200         # tetto per documento


class Extractor:
    def __init__(self, cfg: AppConfig, pace=None):
        self.cfg = cfg
        # chiamata tra una pagina e l'altra dei documenti lunghi: il demone la usa per rallentare
        # (tetto di CPU) o fermarsi, anche a metà di un PDF di centinaia di pagine
        self.pace = pace or (lambda: None)
        self._exif: exiftool.ExifTool | None = None
        self._doc_model: str | None = None     # modello OCR scelto per il documento in corso ("auto")

    def _ocr_model(self, im) -> str:
        """Modello OCR per questa immagine. Con "auto" la prima pagina OCR di ogni documento decide:
        se il modello latino legge con sicurezza si usa quello, altrimenti si provano gli altri modelli
        scaricati e si tiene quello con più testo sicuro (le pagine seguenti usano la stessa scelta)."""
        key = self.cfg.ocr_model
        if key != ocr.AUTO:
            return key
        if self._doc_model:
            return self._doc_model
        others = [k for k in ocr.installed() if k != ocr.DEFAULT_MODEL]
        choice = ocr.DEFAULT_MODEL
        if others:
            def quality(k):
                words = [(w, sc) for w, sc, q in ocr.recognize(im, k) if q]
                good = sum(len(w) * sc for w, sc in words if sc >= 0.6)
                mean = sum(sc for _w, sc in words) / len(words) if words else 0.0
                return good, mean

            best_good, mean = quality(ocr.DEFAULT_MODEL)
            if mean < 0.85 or best_good < 20:
                for k in others:
                    good, _m = quality(k)
                    if good > best_good * 1.15:
                        best_good, choice = good, k
        self._doc_model = choice
        return choice

    # ----------------------------------------------------------------- OCR
    def _ocr_image(self, img) -> tuple[str, list]:
        """OCR con RapidOCR: ritorna il testo e le parole con il loro riquadro in coordinate 0..1
        dell'immagine ORIGINALE (per l'evidenziazione sull'anteprima).

        RapidOCR trova da solo le righe anche storte e capovolte (180 gradi); qui si aggiungono:
          1. ingrandimento delle immagini piccole (foto, screenshot)
          2. pagine girate di 90/270 gradi: se molte delle righe trovate sono più alte che
             larga (almeno un terzo) si riprova ruotando l'immagine e si tiene il risultato con più testo sicuro
        """
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(img)
        orig_w, orig_h = img.size
        work = img.convert("RGB") if img.mode != "RGB" else img
        scale = 1.0
        longest = max(work.size)
        if longest < 1200:
            scale = min(3.0, 1600 / longest)
            work = work.resize((int(work.width * scale), int(work.height * scale)), Image.LANCZOS)

        def run(angle: int):
            im = work.rotate(angle, expand=True) if angle else work
            try:
                words = ocr.recognize(im, self._ocr_model(im))
            except ocr.OcrNotReady as exc:
                raise ExtractionError(str(exc)) from exc
            good = sum(len(w) for w, sc, q in words if q and sc >= 0.6)
            quads = [q for w, sc, q in words if q]
            tall = sum(1 for q in quads if _quad_h(q) > 1.5 * _quad_w(q) and len(q) == 4)
            return good, tall, len(quads), angle, im.size, words

        best = run(0)
        if best[2] and best[1] >= max(2, best[2] / 3):  # molte righe verticali: pagina girata di 90 gradi
            for angle in (90, 270):
                cand = run(angle)
                if cand[0] > best[0]:
                    best = cand
        _good, _tall, _n, angle, _size, words = best

        def unrotate(x, y):
            # rotate() di PIL è antiorario con expand: punto dell'immagine ruotata -> immagine non ruotata
            if angle == 90:
                return work.width - y, x
            if angle == 270:
                return y, work.height - x
            return x, y

        lines: list[list[str]] = [[]]
        boxes = []
        for word, _score, quad in words:
            if word == "\n":
                lines.append([])
                continue
            lines[-1].append(word)
            pts = [unrotate(px, py) for px, py in quad]
            xs, ys = [p[0] / scale for p in pts], [p[1] / scale for p in pts]
            boxes.append([word, round(max(0.0, min(xs)) / orig_w, 5), round(max(0.0, min(ys)) / orig_h, 5),
                          round(min(orig_w, max(xs)) / orig_w, 5), round(min(orig_h, max(ys)) / orig_h, 5)])
        text = "\n".join(" ".join(ws) for ws in lines if ws)
        return text, boxes

    # ------------------------------------------------------------ dispatch
    def extract(self, path: str | Path, doc_type: str | None = None, with_meta: bool = True) -> Extraction:
        path = Path(path)
        self._doc_model = None
        if not doc_type or doc_type == dt.UNKNOWN:
            doc_type, _ = dt.detect(path)
        if doc_type == dt.UNKNOWN and self.exif_on():
            doc_type = dt.META
        meta, meta_read = self.metadata(path) if with_meta else ([], False)
        if doc_type == dt.META:                       # file senza testo: solo i metadati
            return Extraction(doc_type, [], 0, meta, meta_read)
        if doc_type == dt.ARCHIVE:                    # archivi: il testo dei file contenuti (7-Zip)
            if not self.module_on(doc_type):
                raise ExtractionError(tr("Modulo disattivato o non installato: {name}", name="7-Zip"))
            arch = self._archive(path)
            return Extraction(doc_type, [], len(arch), meta, meta_read, archive=arch)
        if doc_type in dt.DB_TYPES:                   # database: una "tabella" per tabella
            if not self.module_on(doc_type):
                raise ExtractionError(tr("Modulo disattivato o non installato: {name}", name=doc_type))
            reader = {dt.SQLITE: self._sqlite, dt.DBF: self._dbf, dt.ACCESS: self._access}[doc_type]
            tables = [DbTable(doc_type, p.label or "", _clean(p.text)) for p in reader(path)]
            tables = [t for t in tables if t.text]
            return Extraction(doc_type, [], len(tables), meta, meta_read, tables=tables)
        handler = {
            dt.PDF: self._pdf,
            dt.IMAGE: self._image,
            dt.DOCX: self._docx,
            dt.DOC: self._doc,
            dt.XLSX: self._xlsx,
            dt.XLS: self._xls,
            dt.PPTX: self._pptx,
            dt.PPT: self._ppt,
            dt.ODT: self._odf,
            dt.ODS: self._odf,
            dt.RTF: self._rtf,
            dt.TEXT: self._text,
            dt.ODP: self._odf,
            dt.EPUB: self._epub,
            dt.HTML: self._html,
            dt.XML: self._xml,
            dt.JSON: self._json,
            dt.EML: self._eml,
            dt.MSG: self._msg,
            dt.GRAPH: self._graph,
        }.get(doc_type)
        if handler is None:
            raise ExtractionError(tr("Tipo di documento non supportato ({type})", type=doc_type))
        pages = handler(path)
        if doc_type in EMBEDDED_MEDIA:
            # testo dentro le immagini incollate nel documento (foto, scansioni, screenshot): OCR
            pages += self._embedded_images(path, EMBEDDED_MEDIA[doc_type], len(pages))
        pages = [Page(p.page_no, _clean(p.text), p.method, p.label, p.boxes) for p in pages]
        count = len(pages)
        pages = [p for p in pages if p.text]
        return Extraction(doc_type, pages, count, meta, meta_read)

    # ------------------------------------------------------------- moduli
    def module_on(self, doc_type: str) -> bool:
        """Il modulo che legge questo tipo è attivo (e installato, se serve)?"""
        cfg = self.cfg
        if doc_type == dt.ARCHIVE:
            return bool(getattr(cfg, "mod_archives", True)) and sevenzip.available()
        if doc_type == dt.SQLITE:
            return bool(getattr(cfg, "mod_sqlite", True))
        if doc_type == dt.DBF:
            return bool(getattr(cfg, "mod_dbf", True))
        if doc_type == dt.ACCESS:
            return bool(getattr(cfg, "mod_access", True)) and access_driver() is not None
        return True

    def metadata(self, path: Path) -> tuple[list, bool]:
        """Metadati con ExifTool, se attivo e installato: ([(tool, tag, valore)], letti?)."""
        if not self.exif_on():
            return [], False
        if self._exif is None:
            self._exif = exiftool.ExifTool()
        try:
            return [("exiftool", t, v) for t, v in exiftool.to_items(self._exif.metadata(path))], True
        except OSError:
            return [], False

    # ------------------------------------------------------------- archivi
    def _archive(self, path: Path) -> list[ArchivePage]:
        """Testo dei file dentro l'archivio. Si estrae in una cartella temporanea (cancellata subito dopo);
        si leggono i file dei tipi supportati, non gli archivi annidati, entro i limiti di dimensione."""
        max_file = self.cfg.max_file_mb * 1024 * 1024
        files = sevenzip.listing(path)
        total = sum(s for _p, s in files if s <= max_file)
        if total > 4 * max_file:
            raise ExtractionError(tr("Archivio troppo grande da estrarre ({mb} MB)", mb=total // 1048576))
        out: list[ArchivePage] = []
        wanted = self.cfg.included()
        with sevenzip.Extracted(path) as tmp:
            for f in sorted(p for p in tmp.rglob("*") if p.is_file()):
                inner = f.relative_to(tmp).as_posix()
                if f.stat().st_size > max_file or (f.suffix.lower() not in wanted and f.suffix):
                    continue
                self.pace()
                try:
                    kind, _m = dt.detect(f)
                    if kind in (dt.ARCHIVE, dt.UNKNOWN, dt.META):
                        continue
                    sub = self.extract(f, kind, with_meta=False)
                except Exception:  # noqa: BLE001  (un file rovinato non blocca l'archivio)
                    continue
                for p in sub.pages:
                    out.append(ArchivePage(inner, p.page_no, p.text, p.method, p.label))
                for t in sub.tables:
                    out.append(ArchivePage(inner, 1, t.text, "table", t.table))
        return out

    # ------------------------------------------------------------- DBF, Access
    @staticmethod
    def _dbf(path: Path) -> list[Page]:
        """Database dBASE / FoxPro (.dbf) con dbfread: nomi dei campi e valori, una pagina."""
        from dbfread import DBF

        table = DBF(str(path), ignore_missing_memofile=True, char_decode_errors="replace", load=False)
        lines = [" | ".join(table.field_names)]
        size = 0
        for rec in table:
            vals = [str(v) for v in rec.values() if v not in (None, "") and str(v).strip()]
            if vals:
                line = " | ".join(vals)
                lines.append(line)
                size += len(line)
            if size > 20_000_000:
                break
        return [Page(1, "\n".join(lines), "table", label=path.stem)]

    @staticmethod
    def _access(path: Path) -> list[Page]:
        """Database Access (.mdb/.accdb) con il driver ODBC di Microsoft (installato con Office), in sola lettura."""
        import pyodbc

        drv = access_driver()
        if drv is None:
            raise ExtractionError(tr("Driver di Microsoft Access non trovato"))
        con = pyodbc.connect(f"DRIVER={{{drv}}};DBQ={path};ReadOnly=1;", readonly=True, timeout=30)
        out = []
        try:
            cur = con.cursor()
            tables = [t.table_name for t in cur.tables(tableType="TABLE")]
            for t in tables:
                try:
                    rows = cur.execute(f"SELECT * FROM [{t}]")
                except pyodbc.Error:
                    continue
                cols = [d[0] for d in rows.description or []]
                lines = [" | ".join(cols)]
                size = 0
                for row in rows:
                    vals = [str(v) for v in row if isinstance(v, (str, int, float)) and str(v).strip()]
                    if vals:
                        line = " | ".join(vals)
                        lines.append(line)
                        size += len(line)
                    if size > 20_000_000:
                        break
                out.append(Page(len(out) + 1, "\n".join(lines), "table", label=t))
        finally:
            con.close()
        return out

    def exif_on(self) -> bool:
        return bool(getattr(self.cfg, "exif_enabled", False)) and exiftool.available()

    def close(self) -> None:
        if self._exif is not None:
            self._exif.close()
            self._exif = None

    def _embedded_images(self, path: Path, folders: tuple[str, ...], first: int) -> list[Page]:
        """OCR delle immagini contenute in un documento Office/OpenDocument/EPUB (sono file dentro lo zip).
        Le immagini piccole (icone, loghi, decorazioni) si saltano. Una pagina per immagine, dopo il testo."""
        from io import BytesIO

        from PIL import Image

        out: list[Page] = []
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            return out
        with zf:
            names = [i for i in zf.infolist()
                     if i.filename.lower().startswith(folders) and i.file_size >= MIN_EMBEDDED_BYTES
                     and i.filename.lower().endswith(EMBEDDED_IMAGE_EXT)]
            for n, info in enumerate(sorted(names, key=lambda i: i.filename)[:MAX_EMBEDDED_IMAGES], start=1):
                self.pace()
                try:
                    with Image.open(BytesIO(zf.read(info))) as im:
                        im.load()
                        if min(im.size) < MIN_EMBEDDED_SIDE:
                            continue
                        text, boxes = self._ocr_image(im.copy())
                except ExtractionError:
                    raise
                except Exception:  # noqa: BLE001  (immagine danneggiata o formato non leggibile)
                    continue
                if text.strip():
                    out.append(Page(first + len(out) + 1, text, "ocr", boxes=boxes,
                                    label=tr("immagine {n}", n=n) + f" ({Path(info.filename).name})"))
        return out

    # ----------------------------------------------------------------- PDF
    def _pdf(self, path: Path) -> list[Page]:
        import pymupdf as fitz
        from PIL import Image

        out: list[Page] = []
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise ExtractionError(tr("PDF protetto da password"))
            for i, page in enumerate(doc, start=1):
                self.pace()
                text = page.get_text("text") or ""
                garbled = bad_char_ratio(text) > 0
                if len(text.strip()) >= self.cfg.ocr_min_chars_per_page and not garbled:
                    out.append(Page(i, text, "text"))
                    continue
                # pagina scansionata, quasi vuota, oppure con font senza mappatura Unicode
                # (es. legature "tt"/"ti" che diventano caratteri di controllo): OCR della pagina
                zoom = self.cfg.ocr_dpi / 72
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
                img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                try:
                    ocr_text, boxes = self._ocr_image(img)
                except ExtractionError:
                    if garbled and text.strip():
                        # senza modelli OCR si tiene il testo (verrà ripulito dai caratteri non validi)
                        out.append(Page(i, text, "text"))
                        continue
                    raise
                if garbled and len(ocr_text.strip()) >= 0.6 * len(text.strip()):
                    out.append(Page(i, ocr_text, "ocr", boxes=boxes))
                elif not garbled and len(ocr_text.strip()) > len(text.strip()):
                    out.append(Page(i, ocr_text, "ocr", boxes=boxes))
                else:
                    out.append(Page(i, text, "text"))
        return out

    # -------------------------------------------------------------- images
    def _image(self, path: Path) -> list[Page]:
        from PIL import Image, ImageSequence

        _register_heif()

        out: list[Page] = []
        with Image.open(path) as im:
            for i, frame in enumerate(ImageSequence.Iterator(im), start=1):
                self.pace()
                text, boxes = self._ocr_image(frame.copy())
                out.append(Page(i, text, "ocr", boxes=boxes))
        return out

    # ---------------------------------------------------------------- Word
    def _docx(self, path: Path) -> list[Page]:
        import docx

        d = docx.Document(str(path))
        parts: list[str] = []
        for sec in d.sections:
            for p in sec.header.paragraphs:
                parts.append(p.text)
        for p in d.paragraphs:
            parts.append(p.text)
        for table in d.tables:
            for row in table.rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip()
                    if t and (not cells or cells[-1] != t):  # celle unite ripetute
                        cells.append(t)
                parts.append(" | ".join(cells))
        for sec in d.sections:
            for p in sec.footer.paragraphs:
                parts.append(p.text)
        return [Page(1, "\n".join(parts), "text")]

    def _doc(self, path: Path) -> list[Page]:
        """.doc legacy: usa Microsoft Word via COM se disponibile, altrimenti estrazione grezza."""
        try:
            return [Page(1, self._via_word_com(path), "text")]
        except Exception:
            pass
        return [Page(1, self._ole_raw_text(path), "text")]

    @staticmethod
    def _via_word_com(path: Path) -> str:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(str(path.resolve()), ReadOnly=True, AddToRecentFiles=False,
                                      ConfirmConversions=False, Visible=False)
            try:
                return doc.Content.Text
            finally:
                doc.Close(SaveChanges=False)
        finally:
            if word is not None:
                word.Quit()
            pythoncom.CoUninitialize()

    @staticmethod
    def _ole_raw_text(path: Path) -> str:
        import olefile

        with olefile.OleFileIO(str(path)) as ole:
            data = ole.openstream("WordDocument").read()
        runs_u16 = re.findall(r"(?:[\x20-\x7e -ɏ\r\n\t]){4,}", data.decode("utf-16le", "ignore"))
        runs_8 = re.findall(r"[\x20-\x7e\xa0-\xff\r\n\t]{4,}", data.decode("cp1252", "ignore"))
        best = max(("\n".join(runs_u16), "\n".join(runs_8)), key=len)
        return best.replace("\r", "\n")

    # --------------------------------------------------------------- Excel
    @staticmethod
    def _fmt(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    def _xlsx(self, path: Path) -> list[Page]:
        import openpyxl

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        out: list[Page] = []
        try:
            for i, ws in enumerate(wb.worksheets, start=1):
                lines = []
                for row in ws.iter_rows(values_only=True):
                    cells = [self._fmt(v) for v in row]
                    if any(cells):
                        lines.append(" | ".join(c for c in cells if c))
                out.append(Page(i, "\n".join(lines), "table", ws.title))
        finally:
            wb.close()
        return out

    def _xls(self, path: Path) -> list[Page]:
        import xlrd
        import xlrd.book

        try:
            book = xlrd.open_workbook(str(path), on_demand=True, logfile=_NULL_LOG)
        except UnicodeDecodeError:
            # file con la tabella delle stringhe danneggiata (stringa troncata): si rilegge
            # decodificando in modo tollerante invece di fallire su tutto il file
            strict = xlrd.book.unicode
            xlrd.book.unicode = lambda b, enc: b.decode(enc, "replace")
            try:
                book = xlrd.open_workbook(str(path), on_demand=True, logfile=_NULL_LOG)
            finally:
                xlrd.book.unicode = strict
        out: list[Page] = []
        try:
            for i in range(book.nsheets):
                sh = book.sheet_by_index(i)
                lines = []
                for r in range(sh.nrows):
                    cells = [self._fmt(v) for v in sh.row_values(r)]
                    if any(cells):
                        lines.append(" | ".join(c for c in cells if c))
                out.append(Page(i + 1, "\n".join(lines), "table", sh.name))
        finally:
            book.release_resources()
        return out

    # ------------------------------------------------------- altri formati
    # ---------------------------------------------------------- PowerPoint
    _ppt_lock = threading.Lock()  # PowerPoint gira come istanza unica: una presentazione alla volta

    def _ppt(self, path: Path) -> list[Page]:
        """.ppt legacy: PowerPoint via COM se disponibile, altrimenti lettura dei record di testo."""
        try:
            with self._ppt_lock:
                return self._ppt_via_com(path)
        except Exception:  # noqa: BLE001
            return [Page(1, self._ppt_raw_text(path), "text")]

    @staticmethod
    def _ppt_via_com(path: Path) -> list[Page]:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            app = win32com.client.DispatchEx("PowerPoint.Application")
            pres = app.Presentations.Open(str(path.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
            out: list[Page] = []
            try:
                for i, slide in enumerate(pres.Slides, start=1):
                    parts = []
                    for shape in slide.Shapes:
                        try:
                            if shape.HasTextFrame and shape.TextFrame.HasText:
                                parts.append(shape.TextFrame.TextRange.Text)
                            elif shape.HasTable:
                                tbl = shape.Table
                                for r in range(1, tbl.Rows.Count + 1):
                                    parts.append(" | ".join(
                                        tbl.Cell(r, c).Shape.TextFrame.TextRange.Text
                                        for c in range(1, tbl.Columns.Count + 1)))
                        except Exception:  # noqa: BLE001
                            continue
                    for shape in slide.NotesPage.Shapes:
                        try:
                            # 2 = ppPlaceholderBody: il testo delle note
                            if shape.PlaceholderFormat.Type == 2 and shape.TextFrame.HasText:
                                parts.append("[Note] " + shape.TextFrame.TextRange.Text)
                        except Exception:  # noqa: BLE001
                            continue
                    out.append(Page(i, "\n".join(parts), "text", f"Diapositiva {i}"))
            finally:
                pres.Close()
                if app.Presentations.Count == 0:
                    app.Quit()
            return out
        finally:
            pythoncom.CoUninitialize()

    @staticmethod
    def _ppt_raw_text(path: Path) -> str:
        """Legge i record TextCharsAtom (UTF-16) e TextBytesAtom (latin-1) dello stream PowerPoint."""
        import olefile
        import struct

        with olefile.OleFileIO(str(path)) as ole:
            data = ole.openstream("PowerPoint Document").read()
        parts, pos, n = [], 0, len(data)
        while pos + 8 <= n:
            ver_inst, rtype, rlen = struct.unpack_from("<HHI", data, pos)
            pos += 8
            if ver_inst & 0x0F == 0x0F:  # contenitore: si entra nei record figli
                continue
            body = data[pos:pos + rlen]
            if rtype == 0x0FA0:
                parts.append(body.decode("utf-16-le", "ignore"))
            elif rtype == 0x0FA8:
                parts.append(body.decode("latin-1", "ignore"))
            pos += rlen
        return "\n".join(parts)

    @staticmethod
    def _pptx(path: Path) -> list[Page]:
        import html

        out: list[Page] = []
        with zipfile.ZipFile(path) as zf:
            slides = [n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
            slides.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
            for i, name in enumerate(slides, start=1):
                xml = zf.read(name).decode("utf-8", "ignore")
                xml = re.sub(r"</a:p>", "\n", xml)
                text = "".join(a or b for a, b in re.findall(r"<a:t>([^<]*)</a:t>|(\n)", xml))
                # le note sono collegate alla diapositiva tramite il suo file .rels
                rels = name.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
                notes = None
                if rels in zf.namelist():
                    m = re.search(r'Target="\.\./notesSlides/(notesSlide\d+\.xml)"', zf.read(rels).decode("utf-8", "ignore"))
                    notes = f"ppt/notesSlides/{m.group(1)}" if m else None
                if notes and notes in zf.namelist():
                    nxml = re.sub(r"</a:p>", "\n", zf.read(notes).decode("utf-8", "ignore"))
                    ntext = "".join(a or b for a, b in re.findall(r"<a:t>([^<]*)</a:t>|(\n)", nxml))
                    if ntext.strip():
                        text += "\n[Note] " + ntext
                out.append(Page(i, html.unescape(text), "text", f"Diapositiva {i}"))
        return out

    @staticmethod
    def _odf(path: Path) -> list[Page]:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("content.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</text:p>|</text:h>|</table:table-row>", "\n", xml)
        xml = re.sub(r"</table:table-cell>", " | ", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        import html

        return [Page(1, html.unescape(text), "text")]

    def _rtf(self, path: Path) -> list[Page]:
        try:
            return [Page(1, self._via_word_com(path), "text")]
        except Exception:
            raw = path.read_bytes().decode("cp1252", "ignore")
            raw = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252"), raw)
            raw = re.sub(r"\\par[d]?", "\n", raw)
            raw = re.sub(r"\{\\\*[^{}]*\}|\\[a-zA-Z]+-?\d* ?|[{}]", "", raw)
            return [Page(1, raw, "text")]

    @staticmethod
    def _text(path: Path) -> list[Page]:
        return [Page(1, _decode(path.read_bytes()), "text")]

    STREAM_BYTES = 16 * 1024 * 1024       # file di testo più grandi di così si leggono a pezzi
    STREAM_PAGE = 1024 * 1024             # byte letti per "pagina"

    def text_stream(self, path: str | Path, doc_type: str | None):
        """File di testo grande (CSV, log, elenchi da GB): generatore di pagine da circa 1 MB, tagliate a
        fine riga, senza mai tenere il file intero in memoria. None se il file si legge nel modo normale."""
        path = Path(path)
        if not doc_type or doc_type == dt.UNKNOWN:
            doc_type, _ = dt.detect(path)
        try:
            if doc_type != dt.TEXT or path.stat().st_size <= self.STREAM_BYTES:
                return None
        except OSError:
            return None
        return self._text_pages(path)

    def _text_pages(self, path: Path):
        import codecs

        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
            if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
                enc = "utf-16"
            elif dt.utf16_without_bom(head[:4096]):
                enc = dt.utf16_without_bom(head[:4096])
            else:
                try:
                    codecs.getincrementaldecoder("utf-8-sig")().decode(head)
                    enc = "utf-8-sig"
                except UnicodeError:
                    enc = "cp1252"
            dec = codecs.getincrementaldecoder(enc)(errors="replace")
            fh.seek(0)
            rest, n = "", 0
            while True:
                chunk = fh.read(self.STREAM_PAGE)
                text = rest + dec.decode(chunk, final=not chunk)
                if chunk:
                    cut = text.rfind("\n")
                    if cut < 0 and len(text) < 4 * self.STREAM_PAGE:
                        rest = text                 # riga lunghissima: si aspetta la sua fine
                        continue
                    text, rest = (text[:cut + 1], text[cut + 1:]) if cut >= 0 else (text, "")
                else:
                    rest = ""
                text = _clean(text)
                if text:
                    n += 1
                    yield Page(n, text, "text")
                if not chunk:
                    return

    # ------------------------------------------------- web, dati, posta
    @staticmethod
    def _html(path: Path) -> list[Page]:
        return [Page(1, _html_to_text(_decode(path.read_bytes())), "text")]

    @staticmethod
    def _xml(path: Path) -> list[Page]:
        return [Page(1, _xml_text(path.read_bytes()), "text")]

    @staticmethod
    def _json(path: Path) -> list[Page]:
        """Chiavi e valori del JSON (anche annidati), una riga per valore: "chiave: valore"."""
        import json

        raw = _decode(path.read_bytes())
        try:
            data = json.loads(raw)
        except ValueError:
            return [Page(1, raw, "text")]
        lines: list[str] = []

        def walk(v, key=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, str(k))
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
            elif v is not None and str(v).strip():
                lines.append(f"{key}: {v}" if key else str(v))

        walk(data)
        return [Page(1, "\n".join(lines), "text")]

    @staticmethod
    def _eml(path: Path) -> list[Page]:
        import email
        from email import policy

        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        return [Page(1, _mail_text(msg), "text")]

    @staticmethod
    def _msg(path: Path) -> list[Page]:
        """E-mail di Outlook (.msg) con extract-msg."""
        import extract_msg

        m = extract_msg.openMsg(str(path))
        try:
            head = [f"From: {m.sender or ''}", f"To: {m.to or ''}", f"Cc: {m.cc or ''}",
                    f"Date: {m.date or ''}", f"Subject: {m.subject or ''}"]
            body = m.body or ""
            if not body.strip() and getattr(m, "htmlBody", None):
                body = _html_to_text(_decode(m.htmlBody))
            names = [getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or ""
                     for a in m.attachments]
            att = ("\n" + tr("Allegati") + ": " + ", ".join(n for n in names if n)) if any(names) else ""
            return [Page(1, "\n".join(head) + "\n\n" + body + att, "text")]
        finally:
            m.close()

    @staticmethod
    def _epub(path: Path) -> list[Page]:
        """EPUB: un capitolo (file XHTML) per pagina, nell'ordine dell'archivio."""
        out = []
        with zipfile.ZipFile(path) as zf:
            names = sorted(n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
            for i, n in enumerate(names, start=1):
                text = _html_to_text(_decode(zf.read(n)))
                if text.strip():
                    out.append(Page(len(out) + 1, text, "text", label=Path(n).stem))
        return out

    # ----------------------------------------------------------- database
    @staticmethod
    def _sqlite(path: Path) -> list[Page]:
        """Database SQLite: una pagina per tabella con i nomi delle colonne e i valori di testo.
        Il file si apre in sola lettura (immutable), così non si tocca un db in uso da un altro programma."""
        import sqlite3

        uri = Path(path).resolve().as_uri().replace("file:", "file:", 1) + "?mode=ro&immutable=1"
        out = []
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for t in tables:
                try:
                    cur = con.execute(f'SELECT * FROM "{t.replace(chr(34), chr(34) * 2)}" LIMIT 200000')
                except sqlite3.DatabaseError:
                    continue   # es. tabelle virtuali di moduli non disponibili
                cols = [d[0] for d in cur.description or []]
                lines = [" | ".join(cols)]
                size = 0
                for row in cur:
                    vals = [str(v) for v in row if isinstance(v, (str, int, float)) and str(v).strip()]
                    if vals:
                        line = " | ".join(vals)
                        lines.append(line)
                        size += len(line)
                    if size > 20_000_000:   # tetto per tabella: il testo resta cercabile senza esplodere
                        break
                out.append(Page(len(out) + 1, "\n".join(lines), "table", label=t))
        finally:
            con.close()
        return out

    # --------------------------------------------------------------- grafi
    @staticmethod
    def _graph(path: Path) -> list[Page]:
        """Grafi: GraphML / GEXF (XML), GML e DOT (testo). Si estraggono etichette, attributi e archi."""
        data = path.read_bytes()
        head = data[:2048].lower()
        if b"<graphml" in head or b"<gexf" in head or data.lstrip()[:5] == b"<?xml":
            return [Page(1, _xml_text(data, attributes=True), "text")]
        text = _decode(data)
        # GML / DOT: si tengono i valori tra virgolette e gli identificatori, senza la sintassi
        values = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
        words = re.sub(r'"(?:[^"\\]|\\.)*"', " ", text)
        words = re.sub(r"[{}\[\];=,]|->|--", " ", words)
        return [Page(1, "\n".join(values) + "\n" + words, "text")]
