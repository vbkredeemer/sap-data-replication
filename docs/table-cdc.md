# Ansatz 1: Table CDC (Trigger-basiert)

## Prinzip

Change Data Capture (CDC) auf Basis von Datenbank-Triggern. Für jede zu replizierende Tabelle wird ein Trigger auf der SAP-HANA-Datenbank angelegt, der jede Änderung (INSERT, UPDATE, DELETE) in einer Log-Tabelle protokolliert. Ein Client-Skript liest die Log-Tabelle regelmäßig aus und synchronisiert die Änderungen in die Zieldatenbank.

Dieser Ansatz entspricht dem **Table CDC** Produkt von Theobald Software und dem **Trigger-based CDC** von Qlik Replicate.

## Architektur

```
SAP-System                                    Zieldatenbank (z.B. MS SQL Server)
┌─────────────────────────────┐               ┌──────────────────────────┐
│  MARA (Originaltabelle)     │               │  dbo.MARA (Kopie)        │
│  ↑ INSERT/UPDATE/DELETE     │               │                          │
│  │                          │               │                          │
│  │ Trigger feuert           │               │                          │
│  ↓                          │               │                          │
│  Z_MARA_CDC_LOG (Log-Tabelle)│              │                          │
│  ┌─────┬───────────┬──────┐ │               │                          │
│  │ SEQ │ OPERATION │ KEY  │ │               │                          │
│  │ 1   │ U         │ 1234 │ │               │                          │
│  │ 2   │ I         │ 5678 │ │               │                          │
│  │ 3   │ D         │ 9999 │ │               │                          │
│  └─────┴───────────┴──────┘ │               │                          │
│         ↑                   │               │                          │
│         │ Z_CDC_READ (RFC)  │               │                          │
│         │ JOIN mit MARA     │               │                          │
└─────────┼───────────────────┘               │                          │
          │                                    │                          │
          │  RFC (NWRFC SDK)                   │                          │
          ↓                                    │                          │
┌──────────────────────────────┐               │                          │
│  Python/C# Client            │─── INSERT/UPDATE/DELETE ───────────────→ │
│  (auf MSSQL-Server)          │               │                          │
│  1. Z_CDC_READ aufrufen      │               │                          │
│  2. Daten in MSSQL mergen    │               │                          │
│  3. Letzte SEQ speichern     │               │                          │
│  4. Z_CDC_CLEANUP aufrufen   │─── Z_CDC_CLEANUP (RFC) ────────────────→ │
└──────────────────────────────┘               └──────────────────────────┘
```

## Funktionsbausteine

Es werden drei RFC-fähige Funktionsbausteine benötigt, alle in der Funktionsgruppe `Z_SQL` (derselbe wie `Z_EXECUTE_SQL` und `Z_READ_TABLE`).

### 1. `Z_CDC_INIT` — CDC initialisieren

Legt die Log-Tabelle und die Trigger für eine Quelltabelle an. Idempotent — kann gefahrlos mehrfach aufgerufen werden.

**Parameter:**
- `IV_TABLE` (TABNAME) — Name der SAP-Quelltabelle (z.B. 'MARA')
- `IV_KEYFIELDS` (STRING) — Komma-separierte Liste der Primärschlüsselfelder (z.B. 'MATNR')
- `EV_LOG_TABLE` (TABNAME) — Name der erzeugten Log-Tabelle (z.B. 'Z_MARA_CDC_LOG')
- `EV_TRIGGER_EXISTS` (CHAR1) — 'X' wenn Trigger bereits existierte, ' ' wenn neu angelegt
- `EV_GAP_DETECTED` (CHAR1) — 'X' wenn Lücke erkannt (Trigger war weg, Log-Einträge fehlen)
- `EV_LAST_LOG_TIME` (TIMESTAMP) — Zeitstempel des letzten Log-Eintrags (bei Lücke relevant)
- `EV_ERROR` (STRING) — Fehlermeldung

**Logik:**
1. Prüfe ob Log-Tabelle `Z_<TABNAME>_CDC_LOG` existiert → falls nein, erzeugen
2. Prüfe ob Trigger `Z_<TABNAME>_CDC_TRG` existiert → falls ja, nichts tun
3. Falls Trigger nicht existiert:
   - Prüfe ob Log-Tabelle Einträge hat und letzter Zeitstempel alt ist → `EV_GAP_DETECTED = 'X'`
   - Erstelle Trigger für INSERT, UPDATE, DELETE
4. Log-Tabellen-Struktur:
   - `SEQ` (INT, auto-increment) — fortlaufende Sequenznummer
   - `OPERATION` (CHAR 1) — I (Insert), U (Update), D (Delete)
   - `KEYVALUES` (STRING) — Primärschlüsselwerte, pipe-delimited
   - `TIMESTMP` (TIMESTAMP) — Zeitpunkt der Änderung

### 2. `Z_CDC_READ` — Delta abholen

