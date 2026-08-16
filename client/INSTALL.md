# SAP Data Replication — Installation

## Voraussetzungen

### SAP-Seite
1. Funktionsgruppe `Z_SQL` muss existieren (dieselbe wie für ODBC-Treiber / Z_EXECUTE_SQL)
2. DDIC-Typen `ZSQL_FIELD` und `ZSQL_ROW` müssen vorhanden sein
3. Funktionsbausteine installieren:
   - `Z_CDC_INIT` (Remote-Enabled ✓) — aus `abap/Z_CDC_INIT.abap`
   - `Z_CDC_READ` (Remote-Enabled ✓) — aus `abap/Z_CDC_READ.abap`
   - `Z_CDC_CLEANUP` (Remote-Enabled ✓) — aus `abap/Z_CDC_CLEANUP.abap`
   - `Z_READ_TABLE` (Remote-Enabled ✓) — aus dem ODBC-Projekt (`abap/Z_READ_TABLE.abap`)
4. RFC-User mit Berechtigung für `Z_CDC_*` und `Z_READ_TABLE`

### Client-Seite (Windows / MSSQL Server)
1. **Python 3.10+** — https://python.org
2. **pyrfc** — `pip install pyrfc`
3. **pyodbc** — `pip install pyodbc`
4. **SAP NWRFC SDK** — `libsapnwrfc.dll` in `C:\Windows\System32` oder im PATH
5. **ODBC Driver for SQL Server** — Microsoft SQL Server ODBC Driver 17 oder 18
6. **Zieldatenbank** — SQL Server Datenbank mit Tabellen die den SAP-Tabellen entsprechen

## Installation

### Variante A: GUI-Client (empfohlen)

```cmd
:: Abhängigkeiten installieren
pip install -r requirements.txt

:: GUI-Client starten
python gui_client.py

:: Oder als standalone .exe bauen (optional)
pip install pyinstaller
pyinstaller sap_replication_client.spec
:: → dist\SAPDataReplication.exe
```

Der GUI-Client hat vier Tabs:
1. **Verbindungen** — SAP, SQL Server, SSH konfigurieren + testen
2. **Tabellen** — Tabellen-Liste mit Modus-Konfiguration pro Tabelle
3. **Ausführen** — Sync starten, CDC initialisieren, Schema erstellen, Log-Ausgabe
4. **Zeitplan** — Eingebauter Scheduler + Windows-Aufgabe erstellen

### Variante B: Kommandozeile

```cmd
:: Abhängigkeiten installieren
pip install pyrfc pyodbc

:: Konfiguration kopieren und anpassen
copy config.example.json config.json
notepad config.json

:: Ausführen
python sap_replicate.py --config config.json
```

### 1. SAP-Funktionsbausteine anlegen

Für jeden Baustein in SE37:
1. Funktionsgruppe: `Z_SQL`
2. Attributes: Remote-Enabled Module ✓
3. Interface wie im Kommentar des jeweiligen ABAP-Files dokumentiert
4. Source Code aus `abap/` Verzeichnis einfügen
5. Aktivieren

### 2. Python-Client einrichten

```cmd
:: Abhängigkeiten installieren
pip install pyrfc pyodbc

:: Konfiguration kopieren und anpassen
copy config.example.json config.json
notepad config.json
```

### 3. Zieldatenbank vorbereiten

Die Zieldatenbank muss Tabellen enthalten die den SAP-Tabellen entsprechen.
Spaltennamen müssen übereinstimmen (Groß-/Kleinschreibung beachten).

```sql
-- Beispiel: MARA Tabelle anlegen
CREATE TABLE dbo.MARA (
    MATNR NVARCHAR(18) PRIMARY KEY,
    MTART NVARCHAR(4),
    MEINS NVARCHAR(3),
    LAEDA NVARCHAR(8),
    -- ... weitere Felder
);

-- CDC_STATE Tabelle (wird automatisch vom Client angelegt)
-- Wird für CDC-Modus benötigt
```

### 4. CDC initialisieren (nur für CDC-Modus)

Für Tabellen die im CDC-Modus betrieben werden sollen:

```cmd
python sap_replicate.py --config config.json --init-only --table MARA
```

Das erzeugt Log-Tabelle und Trigger im SAP-System.

### 5. Ersten Full-Load machen

Bevor CDC-Delta-Sync läuft, muss die Zieltabelle einmal komplett gefüllt werden:

```cmd
python sap_replicate.py --config config.json --table MARA --mode full
```

### 6. Regelmäßigen Sync einrichten

**SQL Server Agent Job (täglich um 20:00):**
```cmd
python C:\sap-repl\sap_replicate.py --config C:\sap-repl\config.json
```

## Konfiguration

### config.json Format

