# Vergleich: Unser ODBC-Treiber vs. Qlik SAP Connector vs. Theobald Software

## Übersicht

Diese Gegenüberstellung vergleicht drei Ansätze für den Zugriff auf SAP-Daten über den Applikationsserver (RFC), mit besonderem Fokus auf die Auswirkungen von SAP Note 3255746 (ODP-RFC-Blockade seit Juni 2026).

## Beteiligte Produkte

| Produkt | Anbieter | Lizenzmodell |
|---|---|---|
| **sap-odbc-abap** | Eigenentwicklung (Open Source, GPL-3.0) | Kostenlos |
| **Qlik SAP Connector** (ODP / Table / Replicate) | Qlik | Kommerziell (~Teil der Qlik-Lizenz) |
| **Theobald Xtract Universal / Xtract IS** | Theobald Software | Kommerziell (separate Lizenz) |

---

## Technologie-Vergleich

### Kommunikationsprotokoll

| | Unser Treiber | Qlik ODP | Qlik Table | Theobald |
|---|---|---|---|---|
| Protokoll | RFC (NWRFC SDK) | RFC (ODP-RFC) | RFC (RFC_READ_TABLE / Custom) | RFC (Custom) |
| Über Applikationsserver | ✅ | ✅ | ✅ | ✅ |
| Lizenzrechtlich sicher | ✅ | ❌ Siehe unten | ✅ | ✅ |

### SAP Note 3255746 — ODP-RFC-Blockade

**Hintergrund:** SAP hat im Juni 2026 einen Security-Patch ausgerollt, der ODP-RFC-Aufrufe von nicht-authorisierten Tools technisch blockiert. SAP Note 3255746 (Version 11, April 2026) stellt klar: Die RFC-Module des ODP Data Replication API sind ausschließlich für SAP-interne Anwendungen bestimmt.

Eine temporäre Opt-Out-Möglichkeit besteht bis Ende 2026 — danach ist ODP-RFC für Drittanbieter endgültig nicht mehr nutzbar.

| | Unser Treiber | Qlik ODP | Qlik Table | Theobald ODP | Theobald Table CDC |
|---|---|---|---|---|---|
| Nutzt ODP-RFC? | ❌ Nein | ✅ Ja | ❌ Nein | ✅ Ja (einige Komponenten) | ❌ Nein |
| Von SAP Note 3255746 betroffen? | ❌ **Nein** | ❌ **Ja — blockiert** | ❌ Nein | ❌ **Ja — blockiert** | ❌ Nein |
| Zukunftssicher? | ✅ **Ja** | ❌ Nein | ✅ Ja | ❌ Nein | ✅ Ja |

**Erklärung:**

- **Qlik's ODP-Connector** (Qlik Replicate, Qlik Cloud SAP ODP) nutzt SAP's ODP-RFC-Schnittstelle. Diese wird ab Juni 2026 blockiert. Qlik entwickelt Ausweichlösungen (ODP-OData), die aber langsamer sind.
- **Qlik's Table Connector** nutzt RFC_READ_TABLE oder Custom-Bausteine — nicht von der Blockade betroffen.
- **Theobald** nutzt ODP-RFC für einige Komponenten (ODP Extraction Type), bietet aber Migration zu Table CDC, DeltaQ, Table with CDS View an — alle nicht-ODP-RFC.
- **Unser Treiber** nutzt ausschließlich Custom-Funktionsbausteine (`Z_EXECUTE_SQL`, `Z_READ_TABLE`) — nie ODP-RFC. Nicht betroffen, nicht blockierbar.

**Wichtige Klarstellung aus Theobald's Dokumentation:**
> "SAP Note 3255746 verbietet nicht RFC als Kommunikationsprotokoll. RFC bleibt voll nutzbar — für Tabellen/CDS-View-Extraktion, DeltaQ, BAPIs oder Custom-Funktionsbausteine. Nur die Nutzung des ODP Data Replication API via RFC ist eingeschränkt. Wenn Sie nicht ODP-RFC nutzen, sind Sie nicht betroffen."

### Funktionsumfang

