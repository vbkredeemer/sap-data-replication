# SAP Data Replication

Replikation von SAP-Tabellen in Fremdsysteme (z.B. Microsoft SQL Server) — ohne kommerzielle Produkte, ohne ODP-RFC, ohne SLT-Lizenz.

## Status

**Code ist implementiert** — drei ABAP-Funktionsbausteine + Python-Client-Skript.

## Lösungsansätze

### Ansatz 1: Table CDC (Trigger-basiert)
- Datenbank-Trigger auf SAP-Quelltabelle → Log-Tabelle → Delta-Abholung
- Entspricht Theobald Table CDC / Qlik Replicate (Trigger-Modus)
- Erfasst INSERT, UPDATE, DELETE automatisch
- [`docs/table-cdc.md`](docs/table-cdc.md)

### Ansatz 2: Zeitfenster-Delta (Trigger-frei)
- Lädt nur Sätze mit Änderungsdatum im aktuellen Zeitraum (Tag/Monat)
- Löscht den Zeitraum in der Zieldatenbank und lädt ihn neu
- Kein Trigger, keine Log-Tabelle, upgrade-sicher
- [`docs/timeframe-delta.md`](docs/timeframe-delta.md)

### Ansatz 3: Full Load
- Komplette Tabelle laden (TRUNCATE + INSERT)
- Für kleine Tabellen oder als Fallback
- Nutzt `Z_READ_TABLE` mit Chunking (10.000er Blöcke)

### Ansatz 4: Flatfile Export (schnellster für große Tabellen)
- ABAP schreibt CSV auf SAP-Server-Dateisystem
- Python-Client lädt Datei via SCP herunter
- BULK INSERT in MSSQL (10-50x schneller als INSERT-Batches)
- 3-5x schneller als RFC-basierte Übertragung bei Millionen Zeilen
- Zeitraum-Filter und Replace-Modi konfigurierbar

## Komponenten

### ABAP-Funktionsbausteine (`abap/`)

| Baustein | Zweck |
|---|---|
| `Z_CDC_INIT` | Log-Tabelle + Trigger erzeugen (idempotent, mit Lücken-Erkennung) |
| `Z_CDC_READ` | Delta abholen (Log JOIN Originaltabelle, mit Chunking) |
| `Z_CDC_CLEANUP` | Log aufräumen oder CDC komplett entfernen |
| `Z_READ_TABLE` | Chunked Table Read (aus dem ODBC-Projekt, wird hier vorausgesetzt) |
| `Z_EXPORT_TABLE` | Flatfile-Export: schreibt CSV auf SAP-Server-Dateisystem |
| `Z_DELETE_FILE` | Löscht eine Datei auf dem SAP-Server (Cleanup nach Import) |

### Python-Client (`client/`)

| Datei | Zweck |
|---|---|
| `gui_client.py` | **GUI-Client (PySide6/Qt)** — professioneller Desktop-Client mit 3 Tabs |
| `sap_replicate.py` | Kommandozeilen-Client: CDC, Timeframe, Full-Load, Flatfile Modi |
| `config.example.json` | Konfigurationsvorlage |
| `requirements.txt` | Python-Abhängigkeiten (pyrfc, pyodbc, PySide6) |
| `sap_replication_client.spec` | PyInstaller-Spec für standalone .exe |
| `INSTALL.md` | Installationsanleitung |

### Dokumentation (`docs/`)

| Dokument | Inhalt |
|---|---|
| `table-cdc.md` | Architektur, Trigger-Syntax, CDC-Zyklus, Performance |
| `timeframe-delta.md` | Zeitfenster-Logik, Vorteile/Nachteile, Hybrid-Strategie |
| `data-access.md` | Script (pyrfc) vs. ODBC-Treiber (Linked Server) |
| `risks-and-maintenance.md` | Trigger-Verlust, Lücken-Erkennung, Nightly-Check, Post-Import-Hook |
| `comparison.md` | Unser Treiber vs. Qlik vs. Theobald + SAP Note 3255746 |
| `type-conversion.md` | SAP→MSSQL Datentyp-Konvertierung (DATE, TIME, PACKED, RAW, etc.) |
| `schema-sync.md` | Tabellen + Indizes automatisch aus SAP-DDIC in MSSQL erstellen |

