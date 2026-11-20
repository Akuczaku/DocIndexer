# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Ricerca regex in un processo separato.

Le espressioni regolari di Python tengono occupato l'interprete per tutta la durata di una ricerca in una
pagina: con certi pattern su certi testi (es. e-mail o importi su righe di migliaia di puntini o cifre) una
sola pagina può richiedere minuti e, nello stesso processo, la finestra resterebbe congelata. In un processo
a parte la finestra resta sempre libera e la ricerca si può fermare in ogni momento (il processo si chiude).

Protocollo (pickle su stdin/stdout, tra processi dello stesso programma):
  ingresso  {"db": percorso, "spec": ricerca, "folders": cartelle monitorate, "limit": n}
  uscita    ("row", risultato) ... poi ("done", None) oppure ("error", messaggio)

Avvio: python -m docindexer.regexworker   (programma compilato: DocIndexer.exe --regex-worker, vedi main.py)
"""
from __future__ import annotations

import pickle
import subprocess
import sys

ARG = "--regex-worker"


def command() -> list[str]:
    """Riga di comando che avvia questo processo, da sorgenti o dal programma compilato."""
    if getattr(sys, "frozen", False):
        return [sys.executable, ARG]
    return [sys.executable, "-m", "docindexer.regexworker"]


def start(job: dict) -> subprocess.Popen:
    """Avvia la ricerca: i risultati si leggono con read() da proc.stdout."""
    from .config import BASE_DIR

    proc = subprocess.Popen(command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            cwd=str(BASE_DIR), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        pickle.dump(job, proc.stdin)
        proc.stdin.close()
    except OSError:
        pass
    try:                                    # priorità bassa: non deve rallentare il resto del PC
        import psutil

        psutil.Process(proc.pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
    except Exception:  # noqa: BLE001
        pass
    return proc


def read(proc: subprocess.Popen):
    """Messaggi del processo, uno per volta, fino alla fine (anche se il processo viene chiuso)."""
    while True:
        try:
            yield pickle.load(proc.stdout)
        except (EOFError, OSError, pickle.UnpicklingError, ValueError):
            return


def _exit_with_parent(pid: int) -> None:
    """Se DocIndexer viene chiuso mentre la ricerca lavora, questo processo non deve restare acceso."""
    import os
    import threading
    import time

    def watch():
        try:
            import psutil

            while psutil.pid_exists(pid):
                time.sleep(2)
        except Exception:  # noqa: BLE001
            return
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def main() -> None:
    out = sys.stdout.buffer
    try:
        job = pickle.load(sys.stdin.buffer)
        if job.get("parent"):
            _exit_with_parent(int(job["parent"]))
        from . import searches
        from .db import Database

        db = Database(job["db"])
        for row in searches.iter_regex(db, job["spec"], job["folders"], job.get("limit", 500)):
            pickle.dump(("row", row), out)
            out.flush()
        pickle.dump(("done", None), out)
    except Exception as exc:  # noqa: BLE001
        pickle.dump(("error", str(exc)), out)
    out.flush()


if __name__ == "__main__":
    main()