Liest die geänderten Datensätze aus der Log-Tabelle und joined mit der Originaltabelle um die vollständigen Daten zu liefern. Unterstützt Chunking wie `Z_READ_TABLE`.

**Parameter:**
- `IV_TABLE` (TABNAME) — Name der SAP-Quelltabelle
- `IV_FROM_SEQ` (INT) — Ab welcher Sequenznummer lesen (Lese-Pointer des Clients)
- `IV_CHUNK_SIZE` (INT) — Maximale Zeilen pro Aufruf (z.B. 10000)
- `EV_ROW_COUNT` (INT) — Tatsächlich zurückgegebene Zeilen
- `EV_NEXT_SEQ` (INT) — Nächste zu lesende Sequenznummer (für nächsten Aufruf)
- `EV_HAS_MORE` (CHAR1) — 'X' wenn weitere Daten verfügbar
- `EV_ERROR` (STRING) — Fehlermeldung
- `ET_FIELDS` (ZSQL_FIELD) — Spaltenmetadaten (wie Z_EXECUTE_SQL / Z_READ_TABLE)
- `ET_DATA` (ZSQL_ROW) — Pipe-delimited Zeilendaten, erweitert um OPERATION-Feld

**Logik:**
1. Log-Tabelle: `SELECT * FROM Z_<TABNAME>_CDC_LOG WHERE SEQ > IV_FROM_SEQ ORDER BY SEQ UP TO IV_CHUNK_SIZE ROWS`
2. Für jede Log-Zeile: JOIN mit Originaltabelle über KEYVALUES
   - OPERATION='I' oder 'U': Vollständige Zeile aus Originaltabelle
   - OPERATION='D': Nur Key-Felder (Zeile existiert nicht mehr in Originaltabelle)
3. Ergebnis als pipe-delimited ROWDATA mit Präfix `OPERATION|` vor den Daten
4. `ET_DATA`-Format: `I|MATNR|WERKS|...` (Insert), `U|MATNR|WERKS|...` (Update), `D|MATNR||` (Delete, nur Key)

### 3. `Z_CDC_CLEANUP` — Log aufräumen / Trigger entfernen

**Parameter:**
- `IV_TABLE` (TABNAME) — Name der SAP-Quelltabelle
- `IV_UP_TO_SEQ` (INT) — Log-Einträge bis zu dieser Sequenznummer löschen (0 = nur aufräumen)
- `IV_REMOVE_ALL` (CHAR1) — 'X' = Trigger und Log-Tabelle komplett löschen
- `EV_ERROR` (STRING) — Fehlermeldung

**Logik:**
- `IV_REMOVE_ALL = ' '`: `DELETE FROM Z_<TABNAME>_CDC_LOG WHERE SEQ <= IV_UP_TO_SEQ`
- `IV_REMOVE_ALL = 'X'`: Trigger löschen, Log-Tabelle löschen

## DDIC-Objekte

Es werden **keine neuen DDIC-Objekte** benötigt. Die Log-Tabelle wird dynamisch zur Laufzeit erzeugt (wie Theobald es macht). Die vorhandenen Typen `ZSQL_FIELD` und `ZSQL_ROW` aus dem ODBC-Projekt werden wiederverwendet.

## Trigger-Syntax (HANA)

```sql
-- INSERT-Trigger
CREATE TRIGGER Z_MARA_CDC_INS
AFTER INSERT ON MARA
REFERENCING NEW ROW AS new_row
FOR EACH ROW
BEGIN
  INSERT INTO Z_MARA_CDC_LOG (SEQ, OPERATION, KEYVALUES, TIMESTMP)
  VALUES (Z_MARA_CDC_SEQ.NEXTVAL, 'I', :new_row.MATNR, CURRENT_TIMESTAMP);
END;

-- UPDATE-Trigger
CREATE TRIGGER Z_MARA_CDC_UPD
AFTER UPDATE ON MARA
REFERENCING NEW ROW AS new_row
FOR EACH ROW
BEGIN
  INSERT INTO Z_MARA_CDC_LOG (SEQ, OPERATION, KEYVALUES, TIMESTMP)
  VALUES (Z_MARA_CDC_SEQ.NEXTVAL, 'U', :new_row.MATNR, CURRENT_TIMESTAMP);
END;

-- DELETE-Trigger
CREATE TRIGGER Z_MARA_CDC_DEL
AFTER DELETE ON MARA
REFERENCING OLD ROW AS old_row
FOR EACH ROW
BEGIN
  INSERT INTO Z_MARA_CDC_LOG (SEQ, OPERATION, KEYVALUES, TIMESTMP)
  VALUES (Z_MARA_CDC_SEQ.NEXTVAL, 'D', :old_row.MATNR, CURRENT_TIMESTAMP);
END;
```

## Der CDC-Zyklus