## Schnellstart

### GUI-Client (empfohlen)
1. SAP-Bausteine installieren (SE37, Funktionsgruppe Z_SQL)
2. `pip install -r requirements.txt` (pyrfc, pyodbc, PySide6)
3. `python gui_client.py` starten
4. In "Verbindungen": SAP, SQL Server, SSH konfigurieren + testen
5. In "Tabellen": Tabellen hinzufügen, Modus pro Tabelle wählen
6. In "Ausführen": Sync starten

### Kommandozeile
1. SAP-Bausteine installieren (SE37, Funktionsgruppe Z_SQL)
2. `pip install pyrfc pyodbc`
3. `config.example.json` kopieren zu `config.json` und anpassen
4. `python sap_replicate.py --config config.json --init-only` (CDC initialisieren)
5. `python sap_replicate.py --config config.json --table MARA --mode full` (Erst-Load)
6. `python sap_replicate.py --config config.json` (Regelmäßiger Sync)

### Standalone .exe bauen
```cmd
pip install pyinstaller
pyinstaller sap_replication_client.spec
:: → dist\SAPDataReplication.exe
```

Siehe [`client/INSTALL.md`](client/INSTALL.md) für Details.

## Verwandte Projekte

- **ODBC-Treiber:** https://github.com/vbkredeemer/sap-odbc-abap
- **JDBC-Treiber:** https://github.com/vbkredeemer/sap-jdbc-abap

## Lizenz

GPL-3.0

## Ausgangslage

SAP-Tabellen sollen regelmäßig in eine Zieldatenbank (z.B. MS SQL Server) synchronisiert werden. Die Herausforderungen:

- **Kein direkter Datenbankzugriff** — lizenzrechtlich soll der Zugriff über den SAP-Applikationsserver (RFC) erfolgen
- **Große Datenmengen** — Tabellen wie ACDOCA, MSEG, VBAP können Millionen von Datensätzen haben
- **Delta-Handling** — Nach dem initialen Full-Load sollen nur noch Änderungen übertragen werden
- **SAP Note 3255746** — Seit Juni 2026 blockiert SAP die ODP-RFC-Schnittstelle für Drittanbieter (Qlik, Microsoft, Fivetran, Talend, Informatica sind betroffen)

Dieses Projekt beschreibt zwei Lösungsansätze, die beide auf **Custom-Funktionsbausteinen** basieren und daher nicht von SAP blockiert werden können.

## Inhalte

- [`docs/table-cdc.md`](docs/table-cdc.md) — Ansatz 1: Trigger-basiertes Table CDC (wie Theobald Software)
- [`docs/timeframe-delta.md`](docs/timeframe-delta.md) — Ansatz 2: Zeitfenster-Delta über Änderungsdatum (trigger-frei)
- [`docs/data-access.md`](docs/data-access.md) — Datenabruf: Direktes Script vs. ODBC-Treiber
- [`docs/risks-and-maintenance.md`](docs/risks-and-maintenance.md) — Risiken, Trigger-Wiederherstellung, Monitoring
- [`docs/comparison.md`](docs/comparison.md) — Gegenüberstellung: Unser ODBC-Treiber vs. Qlik SAP Connector vs. Theobald Software

## Verwandte Projekte

- **ODBC-Treiber:** https://github.com/vbkredeemer/sap-odbc-abap — Nativer ODBC-Treiber mit Dual-Mode (Z_EXECUTE_SQL für komplexe Queries, Z_READ_TABLE für Full-Table-Read mit Chunking)
- **JDBC-Treiber:** https://github.com/vbkredeemer/sap-jdbc-abap — JDBC-Treiber für DBeaver und Java-Anwendungen

## Lizenz

GPL-3.0