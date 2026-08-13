# Schema-Synchronisation: Tabellen + Indizes aus SAP erstellen

## Überblick

Der SchemaManager liest Tabellen- und Index-Metadaten aus SAP-DDIC-Tabellen und erstellt automatisch passende Tabellen + Indizes in Microsoft SQL Server.

## Wie es funktioniert

```
1. Python-Client liest DD03L (Felddefinitionen) via Z_EXECUTE_SQL
   → Feldname, INTTYPE, INTLEN, DECIMALS, KEYFLAG

2. Python-Client liest DD12L (Index-Header) via Z_EXECUTE_SQL
   → INDEXNAME, DBINDEX, UNIQUEFLAG

3. Python-Client liest DD17S (Index-Felder) via Z_EXECUTE_SQL
   → FIELDNAME, ASCDESC pro Index

4. Python-Client generiert CREATE TABLE + CREATE INDEX Statements
   → Führt sie auf MSSQL aus
```

## SAP-DDIC-Tabellen die abgefragt werden

| Tabelle | Inhalt | Verwendung |
|---|---|---|
| DD03L | Felddefinitionen (pro Tabelle) | Spaltennamen, Datentypen, Primärschlüssel |
| DD12L | Index-Header (pro Tabelle) | Indexnamen, Unique-Flag |
| DD17S | Index-Felder (pro Index) | Felder und Sortierrichtung pro Index |

## Datentyp-Mapping

| SAP INTTYPE | Beschreibung | MSSQL-Typ |
|---|---|---|
| C / CHAR | Character | NVARCHAR(n) |
| S / STRING | String variable Länge | NVARCHAR(MAX) |
| I / INT4 | Integer 4 Byte | INT |
| S2 / INT2 | Integer 2 Byte | SMALLINT |
| B / INT1 | Integer 1 Byte | TINYINT |
| N / NUMC | Numerisch als Character | NVARCHAR(n) |
| P / PACK | Packed Decimal | DECIMAL(p,s) |
| F / FLTP | Floating Point | FLOAT(53) |
| D / DATS | Datum | DATE |
| T / TIMS | Zeit | TIME(0) |
| X / RAW | Raw Binary | VARBINARY(n) |
| Y / LRAW | Long Raw | VARBINARY(MAX) |

## Indizes

### Primärschlüssel
- Aus DD03L: alle Felder mit `KEYFLAG = 'X'`
- Wird als `PRIMARY KEY` Constraint in der CREATE TABLE erstellt

### Sekundärindizes
- Aus DD12L: alle Indizes mit `AS4LOCAL = 'A'` (aktiv)
- Aus DD17S: Felder und Sortierrichtung (ASC/DESC) pro Index
- Unique-Flag wird übernommen
- Indexname: `IX_<TargetTable>_<SAPIndexName>`

### Beispiel

SAP MARA hat folgende Indizes:
```
Primary Key:  MANDT, MATNR
Index 0 (M~0): MANDT, MATNR (unique)
Index 1 (M~1): MANDT, MTART, MATNR
Index 2 (M~2): MANDT, MEINH, MATNR
```

In MSSQL wird erstellt:
```sql
CREATE TABLE dbo.MARA (
    [MANDT] NVARCHAR(3),
    [MATNR] NVARCHAR(18),
    [MTART] NVARCHAR(4),
    [MEINS] NVARCHAR(3),
    ...
    CONSTRAINT [PK_MARA] PRIMARY KEY ([MANDT], [MATNR])
);

CREATE UNIQUE NONCLUSTERED INDEX [IX_MARA_0] ON dbo.[MARA] ([MANDT] ASC, [MATNR] ASC);
CREATE NONCLUSTERED INDEX [IX_MARA_1] ON dbo.[MARA] ([MANDT] ASC, [MTART] ASC, [MATNR] ASC);
CREATE NONCLUSTERED INDEX [IX_MARA_2] ON dbo.[MARA] ([MANDT] ASC, [MEINH] ASC, [MATNR] ASC);
```

## Verwendung

### GUI-Client
Im Tab "Ausführen":
- **"Schema erstellen"** — Erstellt Tabelle + Indizes für die ausgewählte Tabelle
- **"Alle Schemata erstellen"** — Erstellt alle Tabellen + Indizes für alle aktiven Tabellen

Beide mit Bestätigungsdialog (DROP TABLE + CREATE TABLE).

### Kommandozeile
```cmd
:: Einzelne Tabelle
python sap_replicate.py --config config.json --sync-schema MARA

:: Alle Tabellen aus Config
python sap_replicate.py --config config.json --sync-schema-all
```

## Voraussetzungen

- `Z_EXECUTE_SQL` Funktionsbaustein muss installiert sein (für DD03L/DD12L/DD17S Abfragen)
- RFC-User muss Leseberechtigung für DD03L, DD12L, DD17S haben
- MSSQL-User muss CREATE TABLE und CREATE INDEX Berechtigung haben

## Workflow-Empfehlung

1. **Schema erstellen** — `--sync-schema-all` (einmalig, oder nach SAP-Änderungen)
2. **Erst-Load** — `--mode full` oder `--mode flatfile --window all`
3. **Regelmäßiger Sync** — `--mode timeframe --window day` oder CDC

Der Schema-Sync sollte immer vor dem ersten Daten-Load ausgeführt werden.