# Risiken, Trigger-Wiederherstellung und Monitoring

## Risiko 1: Trigger wird bei DDL-Änderungen gelöscht

### Problem

SAP-HANA löscht automatisch alle Trigger auf einer Tabelle, wenn die Tabellenstruktur geändert wird (DDL-Operation). Das passiert bei:

| Aktion | Trigger wird gelöscht? |
|---|---|
| SAP Support Package das die Tabelle ändert | ✅ Ja |
| SAP Upgrade (Release-Wechsel) | ✅ Ja |
| Append-Struktur hinzufügen/ändern | ✅ Ja |
| CI-Include (Customer-Include) hinzufügen/ändern | ✅ Ja |
| Manuelle SE11-Änderung an der Tabelle | ✅ Ja |
| Transport-Import der die Tabelle ändert | ✅ Ja |
| Normale Datenänderung (INSERT/UPDATE/DELETE) | ❌ Nein — Trigger bleibt |

### Ist das eine Modifikation im SAP-Sinne?

**Nein.** Ein Datenbank-Trigger ist keine DDIC-Modifikation:
- Die Tabellenstruktur im ABAP Dictionary (SE11) wird nicht geändert
- Es entsteht kein SPDD/SPAU-Eintrag
- Der Transportauftrag bleibt unberührt
- SAP's Dictionary weiß nichts von dem Trigger

Der Trigger existiert **nur auf Datenbank-Ebene** (HANA), nicht im ABAP Dictionary. Das ist genau der Grund warum Theobald, Fivetran und Qlik diesen Ansatz nutzen können, ohne SAP-Modifikationen zu erzeugen.

### Was passiert im schlimmsten Fall?

```
22:00  Transport wird importiert → Tabelle wird umstrukturiert → Trigger gelöscht
22:00  Log-Tabelle existiert noch, letzte SEQ = 5000
       ↓
23:00  Nacht-Job: 200 neue Aufträge in VBAK eingefügt
00:00  Nacht-Job: 50 Bestellungen geändert
01:00  Nacht-Job: 10 Stornierungen (DELETEs)
       ↓
       Trigger existiert nicht → nichts wird geloggt
       Die 260 Änderungen der Nacht sind verloren
       ↓
08:00  Jemand merkt es → Z_CDC_INIT → Trigger neu angelegt
       Trigger ab jetzt: SEQ 5001, 5002, ...
       ↓
       Die 260 Änderungen fehlen in der Zieldatenbank
```

### Gegenmaßnahme: Nightly Trigger-Check

Ein nächtlicher Job (vor dem eigentlichen Sync) ruft `Z_CDC_INIT` für alle getrackten Tabellen auf. Da `Z_CDC_INIT` idempotent ist:

- **Trigger existiert** → nichts tun, Log läuft weiter
- **Trigger fehlt** → neu anlegen, Lücke signalisieren

```
 nightly_trigger_check()
   for each table in tracked_tables:
     result = Z_CDC_INIT(IV_TABLE = table)
     if result.EV_GAP_DETECTED == 'X':
       # Lücke erkannt — Trigger war weg
       # Letzter Log-Eintrag war vor X Stunden
       # → Full-Load erforderlich
       send_alert(table, result.EV_LAST_LOG_TIME)
       full_load(table)  # Z_READ_TABLE: komplette Tabelle laden
     else:
       # Alles OK — Trigger existiert
       delta_sync(table)  # Z_CDC_READ: nur Deltas laden
```

### Gegenmaßnahme: Post-Import-Hook

SAP erlaubt es, nach Transport-Imports automatisch ABAP-Code auszuführen. Ein After-Import-Exit könnte `Z_CDC_INIT` für alle Tabellen aufrufen die im Transport geändert wurden:

```
Transport wird importiert
  ↓
After-Import-Exit
  ↓
Für jede DDIC-geänderte Tabelle:
  Z_CDC_INIT(IV_TABLE = changed_table)
  ↓
Trigger ist sofort wieder da, keine Lücke
```

Dies ist die sauberste Lösung — Lücken können gar nicht erst entstehen.

### Gegenmaßnahme: Lücken-Erkennung in Z_CDC_INIT

`Z_CDC_INIT` kann eine Lücke erkennen:

1. Trigger existiert nicht → war wahrscheinlich mal da
2. Log-Tabelle existiert noch → hat alte Einträge
3. Letzter Log-Eintrag: SEQ = 5000, Timestamp = 22:00 gestern
4. Aktuelle Zeit: 08:00 heute
5. Lücke von 10 Stunden ohne Log-Einträge → `EV_GAP_DETECTED = 'X'`

Der Client weiß dann: "Ich brauche einen Full-Load, das Delta ist nicht zuverlässig."

## Risiko 2: Log-Tabelle wächst unkontrolliert

### Problem

Wenn der Client-Abruf längere Zeit nicht läuft (z.B. Server-Ausfall, Wartungsarbeiten), sammeln sich Log-Einträge.

