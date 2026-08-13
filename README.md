# SAP Data Replication

Konzepte und Architektur für die Replikation von SAP-Tabellen in Fremdsysteme (z.B. Microsoft SQL Server) — ohne kommerzielle Produkte, ohne ODP-RFC, ohne SLT-Lizenz.

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