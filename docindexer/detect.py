# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Riconoscimento del tipo di documento dal contenuto (magic bytes), non solo dall'estensione."""
from __future__ import annotations

import zipfile
from pathlib import Path

# tipi interni
PDF, DOCX, DOC, XLSX, XLS, PPTX, PPT, IMAGE, TEXT, ODT, ODS, RTF, UNKNOWN = (
    "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "image", "text", "odt", "ods", "rtf", "unknown",
)
ODP, EPUB, SQLITE, HTML, XML, JSON, EML, MSG, GRAPH = (
    "odp", "epub", "sqlite", "html", "xml", "json", "eml", "msg", "graph",
)
ARCHIVE, DBF, ACCESS = "archive", "dbf", "access"
DB_TYPES = {"sqlite", DBF, ACCESS}
META = "meta"   # file senza testo leggibile (audio, video, raw...): se ne indicizzano solo i metadati
# grafi: formati XML (GraphML, GEXF) e testuali (GML, DOT)
GRAPH_EXT = {".graphml", ".gexf", ".gml", ".dot", ".gv"}

MIME = {
    PDF: "application/pdf",
    DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DOC: "application/msword",
    XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    XLS: "application/vnd.ms-excel",
    PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    PPT: "application/vnd.ms-powerpoint",
    ODT: "application/vnd.oasis.opendocument.text",
    ODS: "application/vnd.oasis.opendocument.spreadsheet",
    RTF: "application/rtf",
    TEXT: "text/plain",
    ODP: "application/vnd.oasis.opendocument.presentation",
    EPUB: "application/epub+zip",
    SQLITE: "application/vnd.sqlite3",
    HTML: "text/html",
    XML: "application/xml",
    JSON: "application/json",
    EML: "message/rfc822",
    MSG: "application/vnd.ms-outlook",
    GRAPH: "application/graph",
}

_IMAGE_SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"BM", "image/bmp"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# estensioni considerate "documenti" durante la scansione della cartella origine
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".docm", ".dot", ".dotx", ".dotm",
    ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".pot", ".potx",
    ".xls", ".xlsx", ".xlsm", ".odt", ".ods", ".rtf",
    ".txt", ".csv", ".tsv", ".md", ".log", ".json", ".xml", ".ini", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp",
}


def detect(path: str | Path) -> tuple[str, str]:
    """Ritorna (doc_type, mime)."""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(4096)

    # un PDF può avere qualche byte prima di "%PDF-"; ma un archivio zip/7z che CONTIENE un PDF non è un PDF
    if head.lstrip()[:5] == b"%PDF-" or (b"%PDF-" in head[:1024] and not head.startswith(
            (b"PK\x03\x04", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x1f\x8b", OLE_MAGIC))):
        return PDF, MIME[PDF]

    for sig, mime in _IMAGE_SIGNATURES:
        if head.startswith(sig):
            return IMAGE, mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return IMAGE, "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1",
                                                b"avif", b"avis"):
        return IMAGE, "image/heic"
    if head.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") or head.startswith(b"\xff\x4f\xff\x51"):
        return IMAGE, "image/jp2"
    if path.suffix.lower() in (".ico", ".tga", ".pcx", ".ppm", ".pgm", ".pbm", ".psd", ".dib"):
        try:
            from PIL import Image

            with Image.open(path) as im:
                im.verify()
            return IMAGE, "image/" + path.suffix.lower().lstrip(".")
        except Exception:  # noqa: BLE001
            pass

    if head.startswith(b"PK\x03\x04"):
        return _detect_zip(path)

    if head.startswith(OLE_MAGIC):
        return _detect_ole(path)

    if head.startswith(b"{\\rtf"):
        return RTF, MIME[RTF]

    if head.startswith(b"SQLite format 3\x00"):
        return SQLITE, MIME[SQLITE]

    if head[4:19] in (b"Standard Jet DB", b"Standard ACE DB"):
        return ACCESS, "application/x-msaccess"
    if (head.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"MSCF"))
            or head[257:262] == b"ustar"):
        return ARCHIVE, "application/x-archive"
    if path.suffix.lower() == ".dbf" and head[:1] in (b"\x02", b"\x03", b"\x30", b"\x31", b"\x32", b"\x43",
                                                      b"\x63", b"\x83", b"\x8b", b"\xcb", b"\xf5", b"\xfb"):
        return DBF, "application/dbf"

    if _looks_like_text(head):
        return _detect_text(path, head)

    return UNKNOWN, "application/octet-stream"


