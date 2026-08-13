# Ansatz 2: Zeitfenster-Delta über Änderungsdatum (trigger-frei)

## Prinzip

Statt Trigger und Log-Tabelle zu verwenden, wird das Änderungsdatum der SAP-Tabelle genutzt um nur geänderte Datensätze abzurufen. Der aktuelle Zeitraum (z.B. aktueller Monat oder aktueller Tag) wird in der Zieldatenbank gelöscht und neu geladen. So werden INSERTs, UPDATEs und DELETEs erfasst, ohne dass ein Trigger benötigt wird.

## Architektur

```
SAP-System                                    Zieldatenbank (z.B. MS SQL Server)
┌─────────────────────────────┐               ┌──────────────────────────┐
│  ACDOCA (Originaltabelle)   │               │  dbo.ACDOCA (Kopie)      │
│  Felder: ... AEDAT ...      │               │                          │
│                             │               │  1. DELETE aktuellen Mon.│
│                             │               │  2. INSERT aktuellen Mon.│
└─────────┬───────────────────┘               └──────────┬───────────────┘
          │                                               │
          │  Z_READ_TABLE (RFC, mit Chunking)             │
          │  WHERE AEDAT >= '20260801'                    │
          ↓                                               │
┌──────────────────────────────┐                         │
│  Python/C# Client            │──────────────────────→  │
│  (auf MSSQL-Server)          │   DELETE + INSERT        │
│                              │                         │
│  1. DELETE Zeitraum in MSSQL │                         │
│  2. Z_READ_TABLE mit WHERE   │                         │
│  3. INSERT in MSSQL          │                         │
└──────────────────────────────┘
```

## Funktionsweise

### Beispiel: Aktueller Monat

```sql
-- Schritt 1: Aktuellen Monat in Zieldatenbank löschen
DELETE FROM dbo.ACDOCA WHERE AEDAT >= '20260801';

-- Schritt 2: Aktuellen Monat aus SAP neu laden
-- (via ODBC-Treiber oder direktem RFC-Skript)
SELECT * FROM ACDOCA WHERE AEDAT >= '20260801';
-- → Z_READ_TABLE wird automatisch in 10.000er Blöcken chunked

-- Schritt 3: In Zieldatenbank einfügen
INSERT INTO dbo.ACDOCA SELECT * FROM ...;
```

Am 1. September wird August nicht mehr angetastet (abgeschlossen), stattdessen:
```sql
DELETE FROM dbo.ACDOCA WHERE AEDAT >= '20260901';
SELECT * FROM ACDOCA WHERE AEDAT >= '20260901';
INSERT INTO dbo.ACDOCA ...;
```

### Beispiel: Täglicher Delta-Load

Für hochfrequente Tabellen kann statt des Monats der aktuelle Tag geladen werden:

```sql
-- Jeden Abend: nur heutige Änderungen
DELETE FROM dbo.ACDOCA WHERE AEDAT = '20260813';
SELECT * FROM ACDOCA WHERE AEDAT = '20260813';
```

Das wären bei ACDOCA vielleicht 100.000-200.000 Sätze pro Tag — mit `Z_READ_TABLE` Chunking in 5-10 Minuten erledigt.

## Vorteile

| Aspekt | Bewertung |
|---|---|
| Kein Trigger nötig | ✅ Keine Datenbank-Trigger, keine Log-Tabelle |
| Keine Modifikation an SAP-Tabellen | ✅ Keine DDIC-Änderung, kein SPDD/SPAU |
| Upgrade-sicher | ✅ Nichts kann wegfliegen |
| DELETEs werden erfasst | ✅ Gesamter Zeitraum wird ersetzt |
| Nutzt vorhandene Infrastruktur | ✅ Z_READ_TABLE + ODBC-Treiber, keine neuen Bausteine |
| Einfach zu automatisieren | ✅ SQL Server Agent Job mit DELETE + INSERT |
| Keine Log-Tabelle auf SAP | ✅ Keine Speicherbelastung |
| Keine Wartung | ✅ Kein Trigger-Monitoring nötig |

## Nachteile und Risiken

### 1. Abhängigkeit vom Änderungsdatum

Nicht jede SAP-Tabelle hat ein zuverlässiges Änderungsdatum. Die häufigsten Felder:

| Feld | Bedeutung | Zuverlässigkeit |
|---|---|---|
| `AEDAT` | Änderungsdatum | ⚠️ Nicht bei allen Tabellen gepflegt |
| `LAEDA` | Letztes Änderungsdatum | ⚠️ Ähnlich wie AEDAT |
| `ERDAT` | Erstelldatum | ❌ Nur INSERTs, keine UPDATEs |
| `CPUDT` | CPU-Datum (Erstellung) | ❌ Nur INSERTs |
| `BUDAT` | Buchungsdatum | ⚠️ Buchungsdatum ≠ Änderungsdatum |

