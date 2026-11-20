# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Esportazione di risultati ed elenchi in CSV, Excel (.xlsx) e PDF, con importanza e note.

  CSV   separatore ";" e codifica UTF-8 con BOM: si apre bene in Excel italiano
  XLSX  openpyxl: intestazione in grassetto, filtri, colonne bloccate, righe colorate per importanza
  PDF   PyMuPDF: tabella in orizzontale A4, con titolo, data e numero di pagina
"""
from __future__ import annotations

import csv
import html
import os
import time
from pathlib import Path

from . import lang as lang_mod
from . import theme
from .i18n import tr


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _level(r: dict) -> str:
    lv = int(r.get("level") or 0)
    return f"{lv} · {tr(theme.LEVEL_NAMES[lv])}" if lv in theme.LEVEL_NAMES else ""


# colonne: (titolo, funzione sulla riga, larghezza in Excel)
_COMMON_HEAD = [
    ("Importanza", _level, 14),
    ("Nota", lambda r: r.get("note") or "", 30),
    ("Nome", lambda r: r.get("original_name") or "", 34),
    ("Cartella", lambda r: os.path.dirname(r.get("path") or ""), 40),
]
_COMMON_TAIL = [
    ("Lingua", lambda r: lang_mod.name(r.get("lang")), 12),
    ("Creato", lambda r: _when(r.get("ctime")), 17),
    ("Modificato", lambda r: _when(r.get("mtime")), 17),
    ("Dimensione (KB)", lambda r: round((r.get("size_bytes") or 0) / 1024, 1), 12),
]
COLUMNS = {
    "results": _COMMON_HEAD + [
        ("Pagina", lambda r: r.get("label") or r.get("page_no") or "", 14),
        ("Punteggio", lambda r: round(float(r.get("score") or 0), 2), 10),
    ] + _COMMON_TAIL + [
        ("Estratto", lambda r: (r.get("snippet_text") or "").replace("\n", " "), 70),
        ("Percorso", lambda r: r.get("path") or "", 60),
    ],
    "documents": _COMMON_HEAD + [
        ("Tipo", lambda r: r.get("doc_type") or "", 10),
        ("Stato", lambda r: r.get("status") or "", 10),
        ("Pagine", lambda r: r.get("page_count") or "", 8),
    ] + _COMMON_TAIL + [
        ("Percorso", lambda r: r.get("path") or "", 60),
    ],
    "duplicates": [
        ("Gruppo", lambda r: r.get("group") or "", 8),
        ("Somiglianza", lambda r: r.get("similarity") or "", 12),
    ] + _COMMON_HEAD + _COMMON_TAIL + [
        ("Percorso", lambda r: r.get("path") or "", 60),
    ],
}


def table(rows: list[dict], kind: str) -> tuple[list[str], list[list]]:
    cols = COLUMNS[kind]
    return [tr(c[0]) for c in cols], [[fn(r) for _t, fn, _w in cols] for r in rows]


def export(path: str | Path, rows: list[dict], kind: str = "results", title: str = "") -> Path:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".xlsx":
        _xlsx(path, rows, kind, title)
    elif ext == ".pdf":
        _pdf(path, rows, kind, title)
    else:
        _csv(path, rows, kind)
    return path


def _csv(path: Path, rows, kind) -> None:
    head, data = table(rows, kind)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(head)
        w.writerows(data)


def _xlsx(path: Path, rows, kind, title) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head, data = table(rows, kind)
    wb = Workbook()
    ws = wb.active
    ws.title = (title or "DocIndexer")[:31].translate({ord(c): " " for c in "[]:*?/\\"}) or "DocIndexer"
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2937")
        cell.alignment = Alignment(vertical="center")
    fills = {lv: PatternFill("solid", fgColor=theme.tint(c, 0.35, "#ffffff").lstrip("#"))
             for lv, c in theme.LEVEL_COLORS.items()}
    for r, values in zip(rows, data):
        ws.append(values)
        lv = int(r.get("level") or 0)
        if lv in fills:
            for cell in ws[ws.max_row]:
                cell.fill = fills[lv]
    for i, (_t, _fn, width) in enumerate(COLUMNS[kind], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def _pdf(path: Path, rows, kind, title) -> None:
    import pymupdf

    head, data = table(rows, kind)
    # nel PDF si lasciano fuori le colonne più lunghe se ce ne sono troppe per la pagina
    keep = [i for i, h in enumerate(COLUMNS[kind]) if h[0] not in ("Cartella",)]
    colors = {lv: theme.tint(c, 0.35, "#ffffff") for lv, c in theme.LEVEL_COLORS.items()}
    css = ("* {font-family: sans-serif; font-size: 7pt;} h1 {font-size: 12pt; margin: 0 0 2pt 0;}"
           " p {color: #555; margin: 0 0 6pt 0;} table {border-collapse: collapse; width: 100%;}"
           " th {background: #1f2937; color: #fff; text-align: left; padding: 2pt;}"
           " td {border-bottom: 0.5pt solid #ccc; padding: 2pt; vertical-align: top;}")
    out = [f"<h1>{html.escape(title or 'DocIndexer')}</h1>",
           "<p>" + html.escape(tr("{n} righe · esportato il {when}", n=len(rows), when=_when(time.time()))) + "</p>",
           "<table><tr>" + "".join(f"<th>{html.escape(head[i])}</th>" for i in keep) + "</tr>"]
    for r, values in zip(rows, data):
        bg = colors.get(int(r.get("level") or 0))
        style = f' style="background:{bg}"' if bg else ""
        out.append(f"<tr{style}>" + "".join(f"<td>{html.escape(str(values[i])[:400])}</td>" for i in keep)
                   + "</tr>")
    out.append("</table>")
    story = pymupdf.Story(html="".join(out), user_css=css)
    rect = pymupdf.paper_rect("a4-l")
    where = rect + (28, 28, -28, -36)
    writer = pymupdf.DocumentWriter(str(path))
    more = True
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    # numero di pagina in basso
    doc = pymupdf.open(str(path))
    for i, page in enumerate(doc, start=1):
        page.insert_text((rect.width - 110, rect.height - 16), f"DocIndexer · {i}/{doc.page_count}", fontsize=7,
                         color=(0.4, 0.4, 0.4))
    doc.saveIncr()
    doc.close()


def ask_and_export(parent, rows: list[dict], kind: str = "results", title: str = "") -> Path | None:
    """Chiede dove salvare (CSV, Excel o PDF) ed esporta. Ritorna il file o None se annullato."""
    from tkinter import filedialog

    from . import dialogs

    name = "".join(c if c.isalnum() or c in " -_" else "_" for c in (title or "DocIndexer"))[:60].strip() or "DocIndexer"
    target = filedialog.asksaveasfilename(
        parent=parent, title=tr("Esporta"), initialfile=f"{name}.xlsx", defaultextension=".xlsx",
        filetypes=[(tr("Excel"), "*.xlsx"), ("CSV", "*.csv"), ("PDF", "*.pdf")])
    if not target:
        return None
    try:
        out = export(target, rows, kind, title)
    except Exception as exc:  # noqa: BLE001
        dialogs.showerror(tr("Esporta"), tr("Esportazione non riuscita:\n{exc}", exc=exc), parent=parent)
        return None
    if dialogs.askyesno(tr("Esporta"), tr("Esportate {n} righe in:\n{path}\n\nVuoi aprire il file?",
                                         n=len(rows), path=out), parent=parent):
        try:
            os.startfile(str(out))  # type: ignore[attr-defined]
        except OSError:
            pass
    return out