```json
{
  "sap": {
    "ashost": "sap-prod.firma.de",
    "sysnr": "10",
    "client": "100",
    "user": "RFC_USER",
    "password": "********",
    "lang": "EN"
  },
  "sql_server": {
    "connection_string": "Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=SAP_REPL;Trusted_Connection=yes;"
  },
  "tables": [
    {
      "name": "MARA",
      "mode": "cdc",
      "key_fields": "MATNR",
      "chunk_size": 10000,
      "active": true
    },
    {
      "name": "VBAK",
      "mode": "timeframe",
      "delta_field": "AEDAT",
      "window": "day",
      "chunk_size": 10000,
      "active": true
    },
    {
      "name": "T001W",
      "mode": "full",
      "chunk_size": 10000,
      "active": true
    }
  ]
}
```

### Modi

| Modus | Beschreibung | Voraussetzung |
|---|---|---|
| `cdc` | Trigger-basiertes CDC mit automatischem Delta | Z_CDC_INIT ausgeführt, Full-Load gemacht |
| `timeframe` | Zeitfenster-Delta über Änderungsdatum | Tabelle hat AEDAT/LAEDA Feld |
| `full` | Komplette Tabelle laden (TRUNCATE + INSERT) | Keine |
| `flatfile` | CSV-Export vom SAP-Server + BULK INSERT | SSH-Zugang zum SAP-Server |

### Window-Optionen (für timeframe-Modus)

| Window | Beschreibung |
|---|---|
| `day` | Aktueller Tag ( YYYYMMDD) |
| `week` | Aktuelle Woche (Montag-basiert) |
| `month` | Aktueller Monat (YYYYMM01) |
| `year` | Aktuelles Jahr (YYYY0101) |

## Kommandozeilen-Optionen

```
python sap_replicate.py --config config.json                          # Alle Tabellen syncen
python sap_replicate.py --config config.json --table MARA             # Nur MARA
python sap_replicate.py --config config.json --table MARA --mode cdc # Mode überschreiben
python sap_replicate.py --config config.json --table VBAK --mode timeframe --window day
python sap_replicate.py --config config.json --init-only              # Nur Trigger prüfen/anlegen
python sap_replicate.py --config config.json --remove-cdc MARA        # CDC für MARA entfernen
python sap_replicate.py --config config.json --import-tables C:\Scripts\SAP_ODBC\queried_tables.txt
```

### Tabellen aus ODBC-Treiber-Log importieren

Der SAP ODBC-Treiber protokolliert alle abgefragten Tabellen in einer Textdatei
(`queried_tables.txt`, eine Tabelle pro Zeile). Mit `--import-tables` können diese
Tabellen in die Konfiguration importiert werden:

```cmd
python sap_replicate.py --config config.json --import-tables C:\Scripts\SAP_ODBC\queried_tables.txt
```

**Verhalten:**
- Neue Tabellen werden mit `mode=full`, `active=false`, `chunk_size=10000`, `fields=*` hinzugefügt
- Bereits vorhandene Tabellen werden übersprungen (Deduplizierung, Case-insensitive)
- Die Konfiguration wird direkt in der `config.json` aktualisiert
- Importierte Tabellen sind **inaktiv** — sie werden beim Sync übersprungen
- Aktivieren Sie Tabellen manuell in der GUI (Checkbox "Aktiv") oder durch Setzen von `"active": true` in der config.json

**Log-Ausgabe:**
```
Imported 15 new tables from C:\Scripts\SAP_ODBC\queried_tables.txt, 3 already existed
  New tables (inactive, review and activate):
    EKKO
    EKPO
    ...
```

**Im GUI-Client:** Tab "Tabellen" → "Import" Button → Datei auswählen → Tabellen werden als inaktiv hinzugefügt → "Speichern"

## Monitoring

### Log-Tabellengröße prüfen (auf SAP)

```sql
-- In SAP via SE16 oder SQL Console:
SELECT COUNT(*) FROM Z_MARA_CDC_LOG;
SELECT MAX(SEQ), MAX(TIMESTMP) FROM Z_MARA_CDC_LOG;
```

### CDC-State prüfen (auf SQL Server)

```sql
SELECT * FROM CDC_STATE ORDER BY table_name;
```

### Trigger-Status prüfen (auf SAP via SQL Console)

```sql
SELECT TRIGGER_NAME FROM SYS.TRIGGERS 
WHERE TRIGGER_NAME LIKE 'Z_%_CDC_TRG_%';
```

## Troubleshooting

| Problem | Lösung |
|---|---|
| `Cannot connect to SAP` | NWRFC SDK nicht im PATH, falsche Credentials |
| `Cannot create log table` | RFC-User braucht CREATE TABLE Berechtigung |
| `Cannot create trigger` | RFC-User braucht CREATE TRIGGER Berechtigung |
| `GAP DETECTED` | Trigger war weg — Full-Load ausführen: `--table X --mode full` |
| `Z_READ_TABLE error` | Tabelle existiert nicht oder keine Berechtigung |
| `INSERT failed` | Zieltabelle hat nicht die richtigen Spalten |
| `pyrfc not found` | `pip install pyrfc` + NWRFC SDK installieren |