**Problem:** Wenn eine Tabelle kein `AEDAT`/`LAEDA` hat oder dieses nicht bei jeder Änderung aktualisiert wird, funktioniert der Ansatz nicht zuverlässig.

### 2. Nachträgliche Änderungen an älteren Perioden

**Szenario:** Jemand ändert im August eine Buchung aus Juli.

- `AEDAT` = '202608xx' (Datum der Änderung)
- `BUDAT` = '202607xx' (Buchungsdatum, unverändert)

Wenn man nach `BUDAT` filtert: Die Änderung wird verpasst (Juli wurde bereits abgeschlossen und nicht neu geladen).

Wenn man nach `AEDAT` filtert: Die Änderung wird erfasst (August wird geladen, der geänderte Satz hat AEDAT im August).

**Fazit:** Man muss nach `AEDAT`/`LAEDA` filtern, nicht nach `BUDAT`. Aber wenn `AEDAT` nicht zuverlässig gepflegt ist, entsteht eine Lücke.

### 3. Größerer Datenabruf als bei Trigger-CDC

Der Trigger loggt nur die tatsächlich geänderten Sätze. Der Zeitfenster-Ansatz lädt **alle Sätze des Zeitraums** neu — auch die die sich nicht geändert haben.

| Tabelle | Trigger-CDC (tägliche Deltas) | Zeitfenster (aktueller Monat) |
|---|---|---|
| MARA | ~1.000 Sätze | ~5.000 Sätze (alle des Monats) |
| VBAK | ~50.000 Sätze | ~500.000 Sätze (alle des Monats) |
| ACDOCA | ~200.000 Sätze | ~2-5 Mio. Sätze (alle des Monats) |

Bei täglicher Ladung (statt monatlich) reduziert sich das deutlich.

### 4. Tabellen ohne Änderungsdatum

Einige Tabellen haben kein verwendbares Änderungsdatum. Für diese Tabellen funktioniert der Zeitfenster-Ansatz **nicht**. Hier muss auf Trigger-CDC oder manuelle Full-Loads zurückgegriffen werden.

## Welche Tabellen für welchen Ansatz?

| Kriterium | Zeitfenster-Delta | Trigger-CDC |
|---|---|---|
| Hat zuverlässiges AEDAT/LAEDA | ✅ Ideal | ✅ Funktioniert auch |
| Kein Änderungsdatum | ❌ Nicht möglich | ✅ Funktioniert |
| Stammdaten, geringe Änderungsrate | ✅ Ideal | ⚠️ Overkill |
| Bewegungsdaten, hohe Änderungsrate | ✅ Täglicher Load | ✅ Besser (nur echte Deltas) |
| Nachträgliche Änderungen an alten Perioden | ⚠️ Risiko | ✅ Immer erfasst |
| Upgrade-Sicherheit kritisch | ✅ Ideal | ⚠️ Trigger kann wegfliegen |

## Empfohlene Strategie: Hybrid

1. **Tabellen mit zuverlässigem AEDAT** → Zeitfenster-Delta (einfach, wartungsfrei)
2. **Tabellen ohne AEDAT oder mit kritischen Nachtragsänderungen** → Trigger-CDC
3. **Einmaliger Full-Load** → `Z_READ_TABLE` mit Chunking

## Automatisierung

Ein Python- oder C#-Skript auf dem MSSQL-Server könnte konfigurierbar sein:

```json
{
  "tables": [
    {
      "name": "MARA",
      "delta_field": "LAEDA",
      "window": "month",
      "full_load": false
    },
    {
      "name": "VBAK",
      "delta_field": "AEDAT",
      "window": "day",
      "full_load": false
    },
    {
      "name": "ACDOCA",
      "delta_field": "AEDAT",
      "window": "day",
      "full_load": false
    },
    {
      "name": "T001W",
      "delta_field": null,
      "window": null,
      "full_load": true,
      "full_load_schedule": "weekly"
    }
  ]
}
```

Das Skript generiert dann pro Tabelle:
1. `DELETE FROM dbo.<table> WHERE <delta_field> >= '<window_start>'`
2. `SELECT * FROM <table> WHERE <delta_field> >= '<window_start>'` (via ODBC/RFC, mit Chunking)
3. `INSERT INTO dbo.<table> ...`