def _detect_zip(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                return DOCX, MIME[DOCX]
            if "xl/workbook.xml" in names:
                return XLSX, MIME[XLSX]
            if "ppt/presentation.xml" in names:
                return PPTX, MIME[PPTX]
            if "mimetype" in names:
                mt = zf.read("mimetype").decode("ascii", "ignore").strip()
                if mt.endswith("opendocument.text"):
                    return ODT, MIME[ODT]
                if mt.endswith("opendocument.spreadsheet"):
                    return ODS, MIME[ODS]
                if mt.endswith("opendocument.presentation"):
                    return ODP, MIME[ODP]
                if mt == "application/epub+zip":
                    return EPUB, MIME[EPUB]
    except zipfile.BadZipFile:
        pass
    return ARCHIVE, "application/zip"


def _detect_ole(path: Path) -> tuple[str, str]:
    try:
        import olefile

        with olefile.OleFileIO(str(path)) as ole:
            if ole.exists("WordDocument"):
                return DOC, MIME[DOC]
            if ole.exists("Workbook") or ole.exists("Book"):
                return XLS, MIME[XLS]
            if ole.exists("PowerPoint Document"):
                return PPT, MIME[PPT]
            if ole.exists("__properties_version1.0") or ole.exists("__nameid_version1.0"):
                return MSG, MIME[MSG]
    except Exception:
        pass
    # fallback sull'estensione per file OLE non riconosciuti
    ext = path.suffix.lower()
    if ext == ".doc":
        return DOC, MIME[DOC]
    if ext == ".xls":
        return XLS, MIME[XLS]
    if ext in (".ppt", ".pps", ".pot"):
        return PPT, MIME[PPT]
    return UNKNOWN, "application/x-ole-storage"


def _detect_text(path: Path, head: bytes) -> tuple[str, str]:
    """File di testo: il formato si deduce dal contenuto e, se serve, dall'estensione."""
    ext = path.suffix.lower()
    sample = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if ext in GRAPH_EXT or b"<graphml" in head.lower() or b"<gexf" in head.lower():
        return GRAPH, MIME[GRAPH]
    if ext in (".html", ".htm", ".xhtml") or sample.startswith((b"<!doctype html", b"<html")):
        return HTML, MIME[HTML]
    if ext == ".eml" or (sample.startswith((b"received:", b"return-path:", b"from:", b"delivered-to:"))
                         and b"\nsubject:" in head.lower()):
        return EML, MIME[EML]
    if ext == ".json" or (sample[:1] in (b"{", b"[") and ext in ("", ".txt")):
        return JSON, MIME[JSON]
    if ext in (".xml", ".xaml", ".svg", ".kml", ".gpx") or sample.startswith(b"<?xml"):
        return XML, MIME[XML]
    return TEXT, MIME[TEXT]


def utf16_without_bom(head: bytes) -> str | None:
    """Riconosce UTF-16 senza BOM: metà dei byte sono zeri, tutti sulle posizioni pari o dispari."""
    if len(head) < 4:
        return None
    even = head[0::2].count(0) / len(head[0::2])
    odd = head[1::2].count(0) / len(head[1::2])
    if odd > 0.4 and even < 0.05:
        return "utf-16-le"
    if even > 0.4 and odd < 0.05:
        return "utf-16-be"
    return None


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or utf16_without_bom(head):
        return True
    if b"\x00" in head:
        return False
    for enc in ("utf-8", "cp1252"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False
