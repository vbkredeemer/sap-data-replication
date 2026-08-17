# Datenabruf: Script vs. ODBC-Treiber

## Übersicht

Für den Datenabruf von SAP stehen zwei Wege zur Verfügung. Beide nutzen RFC über das SAP NWRFC SDK als Transportprotokoll — der Unterschied liegt in der Client-Seite.

## Weg 1: Direktes Python/C#-Script mit NWRFC SDK

```
Python/C# Client
  ↓ pyrfc (Python) oder ERPConnect (C#) oder sapnwrfc.dll direkt
  ↓ RFC-Aufruf
SAP Applikationsserver
  ↓ Z_CDC_READ / Z_READ_TABLE / Z_EXECUTE_SQL
HANA
```

### Vorteile
- **Vollständige Kontrolle** — Transaction-Handling, Error-Retry, Logging
- **Direkter RFC-Zugriff** — kein ODBC-Layer, weniger Overhead
- **CDC-Logik im Script** — SEQ-Pointer, INSERT/UPDATE/DELETE-Merge in einem Durchlauf
- **Batch-Operationen** — MSSQL Bulk Insert für schnelles Schreiben

### Nachteile
- **NWRFC SDK muss installiert sein** — `sapnwrfc.dll` auf dem Client-Rechner
- **Python: pyrfc** — braucht kompilierte Extension, nicht immer trivial zu installieren
- **C#:** Entweder ERPConnect (kommerziell) oder direkte P/Invoke der NWRFC DLL (aufwendig)
- **Kein Standard-Interface** — jedes Tool muss das Protokoll selbst implementieren

### Beispiel (Python mit pyrfc)

```python
from pyrfc import Connection
import pyodbc

# SAP-Verbindung
sap_conn = Connection(ashost='sap-prod.firma.de', sysnr='10', 
                       client='100', user='RFC_USER', passwd='***')

# MSSQL-Verbindung
sql_conn = pyodbc.connect('Driver={ODBC Driver 17 for SQL Server};'
                          'Server=localhost;Database=SAP_REPL;Trusted_Connection=yes;')

# Letzte SEQ abfragen
cursor = sql_conn.cursor()
cursor.execute("SELECT last_seq FROM CDC_STATE WHERE table_name = 'MARA'")
last_seq = cursor.fetchone()[0]

# Delta abholen (mit Chunking)
has_more = True
while has_more:
    result = sap_conn.call('Z_CDC_READ', 
                           IV_TABLE='MARA', 
                           IV_FROM_SEQ=last_seq,
                           IV_CHUNK_SIZE=10000)
    
    for row in result['ET_DATA']:
        op = row['ROWDATA'][0]  # I, U, or D
        data = row['ROWDATA'][2:]  # pipe-delimited values
        
        if op == 'D':
            cursor.execute("DELETE FROM dbo.MARA WHERE MATNR = ?", data.split('|')[0])
        elif op == 'I':
            fields = data.split('|')
            cursor.execute("INSERT INTO dbo.MARA (MATNR, MTART, ...) VALUES (?, ?, ...)", fields)
        elif op == 'U':
            fields = data.split('|')
            cursor.execute("UPDATE dbo.MARA SET MTART=?, ... WHERE MATNR=?", fields[1:], fields[0])
    
    last_seq = result['EV_NEXT_SEQ']
    has_more = result['EV_HAS_MORE'] == 'X'

# SEQ speichern und Log aufräumen
cursor.execute("UPDATE CDC_STATE SET last_seq = ? WHERE table_name = 'MARA'", last_seq)
sql_conn.commit()
sap_conn.call('Z_CDC_CLEANUP', IV_TABLE='MARA', IV_UP_TO_SEQ=last_seq)

sap_conn.close()
sql_conn.close()
```

## Weg 2: Über den ODBC-Treiber

```
Python/C# Client / SQL Server Linked Server / SSIS
  ↓ ODBC API
sapodbcabap.dll (unser ODBC-Treiber)
  ↓ RFC (NWRFC SDK, im Treiber integriert)
SAP Applikationsserver
  ↓ Z_READ_TABLE (einfache Queries) / Z_EXECUTE_SQL (komplexe Queries)
HANA
```

