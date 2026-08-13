# Datentyp-Konvertierung: SAP → MSSQL

## Übersicht

Alle Datenübertragungswege (ODBC-Treiber, CDC, Timeframe, Flatfile) liefern Daten als pipe-delimited Text. Die ABAP-Funktionsbausteine konvertieren die SAP-Datentypen bereits beim Export in MSSQL-kompatible Formate, sodass BULK INSERT und pyodbc die Werte direkt verarbeiten können.

## Konvertierungstabelle

| SAP-Typ | ABAP typekind | SAP-Wert (intern) | Konvertiert (CSV/RFC) | MSSQL-Typ |
|---|---|---|---|---|
| CHAR (C) | typekind_char | `MATNR123   ` | `MATNR123` (trailing spaces entfernt) | VARCHAR / NVARCHAR |
| STRING | typekind_string | `Langer Text` | `Langer Text` | NVARCHAR(MAX) |
| INT4 (I) | typekind_int | `12345` | `12345` | INT |
| INT2 | typekind_int2 | `12345` | `12345` | SMALLINT |
| INT1 | typekind_int1 | `123` | `123` | TINYINT |
| PACKED (P) | typekind_packed | `1234.56` (intern) | `1234.56` (Dezimalpunkt, keine Tausendertrennung) | DECIMAL(p,s) |
| FLOAT (F) | typekind_float | `1.234E+05` | `123400.0` (Dezimalpunkt) | FLOAT |
| DATE (D) | typekind_date | `20260813` | `2026-08-13` (ISO-Format) | DATE |
| TIME (T) | typekind_time | `120000` | `12:00:00` (ISO-Format) | TIME |
| RAW (X) | typekind_hex | Hex-Binary | `0x414243` (Hex-String mit 0x-Präfix) | VARBINARY |

## Wo die Konvertierung stattfindet

Die Konvertierung passiert **ABAP-seitig** in den Funktionsbausteinen, nicht client-seitig:

| Baustein | Projekt | Konvertierung |
|---|---|---|
| `Z_EXPORT_TABLE` | sap-data-replication | ✅ Typbewusst (CSV-Export) |
| `Z_CDC_READ` | sap-data-replication | ✅ Typbewusst (CDC-Delta) |
| `Z_READ_TABLE` | sap-odbc-abap | ✅ Typbewusst (Chunked Read) |
| `Z_EXECUTE_SQL` | sap-odbc-abap | ⚠️ ADBC liefert Text, ODBC-Treiber mappt Typen via ET_FIELDS |

## Details pro Typ

### DATE (D) — SAP-Datum

SAP speichert Datumsfelder als `CHAR 8` im Format `YYYYMMDD` (z.B. `20260813`).

**Konvertierung:** ABAP formatiert zu `YYYY-MM-DD` (ISO 8601), das MSSQL direkt als DATE einliest.

```
SAP:  20260813  →  ABAP konvertiert  →  CSV: 2026-08-13  →  MSSQL: DATE '2026-08-13'
```

Leere Datumsfelder (`00000000` oder Initial) werden als leerer String geliefert → MSSQL setzt NULL.

### TIME (T) — SAP-Zeit

SAP speichert Zeitfelder als `CHAR 6` im Format `HHMMSS` (z.B. `120000`).

**Konvertierung:** ABAP formatiert zu `HH:MM:SS`, das MSSQL als TIME einliest.

```
SAP:  120000  →  ABAP konvertiert  →  CSV: 12:00:00  →  MSSQL: TIME '12:00:00'
```

### PACKED (P) — SAP-Dezimalzahl

SAP speichert gepackte Zahlen intern im BCD-Format. Die ABAP-Ausgabe mit `WRITE ... NO-GROUPING` liefert eine Dezimalzahl ohne Tausendertrennzeichen.

**Dezimaltrennzeichen:** ABAP verwendet standardmäßig das Komma (deutsche Locale). Die Konvertierung ersetzt Komma durch Punkt für MSSQL-Kompatibilität.

```
SAP:  1.234,56 (intern)  →  WRITE NO-GROUPING  →  1234,56  →  Komma→Punkt  →  1234.56
```

### FLOAT (F) — SAP-Gleitkommazahl

Wie PACKED, aber mit `WRITE ... NO-GROUPING` und Komma-zu-Punkt-Konvertierung.

### RAW (X) — SAP-Binärdaten

SAP speichert RAW-Felder als Byte-Folge. ABAP konvertiert zu einem Hex-String mit `0x`-Präfix, das MSSQL's BULK INSERT als VARBINARY erkennt.

```
SAP:  41 42 43 (Bytes)  →  ABAP konvertiert  →  0x414243  →  MSSQL: VARBINARY
```

### CHAR (C) / STRING

Zeichenketten werden unverändert geliefert, nur führende und nachfolgende Leerzeichen werden entfernt. Interne Leerzeichen bleiben erhalten.

```
SAP:  "  Hello World  "  →  ABAP konvertiert  →  "Hello World"
```

## ODBC-Treiber Besonderheit

Der ODBC-Treiber (`sapodbcabap.dll`) nutzt `Z_READ_TABLE` und `Z_EXECUTE_SQL`. Bei `Z_READ_TABLE` erfolgt die Konvertierung ABAP-seitig (wie oben). Bei `Z_EXECUTE_SQL` liefert ADBC die Daten als Text — der ODBC-Treiber kennt aber die Feldtypen aus `ET_FIELDS` und mappt diese auf ODBC-SQL-Typen:

```
ET_FIELDS: DATATYPE='D'  →  ODBC: SQL_TYPE_DATE  →  Excel/Power BI zeigt Datum
ET_FIELDS: DATATYPE='I'  →  ODBC: SQL_INTEGER    →  Excel/Power BI zeigt Zahl
ET_FIELDS: DATATYPE='C'  →  ODBC: SQL_VARCHAR    →  Excel/Power BI zeigt Text
```

Bei `SQLGetData` konvertiert der ODBC-Treiber den String-Wert in den vom Client angeforderten C-Typ (`fCType`-Parameter).

## Zieltabellen-Empfehlung für MSSQL

Bei der Erstellung der MSSQL-Zieltabellen sollten die Spaltentypen zu den SAP-Typen passen:

| SAP-Typ | Empfohlener MSSQL-Typ | Bemerkung |
|---|---|---|
| CHAR(n) | NVARCHAR(n) | Unicode für Umlaute |
| STRING | NVARCHAR(MAX) | Variable Länge |
| INT4 | INT | |
| INT2 | SMALLINT | |
| INT1 | TINYINT | |
| PACKED(p,s) | DECIMAL(p,s) | Precision und Scale aus DDIC |
| FLOAT | FLOAT(53) | |
| DATE | DATE | |
| TIME | TIME(0) | |
| RAW(n) | VARBINARY(n) | |

## BULK INSERT Kompatibilität

Die von `Z_EXPORT_TABLE` erzeugten CSV-Dateien sind direkt kompatibel mit MSSQL BULK INSERT:

```sql
BULK INSERT dbo.MARA
FROM 'C:\temp\MARA_20260813_140000.csv'
WITH (
    FORMAT = 'CSV',
    FIELDTERMINATOR = '|',
    ROWTERMINATOR = '\n',
    FIRSTROW = 2,  -- skip header
    TABLOCK,
    ROWS_PER_BATCH = 50000
)
```

Die Datums- und Zeitformate werden von MSSQL automatisch erkannt:
- `2026-08-13` → DATE
- `12:00:00` → TIME
- `1234.56` → DECIMAL (mit Punkt als Dezimaltrennzeichen)