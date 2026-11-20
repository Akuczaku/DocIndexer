# DocIndexer — język polski

DocIndexer indeksuje dokumenty lokalnie na komputerze z Windows. Autor oryginalnego programu: Andrea Cumini — andrea@osintinfo.net — www.osintinfo.net.

Wersja zawierająca to tłumaczenie udostępnia **Polski** w menu **Language / Język** u góry okna i podczas pierwszego uruchomienia. Tłumaczenie obejmuje 518 tekstów interfejsu oraz warunki użytkowania.

## Uruchomienie ze źródeł

Wymagany jest Python 3.10 lub nowszy. W folderze projektu wykonaj:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

Możesz też użyć `avvia.bat` po przygotowaniu środowiska i zależności. W przypadku gotowego EXE wybierz wydanie, które zawiera już polskie tłumaczenie: samo skopiowanie plików źródłowych obok starszego EXE nie dodaje języka.

[Skrócona instrukcja po polsku](docs/manual/pl/MANUAL.md) jest dostępna również przez przycisk instrukcji w programie. Nie zawiera polskich zrzutów ekranu.

## Informacje o tłumaczeniu

Polskie tłumaczenie dodano 20.11.2026. Zachowano dotychczasowy język domyślny i ustawienia użytkownika. Operatory wyszukiwania AND, OR, NOT i NEAR pozostają bez zmian. Wzorce dotyczące włoskich numerów i dokumentów nadal są opisane jako włoskie.

Licencja: GNU AGPL v3 z dodatkowymi warunkami w pliku NOTICE. Informacje o autorze, licencjach i składnikach zewnętrznych znajdują się w LICENSE, NOTICE i THIRD_PARTY_NOTICES.md.