### Gegenmaßnahme

- `Z_CDC_INIT` kann ein optionales Limit für die Log-Tabelle setzen (z.B. max 500.000 Einträge wie Theobald)
- Bei Überschreitung: Extraktion schlägt fehl → zwingt zur Investigation
- `Z_CDC_CLEANUP` sollte regelmäßig aufgerufen werden

## Risiko 3: Performance-Auswirkungen auf SAP-Produktivsystem

### Problem

Trigger feuern bei jeder Schreiboperation. Bei hochfrequenten Tabellen (MSEG, ACDOCA) ist der Overhead spürbar.

### Bewertung

- Der Trigger macht nur einen einfach INSERT (Key + Operation) in eine Log-Tabelle — keine Berechnung, keine Logik
- HANA verarbeitet einfache INSERTs extrem schnell (In-Memory, Spalten-orientiert)
- Overhead: 5-15% bei hochfrequenten Tabellen, < 1% bei Stammdaten
- Vergleich: SLT nutzt ebenfalls Trigger — auf einer separaten Instanz, aber gleicher Mechanismus
- Theobald und Qlik nutzen den gleichen Ansatz in Produktivsystemen weltweit

### Empfehlung

- Trigger nur auf Tabellen die wirklich repliziert werden müssen
- Bei extrem hochfrequenten Tabellen: Zeitfenster-Delta als Alternative prüfen
- Log-Tabelle regelmäßig abräumen
- Monitoring der Log-Tabellengröße

## Risiko 4: Keine SAP-Zertifizierung

### Problem

Theobald's Bausteine sind SAP-zertifiziert (`/THEO/CDC`). Unsere `Z_CDC_*`-Bausteine sind nicht zertifiziert.

### Bewertung

- SAP-Zertifizierung ist kein rechtliches Erfordernis — es ist ein Quality-Siegel
- Die Bausteine nutzen Standard-ABAP (Open SQL, dynamisches SQL) und Standard-HANA-Trigger
- SAP Note 3255746 verbietet ODP-RFC für Drittanbieter — unsere Custom-Bausteine sind nicht betroffen
- SAP kann unsere Bausteine nicht blockieren — es ist eigener Code auf dem Applikationsserver

## Risiko 5: Tabellen ohne Primary Key

### Problem

Der Trigger loggt den Primary Key der geänderten Zeile. Wenn eine Tabelle keinen eindeutigen Key hat, kann die Log-Zeile nicht eindeutig zugeordnet werden.

### Gegenmaßnahme

- `Z_CDC_INIT` prüft ob die Tabelle einen Primary Key hat
- Falls nicht: Warnung an den Client, Full-Load wird empfohlen
- Alternativ: alle Felder als Key behandeln (aufwendiger im Trigger)

## Risiko 6: Pool- und Cluster-Tabellen

### Problem

Pool- und Cluster-Tabellen haben keine eigene physische Tabelle in HANA — sie sind in Pool-/Cluster-Tabellen zusammengefasst. Trigger funktionieren hier nicht.

### Gegenmaßnahme

- `Z_CDC_INIT` prüft Tabellentyp und lehnt Pool-/Cluster-Tabellen ab
- Für diese Tabellen: Zeitfenster-Delta oder Full-Load
- SAP Note 2583731 warnt vor Triggern auf BW-Objekten (DSO/ADSO) — diese ebenfalls ausschließen

## Risiko 7: BW-Objekte (DSO, ADSO)

### Problem

SAP BW/4HANA kann trigger-basierte Mechanismen auf DSO/ADSO-Tabellen blockieren.

### Gegenmaßnahme

- `Z_CDC_INIT` prüft ob die Tabelle ein BW-Objekt ist
- Falls ja: Warnung, Trigger-CDC nicht empfohlen
- Für BW-Objekte: ODP-OData (falls verfügbar) oder Full-Load

## Wartungsplan

| Tätigkeit | Häufigkeit | Automatisierbar? |
|---|---|---|
| `Z_CDC_INIT` für alle getrackten Tabellen | Nächtlich (vor Sync) | ✅ Job |
| `Z_CDC_CLEANUP` nach erfolgreichem Sync | Nach jedem Sync | ✅ Im Client-Skript |
| Log-Tabellengröße prüfen | Wöchentlich | ✅ Monitoring |
| Trigger nach SAP-Upgrade neu anlegen | Nach Upgrade | ✅ `Z_CDC_INIT` |
| Trigger nach Transport neu anlegen | Nach Transport | ✅ Post-Import-Hook |
| Full-Load bei erkannter Lücke | Bei Bedarf | ⚠️ Manuell auslösen |
| CDC für nicht mehr benötigte Tabellen entfernen | Bei Bedarf | ✅ `Z_CDC_CLEANUP` mit `IV_REMOVE_ALL = 'X'` |