### Initialisierung (einmalig)
1. `Z_CDC_INIT` aufrufen für jede zu replizierende Tabelle
2. Log-Tabelle und Trigger werden angelegt
3. Initialer Full-Load der Tabelle (über `Z_READ_TABLE`) in die Zieldatenbank

### Wiederkehrender Delta-Load (z.B. nächtlich)
1. Client liest letzte SEQ aus Zieldatenbank (z.B. `SELECT last_seq FROM dbo.CDC_STATE WHERE table_name = 'MARA'`)
2. `Z_CDC_READ` aufrufen mit `IV_FROM_SEQ = last_seq`, `IV_CHUNK_SIZE = 10000`
3. Wenn `EV_HAS_MORE = 'X'`: weiteren Aufruf mit `IV_FROM_SEQ = EV_NEXT_SEQ`
4. Solange wiederholen bis `EV_HAS_MORE = ' '`
5. Pro empfangener Zeile:
   - `OPERATION = 'I'` → `INSERT INTO dbo.MARA ...`
   - `OPERATION = 'U'` → `MERGE` oder `UPDATE dbo.MARA SET ... WHERE MATNR = ...`
   - `OPERATION = 'D'` → `DELETE FROM dbo.MARA WHERE MATNR = ...`
6. `last_seq` in Zieldatenbank aktualisieren
7. `Z_CDC_CLEANUP` aufrufen mit `IV_UP_TO_SEQ = last_seq`

### Deinitialisierung (bei Bedarf)
- `Z_CDC_CLEANUP` mit `IV_REMOVE_ALL = 'X'` → Trigger und Log-Tabelle werden entfernt

## Performance-Überlegungen

### Trigger-Overhead

Der Trigger macht pro INSERT/UPDATE/DELETE auf der Quelltabelle **einen zusätzlichen INSERT** in die Log-Tabelle. Nur der Primärschlüssel wird geloggt, nicht die gesamte Zeile.

| Tabellentyp | Schreib-Last | Overhead | Spürbar? |
|---|---|---|---|
| Stammdaten (MARA, KNA1) | Gering | < 1% | Nein |
| Bewegungsdaten (VBAK, VBAP) | Mittel | 5-10% bei INSERTs | Leicht |
| Hochfrequent (MSEG, ACDOCA) | Hoch | 5-15% bei INSERTs | Merklich |

Die eigentlichen Daten werden nicht im Trigger geloggt — nur der Key. Die vollständigen Daten holt sich `Z_CDC_READ` über einen JOIN zur Laufzeit. Das hält den Trigger schnell.

### Delta-Transfer

Nach dem initialen Full-Load überträgt der Delta-Load nur noch die geänderten Sätze:

| Tabelle | Full-Load | Tägliche Deltas |
|---|---|---|
| MARA | ~500.000 Zeilen, 15-30 Min | ~1.000 Zeilen, < 1 Min |
| VBAK | ~2.000.000 Zeilen, 30-60 Min | ~50.000 Zeilen, 2-5 Min |
| ACDOCA | ~50.000.000 Zeilen, 1-2 Std | ~200.000 Zeilen, 10-15 Min |

## Vergleich mit Theobald Table CDC

Theobald Software verkauft genau diese Architektur als Teil von Xtract Universal / Xtract IS:

| Aspekt | Theobald Table CDC | Unser Ansatz |
|---|---|---|
| Trigger-basiert | Ja | Ja |
| Log-Tabelle in SAP | Ja (`/THEO/` Namespace) | Ja (`Z_` Namespace) |
| Custom Funktionsbausteine | `/THEO/CDC`, `/THEO/READ_TABLE` | `Z_CDC_*`, `Z_READ_TABLE` |
| SAP-zertifiziert | Ja | Nein (Custom) |
| Kosten | Lizenz erforderlich | Kostenlos |
| Funktionsumfang | CDC + ETL-Tool | CDC-Bausteine + eigenes Skript |
| Delta-Handling | Automatisch | Automatisch (durch Client-Skript) |
| DELETE-Erkennung | Ja | Ja |
| Upgrade-Sicherheit | Trigger muss neu angelegt werden | Gleiches Risiko |

Theobald's Funktionsbausteine: `/THEO/CLEAR_LOGTAB`, `/THEO/COUNT_LOGTAB_ENTRIES`, `/THEO/CREATE_LOG_TABLE`, `/THEO/CREATE_TRIGGERS`, `/THEO/DELETE_LOG_TABLE`, `/THEO/DELETE_TRIGGERS`, `/THEO/GET_DB`, `/THEO/GET_INFO`, `/THEO/GET_DB_TIMESTAMP`, `/THEO/GET_TRIGGERS`

Unsere Entsprechungen: `Z_CDC_INIT` (CREATE_LOG_TABLE + CREATE_TRIGGERS), `Z_CDC_READ` (lesen + joinen), `Z_CDC_CLEANUP` (CLEAR_LOGTAB + DELETE_TRIGGERS + DELETE_LOG_TABLE)