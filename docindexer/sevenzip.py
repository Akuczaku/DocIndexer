# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Archivi compressi con 7-Zip (modulo aggiuntivo facoltativo in tools\\7zip).

7-Zip non è incluso: si scarica da Impostazioni > Strumenti esterni (installer ufficiale, solo estratto, non
installato). Gli archivi si estraggono in una cartella temporanea di Windows, si legge il testo dei file
contenuti e la cartella si cancella subito dopo. Gli archivi protetti da password si saltano.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import TOOLS_DIR

SEVENZIP_DIR = TOOLS_DIR / "7zip"
SEVENZIP_EXE = SEVENZIP_DIR / "7z.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def available() -> bool:
    return SEVENZIP_EXE.is_file()


def listing(archive: Path) -> list[tuple[str, int]]:
    """(percorso interno, dimensione) dei file dell'archivio, senza estrarre."""
    out = subprocess.run([str(SEVENZIP_EXE), "l", "-slt", "-ba", "-p", str(archive)], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=300, creationflags=NO_WINDOW)
    files, cur = [], {}
    for line in out.stdout.splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Attributes", "").find("D") != 0 and cur.get("Folder") != "+":
                try:
                    files.append((cur["Path"], int(cur.get("Size") or 0)))
                except ValueError:
                    pass
            cur = {}
            continue
        k, _, v = line.partition(" = ")
        cur[k.strip()] = v.strip()
    return files


class Extracted:
    """Archivio estratto in una cartella temporanea, cancellata all'uscita dal blocco with."""

    def __init__(self, archive: Path):
        self.archive = archive
        self.dir = Path(tempfile.mkdtemp(prefix="DocIndexer_archivio_"))

    def __enter__(self) -> Path:
        # -p senza password: un archivio cifrato fallisce subito invece di chiedere la password
        r = subprocess.run([str(SEVENZIP_EXE), "x", "-y", "-p", "-bd", f"-o{self.dir}", str(self.archive)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                           creationflags=NO_WINDOW)
        if r.returncode not in (0, 1):        # 1 = avvisi (es. alcuni file non estratti)
            msg = (r.stderr or r.stdout).strip().splitlines()
            raise OSError(msg[-1] if msg else f"7-Zip: codice {r.returncode}")
        return self.dir

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False
