# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Backup e ripristino di database e impostazioni in un unico file .zip.

Contenuto del backup:
  docindexer.db          copia coerente del database (API di backup di SQLite: si può fare anche mentre
                         l'indicizzazione lavora)
  config.json            impostazioni
  regex_patterns.json    pattern regex personalizzati (se ci sono)
  saved_searches.json    ricerche salvate (se ci sono)
  backup.json            versione del programma e data

I file dei documenti non sono nel backup: DocIndexer non li copia mai, ricorda solo dove sono.
Il ripristino rimette database e impostazioni al loro posto e riavvia DocIndexer.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from .config import CONFIG_PATH, DATA_DIR

EXTRA_FILES = ("regex_patterns.json", "saved_searches.json")
DB_NAME = "docindexer.db"


def default_name() -> str:
    return time.strftime("DocIndexer_backup_%Y-%m-%d_%H%M.zip")


def create(db, target: str | Path, version: str = "") -> Path:
    """Crea il backup. db = Database aperto (o None: si copia il file così com'è)."""
    target = Path(target)
    work = Path(tempfile.mkdtemp(prefix="docindexer_backup_"))
    try:
        snap = work / DB_NAME
        db.backup_to(snap)
        tmp = target.with_suffix(".partial")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(snap, DB_NAME)
            if CONFIG_PATH.exists():
                z.write(CONFIG_PATH, "config.json")
            for name in EXTRA_FILES:
                p = DATA_DIR / name
                if p.exists():
                    z.write(p, name)
            z.writestr("backup.json", json.dumps({"app": "DocIndexer", "version": version,
                                                  "created": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1))
        os.replace(tmp, target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def inspect(path: str | Path) -> dict:
    """Controlla che il file sia un backup di DocIndexer; ritorna le sue informazioni. ValueError se non lo è."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if DB_NAME not in names:
                raise ValueError("no database")
            info = json.loads(z.read("backup.json")) if "backup.json" in names else {}
            info["size"] = z.getinfo(DB_NAME).file_size
            info["files"] = sorted(names)
            return info
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise ValueError(str(exc)) from exc


def restore(path: str | Path, db_file: Path) -> None:
    """Rimette al loro posto database e impostazioni. Il database deve essere chiuso.
    Il database attuale resta come <nome>.before-restore (finché non si fa un altro ripristino)."""
    inspect(path)
    with zipfile.ZipFile(path) as z:
        work = Path(tempfile.mkdtemp(prefix="docindexer_restore_"))
        try:
            z.extractall(work, members=[n for n in z.namelist() if "/" not in n and "\\" not in n and ".." not in n])
            db_file.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(str(db_file) + suffix)
                except OSError:
                    pass
            if db_file.exists():
                os.replace(db_file, db_file.with_name(db_file.name + ".before-restore"))
            shutil.move(str(work / DB_NAME), db_file)
            if (work / "config.json").exists():
                cfg = json.loads((work / "config.json").read_text(encoding="utf-8"))
                # il database ripristinato resta dove DocIndexer lo cerca adesso
                from .config import portable

                cfg["db_path"] = portable(db_file)
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            for name in EXTRA_FILES:
                if (work / name).exists():
                    shutil.copyfile(work / name, DATA_DIR / name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
