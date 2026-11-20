# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Avvio di DocIndexer - indicizzatore documenti."""
import multiprocessing
import sys

if __name__ == "__main__":
    # programma compilato: se una libreria avvia processi con multiprocessing, il figlio esegue solo il suo compito
    multiprocessing.freeze_support()

if __name__ == "__main__" and "--regex-worker" in sys.argv:
    # processo separato della ricerca regex (programma compilato; da sorgenti: python -m docindexer.regexworker)
    from docindexer.regexworker import main as regex_worker

    regex_worker()
    sys.exit(0)

if __name__ == "__main__" and "--extract-worker" in sys.argv:
    # processo separato che legge i file e fa l'OCR (programma compilato; da sorgenti: python -m docindexer.extractworker)
    from docindexer.extractworker import main as extract_worker

    extract_worker()
    sys.exit(0)

import docindexer.nowindow  # noqa: E402,F401  (prima di tutto: niente finestre del terminale per Word, PowerPoint & co.)
from docindexer.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