| Funktion | Unser Treiber | Qlik ODP | Qlik Table | Theobald |
|---|---|---|---|---|
| **SQL-Abfragen (JOIN, GROUP BY)** | ✅ Serverseitig (HANA via ADBC) | ❌ Nein | ❌ Nein | ❌ Nein |
| **Full Table Read** | ✅ Mit Chunking (Z_READ_TABLE) | ✅ | ✅ | ✅ |
| **Max. Zeilen** | Unbegrenzt (Chunking) | Unbegrenzt | Unbegrenzt | Unbegrenzt |
| **Zeilenbreite** | 10.000 Zeichen | Variabel | 512 Zeichen (RFC_READ_TABLE) | Variabel (Custom Baustein) |
| **Delta-Handling (automatisch)** | ❌ Nur manuell (WHERE) | ✅ ODP-Queue | ❌ | ✅ Table CDC (Trigger) |
| **DELETE-Erkennung** | ❌ (nur über Zeitfenster-Replace) | ✅ (ODP) | ❌ | ✅ (Trigger-CDC) |
| **CDC (Trigger-basiert)** | Geplant (separates Projekt) | ✅ (Replicate) | ❌ | ✅ (Table CDC) |
| **Tabellen-Metadaten** | ✅ (SQLTables, SQLColumns) | ✅ | ✅ | ✅ |
| **ODBC-Schnittstelle** | ✅ | ❌ (Qlik-intern) | ❌ (Qlik-intern) | ❌ (Xtract-intern) |
| **JDBC-Schnittstelle** | ✅ (Schwesterprojekt) | ❌ | ❌ | ❌ |
| **Excel / Power BI direkt** | ✅ | ✅ (Qlik-intern) | ✅ (Qlik-intern) | ⚠️ (über Umwege) |
| **SQL Server Linked Server** | ✅ | ❌ | ❌ | ❌ |
| **ABAP-Release-Kompatibilität** | 7.00+ (keine Inline-Deklarationen) | Variabel | Variabel | Variabel |

### Architektur

```
Unser Treiber:
  Client (Excel/Power BI/QlikSense/SQL Server)
    ↓ ODBC API
    sapodbcabap.dll
    ↓ SQL-Parser: einfache Query? komplexe Query?
    ├── Z_READ_TABLE (einfach, mit Chunking)     → Open SQL → HANA
    └── Z_EXECUTE_SQL (komplex, ADBC)            → Native SQL → HANA
    ↓ RFC (NWRFC SDK)
    SAP Applikationsserver

Qlik ODP:
  QlikSense / Qlik Replicate
    ↓ Qlik-internes Protokoll
    ↓ ODP-RFC (SAP-Standard)     ← WIRD AB JUNI 2026 BLOCKIERT
    SAP Applikationsserver
    ↓ ODP-Framework / Delta-Queue
    HANA

Qlik Table:
  QlikSense / Qlik Replicate
    ↓ Qlik-internes Protokoll
    ↓ RFC_READ_TABLE / Custom RFC
    SAP Applikationsserver
    ↓ Open SQL
    HANA

Theobald:
  Xtract Universal / Xtract IS
    ↓ Theobald-internes Protokoll
    ├── /THEO/READ_TABLE (Table)                 → Open SQL → HANA
    ├── /THEO/CDC (Table CDC, Trigger-basiert)   → Log-Tabelle → HANA
    ├── DeltaQ (Extractors, SAPI)                → SAP Extractors → HANA
    └── ODP (wird migriert zu obigen)            → ODP-RFC → HANA  ← BLOCKIERT
    ↓ RFC (NWRFC SDK)
    SAP Applikationsserver
```

### Performance

| | Unser Treiber | Qlik ODP | Qlik Table | Theobald |
|---|---|---|---|---|
| Weg | RFC → Custom Baustein → HANA | RFC → ODP → HANA | RFC → RFC_READ_TABLE → HANA | RFC → Custom Baustein → HANA |
| Flaschenhals | RFC-Transfer | RFC-Transfer | RFC-Transfer + 512-Byte-Limit | RFC-Transfer |
| Komplexe Queries | ✅ HANA-optimiert (serverseitig) | ❌ Nicht möglich | ❌ Nicht möglich | ❌ Nicht möglich |
| Massendaten (Mio. Zeilen) | ✅ Chunking | ✅ | ⚠️ (512-Byte-Limit) | ✅ |
| Relative Performance | Mittel | Mittel | Mittel (schlechter durch 512-Byte) | Mittel |

Alle RFC-basierten Lösungen haben ähnliche Performance — der Flaschenhals ist RFC, nicht die Client-Software. SLT (Datenbank-Trigger-basiert, direkt auf HANA) ist deutlich schneller, kostet aber eine extra SAP-Lizenz.

### Delta-Handling

| | Unser Treiber | Qlik ODP | Theobald Table CDC |
|---|---|---|---|
| Delta-Mechanismus | Manuell (WHERE AEDAT >= ...) | ODP-Queue (automatisch) | Trigger + Log-Tabelle (automatisch) |
| DELETE-Erkennung | ⚠️ Nur über Zeitfenster-Replace | ✅ Automatisch | ✅ Automatisch |
| Echtzeit-Delta | ❌ Nur Batch | ✅ Nahezu Echtzeit | ✅ Nahezu Echtzeit |
| Upgrade-Risiko | ❌ Keines | ❌ Blockiert ab Juni 2026 | ⚠️ Trigger kann wegfliegen |
| Konfigurationsaufwand | Minimal | Hoch (ODP-Provider einrichten) | Mittel (Trigger + Log-Tabelle) |
| Wartung | Keine | ODP-Monitoring | Trigger-Check nach Upgrades |

