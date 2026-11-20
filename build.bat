@echo off
REM Compila DocIndexer in un unico eseguibile: dist\DocIndexer.exe
REM Nel bundle ci sono solo codice, librerie, assets (logo) e il manuale (docs\manual, tutte le lingue).
REM Accanto all'exe restano: config.json, data\, cache\, tools\
REM L'exe si avvia anche con --background (solo icona vicino all'orologio): e' il comando dell'avvio con Windows.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name DocIndexer ^
  --icon "assets\docindexer.ico" ^
  --add-data "assets;assets" ^
  --add-data "docs\manual;docs\manual" ^
  --collect-data customtkinter ^
  --collect-data rapidocr ^
  --collect-binaries onnxruntime ^
  --collect-all pillow_heif ^
  --collect-data extract_msg ^
  --collect-submodules watchdog ^
  --hidden-import pystray._win32 ^
  --exclude-module pymupdf_layout ^
  --exclude-module pymupdf4llm ^
  main.py
if errorlevel 1 exit /b 1
echo.
copy /y LICENSE dist\ >nul
copy /y NOTICE dist\ >nul
copy /y THIRD_PARTY_NOTICES.md dist\ >nul
xcopy /y /i /q LICENSES dist\LICENSES >nul
echo Build completata: dist\DocIndexer.exe (con LICENSE, NOTICE e licenze di terze parti)
echo Copia DocIndexer.exe nella cartella del programma insieme alla cartella tools
echo (il modello OCR principale e' incluso; gli altri alfabeti si scaricano da Impostazioni ^> Strumenti esterni).