### Vorteile
- **Standard-Interface** — jeder ODBC-fähige Client kann zugreifen
- **SQL Server Linked Server** — `SELECT * FROM OPENQUERY(SAP_PROD, 'SELECT * FROM MARA WHERE AEDAT >= ''20260801''')`
- **SSIS-Packages** — nativer SQL Server Integration Services Support
- **Excel / Power BI** — direkter Zugriff ohne Skript
- **Kein pyrfc nötig** — ODBC-Treiber kapselt die RFC-Kommunikation
- **Chunking automatisch** — `Z_READ_TABLE` wird vom Treiber automatisch in Blöcken aufgerufen

### Nachteile
- **Kein CDC-Support** — der ODBC-Treiber kann `Z_CDC_READ` nicht direkt aufrufen (er ist auf SQL-Queries beschränkt)
- **Kein INSERT/UPDATE/DELETE auf SAP** — reine Lese-Schnittstelle
- **ODBC-Overhead** — minimal, aber vorhanden

### Beispiel (SQL Server Linked Server)

```sql
-- Linked Server einrichten
EXEC sp_addlinkedserver 
    @server = 'SAP_PROD',
    @srvproduct = 'SAP ODBC Driver',
    @provider = 'MSDASQL',
    @datasrc = 'SAP-Produktion';

-- Zeitfenster-Delta: aktuellen Monat laden
DELETE FROM dbo.ACDOCA WHERE AEDAT >= '20260801';

INSERT INTO dbo.ACDOCA
SELECT * FROM OPENQUERY(SAP_PROD, 
    'SELECT * FROM ACDOCA WHERE AEDAT >= ''20260801''');

-- Oder mit spezifischen Feldern
INSERT INTO dbo.MARA (MATNR, MTART, MEINS, LAEDA)
SELECT MATNR, MTART, MEINS, LAEDA
FROM OPENQUERY(SAP_PROD,
    'SELECT MATNR, MTART, MEINS, LAEDA FROM MARA WHERE LAEDA >= ''20260801''');
```

## Gegenüberstellung

| Kriterium | Script (pyrfc) | ODBC-Treiber |
|---|---|---|
| CDC (Z_CDC_READ) | ✅ Direkt aufrufbar | ❌ Nicht über ODBC |
| Full-Load (Z_READ_TABLE) | ✅ Direkt aufrufbar | ✅ Über SQL |
| Komplexe Queries (Z_EXECUTE_SQL) | ✅ Direkt aufrufbar | ✅ Über SQL |
| Chunking | ⚠️ Selbst implementieren | ✅ Automatisch |
| SQL Server Linked Server | ❌ Nicht möglich | ✅ Nativ |
| SSIS-Integration | ❌ Nur über Script-Task | ✅ Native ODBC Source |
| Excel / Power BI | ❌ Nicht möglich | ✅ Nativ |
| Merge-Logik (INSERT/UPDATE/DELETE) | ✅ Vollständige Kontrolle | ⚠️ Nur über T-SQL |
| Installation | pyrfc + NWRFC SDK | ODBC-Treiber + NWRFC SDK |
| Aufwand | Höher | Geringer |

## Empfohlene Kombination

| Anwendungsfall | Empfohlener Weg |
|---|---|
| Trigger-CDC mit Z_CDC_READ | **Script (pyrfc)** — braucht CDC-Bausteine |
| Zeitfenster-Delta | **ODBC-Treiber + Linked Server** — einfach, Standard-SQL |
| Interaktive Abfragen (Excel, Power BI) | **ODBC-Treiber** |
| QlikSense Full-Load | **ODBC-Treiber** |
| Komplexe Analysen (Joins, GROUP BY) | **ODBC-Treiber** (Z_EXECUTE_SQL) |
| Automatisierte nächtliche Replikation | **Script** oder **Linked Server + SQL Agent Job** |

Für den Zeitfenster-Delta-Ansatz reicht der ODBC-Treiber mit Linked Server völlig aus — man braucht kein separates Script. Für Trigger-CDC wird ein direktes Script benötigt, da der ODBC-Treiber die CDC-Bausteine nicht direkt aufrufen kann.