### Kosten

| | Unser Treiber | Qlik | Theobald |
|---|---|---|---|
| Software-Lizenz | Kostenlos (GPL-3.0) | Qlik-Lizenz (Teil der Plattform) | Separate Lizenz (~mehrere Tausend €/Jahr) |
| SAP-Zusatzlizenz | Keine | Keine (aber ODP-RFC wird blockiert) | Keine |
| NWRFC SDK | Selbst herunterladen | Im Produkt enthalten | Im Produkt enthalten |
| Implementierung | Selbst (ABAP-Bausteine + DLL) | Qlik-Consulting | Theobald-Consulting |
| Wartung | Selbst | Qlik-Support | Theobald-Support |

### SAP-Zertifizierung

| | Unser Treiber | Qlik | Theobald |
|---|---|---|---|
| SAP-zertifiziert | ❌ Nein | ✅ Ja | ✅ Ja |
| Relevanz | Quality-Siegel, kein rechtliches Erfordernis | Wichtig für Enterprise-Kunden | Wichtig für Enterprise-Kunden |
| Auswirkung | Keine — Custom-Bausteine sind erlaubt | — | — |

---

## Zusammenfassung

### Unser Treiber

**Stärken:**
- Kostenlos, Open Source
- Komplexe SQL-Abfragen (Joins, GROUP BY) serverseitig auf HANA — kein anderer RFC-basierter Treiber kann das
- Nicht von SAP Note 3255746 betroffen (Custom-Bausteine, nicht ODP-RFC)
- ODBC-Standard — funktioniert mit Excel, Power BI, SQL Server Linked Server, QlikSense, DBeaver
- Dual-Mode: Automatische Auswahl zwischen Chunking (Z_READ_TABLE) und ADBC (Z_EXECUTE_SQL)
- Schwesterprojekt: JDBC-Treiber für DBeaver und Java-Anwendungen

**Schwächen:**
- Kein automatisches Delta-Handling (nur manuell über WHERE-Klausel)
- Kein CDC (geplant als separates Projekt)
- Keine SAP-Zertifizierung
- Kein kommerzieller Support

### Qlik SAP Connector

**Stärken:**
- SAP-zertifiziert
- ODP mit automatischem Delta-Handling (solange noch funktionsfähig)
- Integration in QlikSense / Qlik Replicate
- Kommerzieller Support

**Schwächen:**
- ODP-RFC wird ab Juni 2026 blockiert — Haupt-Connector betroffen
- Keine komplexen SQL-Abfragen (keine serverseitigen Joins)
- Kostenpflichtig
- Nur innerhalb von Qlik-Produkten nutzbar (kein ODBC, kein Standard-Interface)

### Theobald Software

**Stärken:**
- SAP-zertifiziert
- Table CDC mit Trigger-basiertem Delta-Handling (nicht von ODP-Blockade betroffen)
- Multiple Komponenten (Table, CDC, DeltaQ, BW Cube, CDS View)
- Kommerzieller Support
- Lange Markterfahrung (seit 2004)

**Schwächen:**
- Kostenpflichtig
- ODP-Komponente von Blockade betroffen (Migration erforderlich)
- Keine komplexen SQL-Abfragen (keine serverseitigen Joins)
- Nur innerhalb von Theobald-Produkten nutzbar (kein ODBC, kein Standard-Interface)
- ETL-Tool, kein ODBC-Treiber

---

## Fazit

Mit SAP Note 3255746 und der technischen Blockade von ODP-RFC ab Juni 2026 befindet sich der Markt für SAP-Datenextraktion in einem Umbruch. Qlik und andere ODP-RFC-basierte Anbieter müssen ihre Connector umbauen. Theobald bietet Migration-Pfade an. 

Unser Ansatz — Custom-Funktionsbausteine über RFC — ist von dieser Blockade **nicht betroffen**. Wir haben von Anfang an auf eigene Bausteine gesetzt, nicht auf SAP's ODP-RFC. Das erweist sich jetzt als richtige Entscheidung.

Was uns fehlt (automatisches Delta-Handling, CDC) ist als separates Projekt [`sap-data-replication`](https://github.com/vbkredeemer/sap-data-replication) konzipiert — mit zwei Ansätzen (Trigger-CDC und Zeitfenster-Delta) die beide ohne ODP-RFC funktionieren.