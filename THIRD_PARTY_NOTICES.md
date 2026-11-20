# Third-party components

DocIndexer is distributed under the **GNU AGPL-3.0** with the additional terms in `NOTICE`
(see `LICENSE`). This file lists what the project uses, includes or downloads, with its license
and any required attribution. Items marked **[bundled]** end up inside the compiled executable
(`build.bat`, PyInstaller); **[downloaded]** items are fetched by the user at runtime into the
`tools` folder; **[build]** items are only needed to build.

Last checked: November 2026.

---

## 1. Python libraries

| Component | Use | License | Notes |
|---|---|---|---|
| Python and standard library | runtime [bundled] | PSF License 2.0 | https://docs.python.org/3/license.html |
| [PyMuPDF](https://pymupdf.readthedocs.io) (Artifex) | PDF text, rendering, preview [bundled] | **GNU AGPL-3.0** or Artifex commercial license | the reason DocIndexer is AGPL; source at https://github.com/pymupdf/PyMuPDF |
| ↳ pymupdf-layout, pymupdf4llm | installed by pip with PyMuPDF, **not used** | no license in package metadata | excluded from the executable (`--exclude-module` in `build.bat`) |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | OCR engine [bundled] | Apache-2.0 | text in `LICENSES/Apache-2.0.txt` |
| ↳ [omegaconf](https://github.com/omry/omegaconf), PyYAML, pyclipper, shapely, colorlog, tqdm, six, requests | RapidOCR dependencies [bundled] | BSD-3-Clause, MIT, MIT, BSD-3-Clause, MIT, MPL-2.0 + MIT, MIT, Apache-2.0 | |
| [ONNX Runtime](https://onnxruntime.ai) (Microsoft) | runs the OCR models [bundled] | MIT | |
| ↳ flatbuffers, protobuf, numpy | ONNX Runtime dependencies [bundled] | Apache-2.0, BSD-3-Clause, BSD-3-Clause (+ 0BSD, MIT, Zlib, CC0 parts) | |
| [OpenCV](https://opencv.org) (opencv-python-headless) | image handling in RapidOCR [bundled] | Apache-2.0 | the wheel also ships an FFmpeg library (`opencv_videoio_ffmpeg*.dll`, GNU LGPL-2.1), see the package's `LICENSE-3RD-PARTY.txt`; DocIndexer does not use video |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | GUI [bundled] | MIT, © 2023 Tom Schimansky | |
| ↳ Roboto fonts inside CustomTkinter | GUI [bundled] | Apache-2.0, © Google | not used by the theme, shipped with the package |
| ↳ darkdetect, packaging | CustomTkinter dependencies [bundled] | BSD-3-Clause; Apache-2.0 or BSD-2-Clause | |
| [Pillow](https://python-pillow.org) | images, preview, logo [bundled] | MIT-CMU (HPND) | |
| [python-docx](https://github.com/python-openxml/python-docx) + lxml | Word .docx [bundled] | MIT; BSD-3-Clause | |
| [openpyxl](https://openpyxl.readthedocs.io) + et_xmlfile | Excel .xlsx [bundled] | MIT; MIT | |
| [xlrd](https://github.com/python-excel/xlrd) | Excel .xls [bundled] | BSD | |
| [olefile](https://github.com/decalage2/olefile) | old Office files [bundled] | BSD-2-Clause | |
| [pywin32](https://github.com/mhammond/pywin32) | Word / PowerPoint via COM [bundled] | PSF License | Windows only |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | fuzzy search [bundled] | MIT | |
| networkx, tabulate | PyMuPDF dependencies | BSD-3-Clause, MIT | |
| [psutil](https://github.com/giampaolo/psutil) | low priority, CPU limit, idle/battery detection [bundled] | BSD-3-Clause | |
| [zstandard](https://github.com/indygreg/python-zstandard) | compression of the stored text [bundled] | BSD-3-Clause; bundles the zstd library (BSD-3-Clause, © Meta) | |
| [watchdog](https://github.com/gorakhargosh/watchdog) | real-time folder monitoring [bundled] | Apache-2.0 | |
| [pystray](https://github.com/moses-palmer/pystray) | icon in the notification area [bundled] | **GNU LGPL-3.0** | used as an unmodified library; with the onefile executable the user can still replace it by running DocIndexer from source |
| [extract-msg](https://github.com/TeamMsgExtractor/msg-extractor) | Outlook .msg e-mails [bundled] | **GNU GPL-3.0** | compatible with the AGPL-3.0 (sections 13 of both licenses) |
| ↳ beautifulsoup4, compressed-rtf, RTFDE, ebcdic, red-black-tree-mod, tzlocal | extract-msg dependencies [bundled] | MIT, MIT, LGPL-3.0, MIT, Apache-2.0, MIT | |
| [pillow-heif](https://github.com/bigcat88/pillow_heif) | HEIC/HEIF/AVIF photos [bundled] | source BSD-3-Clause; the Windows wheel declares **GPL-2.0** as a whole because it ships libheif (LGPL-3.0), libde265 (LGPL-3.0), x265 (GPL-2.0-or-later) and the MinGW runtime (GPL-3.0 with GCC exception) | x265 is "version 2 or any later version", so the combination can be distributed under the AGPL-3.0; licenses in `pillow_heif-*.dist-info/licenses/LICENSES_bundled.txt`. It is an optional import: without it HEIC photos are skipped |
| [dbfread](https://github.com/olemb/dbfread) | dBASE/FoxPro .dbf databases [bundled] | MIT | |
| [pyodbc](https://github.com/mkleehammer/pyodbc) | Access .mdb/.accdb through the Microsoft ODBC driver [bundled] | MIT-0 | the driver itself comes with Microsoft Office / Access Database Engine and is not distributed |
| [PyInstaller](https://pyinstaller.org) | [build] | GPL-2.0 with bootloader exception | the exception allows any license for the built program |

When distributing the compiled executable, copy `LICENSE`, `NOTICE`, this file and the `LICENSES`
folder next to it, and make the source code of the same version available (AGPL section 6).

## 2. OCR models [bundled / downloaded]

| Model | Use | License |
|---|---|---|
| PaddleOCR PP-OCRv6 small (detection + recognition, multilingual) | default model, shipped inside the `rapidocr` package and copied to `tools/rapidocr` | Apache-2.0, © PaddlePaddle Authors |
| PaddleOCR PP-OCRv5 mobile (detection + Cyrillic, East Slavic, Greek, Arabic, Korean, Thai, Devanagari, Tamil, Telugu recognition) | optional, downloaded on request from modelscope.cn (RapidOCR mirror) with SHA-256 check | Apache-2.0, © PaddlePaddle Authors |
| PaddleOCR text-line orientation classifier (ch_ppocr_mobile_v2.0_cls) | 180° line rotation | Apache-2.0, © PaddlePaddle Authors |

The ONNX conversions are published by the RapidOCR project (Apache-2.0).
PaddleOCR: https://github.com/PaddlePaddle/PaddleOCR.

## 3. Add-on tools [downloaded]

| Tool | Use | License | Notes |
|---|---|---|---|
| [ExifTool](https://exiftool.org) by Phil Harvey | file metadata (photos, documents, audio, video) into the `meta_items` table | Perl Artistic License or GPL (same terms as Perl) | downloaded on request from Settings > External tools (official Windows package, SourceForge mirror) into `tools/exiftool`, run as a separate program |
| [7-Zip](https://www.7-zip.org) by Igor Pavlov | text of the files inside zip, rar, 7z, tar, gz archives into the `archive_items` table | GNU LGPL-2.1 + BSD 3-clause parts + unRAR license restriction (RAR code may not be used to re-create the RAR compression algorithm) | `7zr.exe` and the official installer are downloaded on request; 7z.exe and 7z.dll are extracted into `tools/7zip` (not installed), run as a separate program |

These tools are not part of DocIndexer and are not shipped with it.

## 3. Programs used if present

**Microsoft Word / PowerPoint** are only used through COM, when already installed, to read old
`.doc`/`.ppt` files and to make previews. They are not distributed and not required.

## 4. Logo and Panchang typeface

The logo uses the Panchang typeface (Indian Type Foundry, Fontshare Free Font License):
`tools/make_logo.py` regenerates it when the font is installed. The font file is not included in
the repository nor in the program: only the rendered images are.
