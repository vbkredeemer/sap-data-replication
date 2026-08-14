# Code Review Findings: sap-data-replication

**Date:** 2026-08-14  
**Reviewer:** Automated Agent Review  
**Scope:** All source files (ABAP, Python, config, spec, docs)

---

## CRITICAL Findings

### C-01: Z_CDC_INIT — Gap detection variable uninitialized when trigger exists and early RETURN  
**File:** `abap/Z_CDC_INIT.abap`, lines 200-204  
**Severity:** CRITICAL  

When the trigger exists (`lv_trigger_count > 0`), the function sets `ev_trigger_exists = 'X'` and immediately `RETURN`s at line 203. However, `ev_last_log_seq` and `ev_last_log_time` may have been set at lines 156-157 (if the log table had entries). The early return means the gap detection code at lines 213-215 is never reached. This is actually correct behavior per the design (trigger exists → no gap). However, `lv_age_hours` is only assigned inside the `IF lv_last_seq > 0` block, and if the trigger exists path returns early, `ev_gap_detected` remains its initial value (cleared at line 51). This is functionally correct, but the control flow is fragile — any future code inserted between the trigger check and the RETURN could inadvertently change behavior.

**Risk:** Low actual runtime risk, but high maintenance risk. The `RETURN` at line 203 exits before trigger creation logic, which is correct, but the variable `lv_age_hours` is declared at line 49 and only set conditionally at line 166. If the gap detection block at line 213 were ever reached with `lv_last_seq > 0` but the timestamp calculation had failed (catch block at line 167-169), `lv_age_hours` would be 0, which is safe.

**Recommended fix:** No fix needed for current logic, but add a comment making the early RETURN semantics explicit.

---

### C-02: Z_CDC_READ — SQL injection via KEYVALUES in WHERE clause  
**File:** `abap/Z_CDC_READ.abap`, lines 267-270  
**Severity:** CRITICAL  

The `lv_key_value` is read from the log table's `KEYVALUES` column and directly concatenated into a dynamic WHERE clause without any sanitization:
```abap
CONCATENATE ls_ddic_key-fieldname ' = ''' lv_key_value '''' INTO lv_where.
```
If a key value contains a single quote (e.g., a CHAR field with value `O'Brien`), the WHERE clause will be syntactically broken, causing a runtime exception. Worse, if the key value contains `'; DROP TABLE ...`, this is a SQL injection vector.

While SAP key fields are typically numeric or structured, CHAR-type key fields can contain arbitrary characters including single quotes. The `CATCH cx_root` at line 344 catches the error and treats the row as a delete, which means the actual INSERT/UPDATE change is silently lost — data loss.

**Recommended fix:** Escape single quotes in `lv_key_value` by doubling them, or use parameterized ADBC queries.

---

### C-03: Z_CDC_READ — `ev_next_seq` set incorrectly when rows are found but operation was 'D' (row treated as delete fallback)  
**File:** `abap/Z_CDC_READ.abap`, lines 339-348  
**Severity:** CRITICAL  

When an INSERT or UPDATE operation's original row is not found (sy-subrc ≠ 0 at line 277) or an exception occurs (line 344), the row data is rewritten as a DELETE (`lv_rowdata = 'D'` + keyvalues). However, `lv_count` is still incremented (line 232) and `lv_max_seq` is still set to `lv_seq` (line 231). This means `ev_next_seq` will advance past this log entry, and the client will believe it has processed the change. But the client receives a 'D' operation for what was actually an INSERT or UPDATE — this means the client will DELETE a row that should have been INSERTED or UPDATED, causing **data corruption**.

**Recommended fix:** When the original row is not found for an INSERT operation, either skip the row (don't emit it) or emit it with the original operation code but with empty data. Converting I/U to D silently corrupts data.

---

### C-04: Z_EXPORT_TABLE — Keyset paging is broken for composite primary keys  
**File:** `abap/Z_EXPORT_TABLE.abap`, lines 253-265  
**Severity:** CRITICAL  

The keyset paging builds a WHERE clause using `>` on ALL primary key fields:
```abap
IF lv_key_idx = 1.
  CONCATENATE ls_pk_field-fieldname ' > ''' lv_last_key '''' INTO lv_pk_where.
ELSE.
  CONCATENATE lv_pk_where ' AND ' ls_pk_field-fieldname ' > ''' lv_last_key '''' ...
```
This uses `lv_last_key` (the same value) for ALL key fields. It should use the last value of EACH key field separately. Additionally, proper keyset paging for composite keys requires `(key1 > val1) OR (key1 = val1 AND key2 > val2)` — not `key1 > val1 AND key2 > val2`, which would skip valid rows.

Furthermore, `lv_last_key` is only set from the FIRST primary key field (line 488: `READ TABLE lt_pk_fields INTO ls_pk_field INDEX 1`), so for composite keys, only the first key's value is tracked. The second and subsequent key fields get the wrong value.

**Result:** For tables with composite primary keys, rows will be silently skipped during export, causing **data loss**.

**Recommended fix:** Store the last value of each PK field separately and build a proper keyset continuation clause using row-value comparison or tuple syntax.

---

### C-05: Z_EXPORT_TABLE — `lv_char_val` used but never declared  
**File:** `abap/Z_EXPORT_TABLE.abap`, lines 381, 387, 389, etc.  
**Severity:** CRITICAL  

The variable `lv_char_val` is used extensively in the type conversion CASE block (lines 381-452) but is never declared in the DATA section. In ABAP, undeclared variables cause a syntax error. This code will not compile.

**Recommended fix:** Add `lv_char_val TYPE string` to the DATA declarations at the top of the function.

---

### C-06: Z_EXPORT_TABLE — `ALPHA = IN` format option may corrupt date values  
**File:** `abap/Z_EXPORT_TABLE.abap`, line 387  
**Severity:** CRITICAL  

```abap
DATA(lv_date_str) = |{ <ff_field> ALPHA = IN }|.
```
The `ALPHA = IN` format option applies alpha conversion (leading zero padding) to the value. For DATE fields (which are already `YYYYMMDD` format), this is incorrect — `ALPHA = IN` is meant for CHAR fields with alpha conversion exits. While the result might be correct for 8-character dates (they're already numeric), this is semantically wrong and could produce unexpected results if the date field has unusual formatting or if the ABAP runtime applies the conversion exit differently.

**Recommended fix:** Remove `ALPHA = IN` and use plain string conversion: `DATA(lv_date_str) = |{ <ff_field> }|`.

---

### C-07: Z_CDC_INIT — Trigger name hash truncation produces names that may collide and differ from Z_CDC_READ/CDC_CLEANUP  
**File:** `abap/Z_CDC_INIT.abap`, lines 76-98; `abap/Z_CDC_READ.abap`, lines 59-75; `abap/Z_CDC_CLEANUP.abap`, lines 55-75  
**Severity:** CRITICAL  

The hash-based name generation uses `CALCULATE_HASH_FOR_CHAR` and takes the first 6 characters of the hash string. However:

1. The hash function returns a full hash string (typically MD5/SHA), and taking only the first 6 hex characters creates a significant collision risk. With only 6 hex chars (16^6 = ~16M possibilities), birthday paradox means collisions are likely after ~4000 tables.

2. More importantly, the log table name uses `_CDC_LOG` but the trigger name uses `_CDC_TRG` and the sequence uses `_CDC_SEQ`. For the hashed (long table) case:
   - Log table: `Z_` + 6 chars + `_CDC_LOG` = 3+6+8 = 17 chars ✓
   - Trigger INS: `Z_` + 6 chars + `_CDC_TRG_INS` = 3+6+12 = 21 chars ✓ (HANA limit 32)
   - But the trigger name in Z_CDC_INIT is `Z_` + 6 chars + `_CDC_TRG`, then `_INS`/`_UPD`/`_DEL` is appended. So the full trigger name is 3+6+12 = 21 chars — OK.

3. The hashing logic is **duplicated** in all three modules. If one module has a different hash implementation or parameter (e.g., different case for `data` parameter), the names won't match and cleanup will fail silently.

4. The `CALCULATE_HASH_FOR_CHAR` function may not be available in all SAP systems (it's part of `SECH` function group). If it fails, the fallback uses `iv_table(6)` — the first 6 characters of the table name. This fallback could easily collide for tables with the same 6-character prefix.

**Recommended fix:** Centralize name generation in a shared utility, use a longer hash portion, and validate availability of `CALCULATE_HASH_FOR_CHAR`.

---

### C-08: Z_CDC_READ — ADBC `get_char()` used for all column types, including INTEGER  
**File:** `abap/Z_CDC_READ.abap`, lines 225-229  
**Severity:** CRITICAL  

The code reads all four log table columns (SEQ, OPERATION, KEYVALUES, TIMESTMP) using `lo_result->get_char()`:
```abap
DATA(lv_seq_str) = lo_result->get_char( ).
lv_seq = lv_seq_str.
lv_operation = lo_result->get_char( ).
lv_keyvalues = lo_result->get_char( ).
lv_timestmp = lo_result->get_char( ).
```
`get_char()` returns the value as a character string. For the SEQ column (INTEGER), this should work via implicit conversion, but the ADBC `cl_sql_result_cursor` class typically has typed getter methods (`get_int()`, `get_string()`, etc.). Using `get_char()` for an INTEGER column may return unexpected formatting or cause type conversion errors depending on the HANA ADBC implementation.

More critically, `lv_seq` is declared as `TYPE i` (line 203) and assigned from `lv_seq_str` which is a string. The implicit conversion should work, but if the ADBC returns the integer with leading/trailing spaces or formatting, the conversion may fail silently (producing 0).

**Recommended fix:** Use `get_int()` for the SEQ column, or explicitly convert with error handling.

---

### C-09: sap_replicate.py — CDC apply_delta uses INSERT for 'I' operations instead of UPSERT/MERGE  
**File:** `client/sap_replicate.py`, lines 517-528  
**Severity:** CRITICAL  

For INSERT operations from CDC, the code does a plain `INSERT INTO dbo.{table} (...) VALUES (...)`. If the row already exists in the target table (e.g., from a previous full load or a replay), this will fail with a primary key violation. The `executemany` call at line 527 does not handle individual row failures — if one row fails, the entire batch fails.

For UPDATE operations (lines 531-545), the code does individual `UPDATE` statements, which is correct but slow. However, if the row doesn't exist yet (e.g., the CDC trigger fired before the initial load completed), the UPDATE affects 0 rows and the data is silently lost.

The correct approach for CDC is to use MERGE/UPSERT semantics for both I and U operations, or at minimum use `IF EXISTS ... UPDATE ELSE INSERT` logic.

**Recommended fix:** Use MERGE statement or `INSERT ... ON CONFLICT` equivalent for both I and U operations, or use `IF NOT EXISTS` guards.

---

### C-10: sap_replicate.py — SQL injection via table name in f-strings  
**File:** `client/sap_replicate.py`, multiple lines (e.g., 488, 526, 542, 551, 696, 707, 789, 820, 1016, 1021, 1026, 1034)  
**Severity:** CRITICAL  

Table names and column names are interpolated directly into SQL strings using f-strings without any sanitization:
```python
cursor = self.sql.execute(f"SELECT TOP 0 * FROM dbo.{table}")
insert_sql = f"INSERT INTO dbo.{table} ({col_list}) VALUES ({placeholders})"
self.sql.execute(f"TRUNCATE TABLE dbo.{table}")
self.sql.execute(f"DELETE FROM dbo.{table} WHERE [{delta_field}] >= ?", (mssql_date_from,))
```
While table names come from the config file (not user input in the traditional sense), a malicious or mistyped config could execute arbitrary SQL. The `delta_field` is also unsanitized in the DELETE at line 707 (though it's parameterized at line 708 in the timeframe path, the flatfile path at line 1021 uses f-string interpolation).

More importantly, at line 1021:
```python
where = f"WHERE [{date_field}] >= '{mssql_date_from}'"
```
The `date_field` is directly from config with no validation, and the date value is interpolated (not parameterized) — SQL injection.

**Recommended fix:** Validate table names against a whitelist or regex (`^[A-Za-z_][A-Za-z0-9_]+$`), and use parameterized queries for all values.

---

### C-11: sap_replicate.py — BULK INSERT path uses f-string interpolation for file path  
**File:** `client/sap_replicate.py`, lines 1033-1044  
**Severity:** CRITICAL  

```python
bulk_sql = f"""
    BULK INSERT dbo.{target_table}
    FROM '{csv_abs}'
    WITH (...)
"""
```
The `csv_abs` path is derived from a temp directory + the remote filename returned by SAP. If the SAP function returns a crafted filename (e.g., containing a single quote), the BULK INSERT SQL will break or be exploitable. The `target_table` is also unsanitized.

**Recommended fix:** Validate the filename returned by SAP and use `QUOTENAME()` or escaping for both table name and file path.

---

## HIGH Findings

### H-01: Z_CDC_READ — `has_more` check queries with `lv_max_seq` which may be 0 when no rows found  
**File:** `abap/Z_CDC_READ.abap`, lines 372-385  
**Severity:** HIGH  

When `lv_count = 0` (no log entries found), `lv_max_seq` retains its initial value of 0 (line 207). The `has_more` check at line 373 queries `WHERE SEQ > 0`, which will count ALL entries in the log table — not just entries after the client's pointer. This is wrong: it should query `WHERE SEQ > iv_from_seq` (the client's starting point), not `WHERE SEQ > lv_max_seq`.

If the log table has entries but none after `iv_from_seq` (all already processed), `lv_count = 0` and `lv_max_seq = 0`. The has_more query then returns `COUNT(*) WHERE SEQ > 0` — which is the total count of ALL entries, not the remaining count. This makes `ev_has_more = 'X'` even though there's nothing new, causing the client to loop indefinitely (calling Z_CDC_READ repeatedly with the same `from_seq`, getting 0 rows each time, but `has_more = 'X'`).

Wait — actually, when `lv_count = 0`, the code at line 364-368 sets `ev_next_seq = iv_from_seq`, and the client checks `has_more`. If `has_more = 'X'` erroneously, the client will call again with the same `from_seq`, get 0 rows again, and loop forever.

**Actually:** The `lv_max_seq` is initialized to 0 at declaration (line 207) and only updated in the WHILE loop. When no rows are found, the has_more query uses `lv_max_seq = 0`, which counts all rows with SEQ > 0 — i.e., all rows. If the log table has ANY entries (even old ones already processed), `has_more` will be 'X', and the client will loop forever.

**Recommended fix:** When `lv_count = 0`, set `ev_has_more = ' '` unconditionally, or query `WHERE SEQ > iv_from_seq` instead of `WHERE SEQ > lv_max_seq`.

---

### H-02: Z_CDC_INIT — Trigger uses `CURRENT_TIMESTAMP` but log table TIMESTMP column is TIMESTAMP type  
**File:** `abap/Z_CDC_INIT.abap`, lines 133, 262  
**Severity:** HIGH  

The trigger inserts `CURRENT_TIMESTAMP` into the `TIMESTMP` column. In HANA, `CURRENT_TIMESTAMP` returns a `TIMESTAMP` value. The column is declared as `TIMESTAMP NOT NULL` (line 133). This should work. However, the gap detection code at line 154 reads `MAX(timestmp)` into `lv_last_time` which is `TYPE timestampl` (long timestamp). The `SELECT MAX(timestmp)` returns a TIMESTAMP, and assigning to TIMESTAMPL should work via implicit conversion. Then `cl_abap_tstmp=>subtractsecs` expects TIMESTAMPL operands.

The risk is that the implicit conversion from TIMESTAMP to TIMESTAMPL may lose precision or fail on some HANA versions.

**Recommended fix:** Explicitly cast or use `TIMESTAMPL` consistently, or use `CURRENT_TIMESTAMP` with explicit casting in the trigger.

---

### H-03: Z_CDC_INIT — Sequence creation error is silently ignored  
**File:** `abap/Z_CDC_INIT.abap`, lines 121-126  
**Severity:** HIGH  

```abap
TRY.
    DATA(lo_sql) = NEW cl_sql_statement( ).
    lo_sql->execute_ddl( lv_sql ).
  CATCH cx_root INTO DATA(lo_cx).
    * Sequence might already exist — ignore
ENDTRY.
```
If the sequence creation fails for a reason other than "already exists" (e.g., permission denied, invalid name), the error is silently ignored. The subsequent trigger creation will then fail when it tries to use `lv_seq_name.NEXTVAL`, but the error message will say "Cannot create INSERT trigger" — misleading the admin about the root cause.

**Recommended fix:** Check the exception text — if it's not "already exists", propagate the error.

---

### H-04: Z_CDC_INIT — `lv_short` may not be declared when hash fails on first call  
**File:** `abap/Z_CDC_INIT.abap`, lines 86-90  
**Severity:** HIGH  

```abap
IF sy-subrc = 0 AND strlen( lv_hash_str ) >= 6.
  DATA(lv_short) = lv_hash_str(6).
ELSE.
  lv_short = iv_table(6).
ENDIF.
```
The inline declaration `DATA(lv_short)` is inside the `IF` branch. In ABAP 7.40+, inline declarations are visible in the entire processing block (not just the IF), so `lv_short` in the ELSE branch should be valid. However, this depends on ABAP version behavior. In older ABAP (7.00-7.31), inline declarations may not be available at all, and the code uses `DATA(...)` syntax extensively.

The code claims 7.00+ compatibility (uses `cl_abap_tstmp=>subtractsecs` instead of `utclong`), but inline declarations like `DATA(lv_hash_str)` at line 83, `DATA(lv_short)` at line 87, `DATA(lo_sql)` at line 122 are **ABAP 7.40+ features** — they do NOT exist in 7.00-7.31.

**Recommended fix:** If 7.00 compatibility is required, replace all inline `DATA(...)` declarations with traditional `DATA:` declarations at the top. If 7.40+ is the actual minimum, update the documentation.

---

### H-05: sap_replicate.py — CDC sync_table uses `last_seq` (starting point) instead of `next_seq` for next iteration  
**File:** `client/sap_replicate.py`, lines 597-610  
**Severity:** HIGH  

```python
last_seq = self.state.get_last_seq(table)
...
while True:
    rows, next_seq, has_more = self.read_delta(table, last_seq, chunk_size)
    ...
    max_seq = next_seq - 1
    last_seq = next_seq
    if not has_more:
        break
```
The first iteration uses `last_seq` (from state) as `IV_FROM_SEQ`. `Z_CDC_READ` returns entries with `SEQ > IV_FROM_SEQ`. So if state has `last_seq = 5000`, it reads SEQ > 5000. The returned `next_seq` is `max_seq_in_result + 1`. Then `last_seq` is set to `next_seq`.

This is correct — `IV_FROM_SEQ` is exclusive (SEQ > IV_FROM_SEQ), and `EV_NEXT_SEQ` is the next SEQ to read from. The state stores `max_seq` (line 613), which is `next_seq - 1`.

However, the cleanup at line 617 uses `max_seq`:
```python
self.cleanup(table, max_seq)
```
And `Z_CDC_CLEANUP` deletes `WHERE SEQ <= IV_UP_TO_SEQ`. This is correct — it deletes up to and including the last processed SEQ.

But there's a subtle issue: if `has_more = 'X'` but `rows` is empty (which shouldn't happen but could due to H-01), the loop continues with the same `last_seq`, and `max_seq` is set to `next_seq - 1` which equals `last_seq - 1` (since `next_seq = iv_from_seq` when no rows). This could cause the state to go backwards.

**Recommended fix:** Add a guard: if `rows` is empty and `has_more` is true, break the loop to prevent infinite loops.

---

### H-06: sap_replicate.py — TimeframeReplicator uses IV_ROWSKIPS (OFFSET-based paging) which is inefficient and unreliable  
**File:** `client/sap_replicate.py`, lines 728-729, 803-804  
**Severity:** HIGH  

```python
result = self.sap.call('Z_READ_TABLE',
                       IV_TABLE=table,
                       IV_WHERE=where_clause,
                       IV_FIELDS='*',
                       IV_ORDERBY='',
                       IV_ROWSKIPS=skip,
                       IV_ROWCOUNT=chunk_size)
```
The `IV_ROWSKIPS` parameter implements OFFSET-based paging (`SKIP n ROWS`). For large tables, OFFSET becomes increasingly slow as `skip` grows — the database must scan and discard all skipped rows. With 50M rows and 10K chunks, the last chunk requires skipping 49.99M rows.

Additionally, if data changes between chunk reads (new rows inserted), OFFSET-based paging may skip rows or return duplicates.

The ABAP `Z_EXPORT_TABLE` module correctly uses keyset paging, but `Z_READ_TABLE` (used by Timeframe and Full-Load modes) uses OFFSET.

**Recommended fix:** Implement keyset paging in Z_READ_TABLE as well, or use the flatfile mode for large tables (which already has keyset paging).

---

### H-07: sap_replicate.py — executemany with `fast_executemany = True` may fail silently on type mismatches  
**File:** `client/sap_replicate.py`, lines 134-137  
**Severity:** HIGH  

```python
def executemany(self, sql: str, rows: list):
    cursor = self.conn.cursor()
    cursor.fast_executemany = True
    cursor.executemany(sql, rows)
    return cursor.rowcount
```
`fast_executemany = True` in pyodbc can cause silent data corruption or unexpected failures when the data types don't match exactly. Since all CDC data arrives as pipe-delimited strings and is split into lists of strings, pyodbc must implicitly convert strings to the target column types (INT, DATE, DECIMAL, etc.). With `fast_executemany = True`, some type conversions are skipped or handled differently, which can lead to:
- NULL values being inserted as empty strings
- Date format mismatches ('2026-08-13' may not convert correctly)
- Decimal values losing precision

Additionally, `cursor.rowcount` after `executemany` with `fast_executemany = True` may return -1 (unknown) rather than the actual count.

**Recommended fix:** Validate types before executemany, or set `fast_executemany = False` for CDC delta (small batches) and only use it for bulk loads.

---

### H-08: sap_replicate.py — DELETE in CDC apply_delta is not batched  
**File:** `client/sap_replicate.py`, lines 548-552  
**Severity:** HIGH  

```python
if deletes:
    for key_vals in deletes:
        where = ' AND '.join(f"{k} = ?" for k in key_fields)
        self.sql.execute(f"DELETE FROM dbo.{table} WHERE {where}", key_vals)
    self.sql.commit()
```
Each delete is a separate SQL statement with a separate round-trip to the database. For a large CDC delta with many deletes, this is very slow. The INSERTs are batched with `executemany`, but deletes are not.

**Recommended fix:** Use a temporary table or `WHERE ... IN (...)` batch, or build a single DELETE with OR conditions.

---

### H-09: gui_client.py — SyncWorker `finished_all` signal emitted even on early return paths  
**File:** `client/gui_client.py`, lines 177-215  
**Severity:** HIGH  

For `init_only`, `remove_cdc`, and `sync_schema` actions, the worker returns early from the `run()` method without emitting `finished_all`. The `finished_all` signal is only emitted at line 248, which is at the end of `run()` — but the early returns at lines 185, 194, and 215 use `return` which skips line 248.

Wait, actually — the `return` statements at lines 185, 194, 215 are inside the `try` block, but the `finally` block at line 245-248 will still execute. So `root_logger.removeHandler(handler)` runs, and then `self.finished_all.emit(success, fail)` at line 248 runs. But `success` and `fail` may be 0 (their initial values) for `init_only` and `remove_cdc` actions, which never increment them.

This means the GUI's `_on_finished` handler will show "0 erfolgreich, 0 fehlgeschlagen" for init_only and remove_cdc — misleading.

**Recommended fix:** Track success/fail counts for all action types, or emit different signals for non-sync actions.

---

### H-10: gui_client.py — ScheduleTab scheduler only runs one job per check cycle  
**File:** `client/gui_client.py`, lines 1165-1197  
**Severity:** HIGH  

```python
for s in schedules:
    ...
    if (now - last_run) >= interval_sec:
        ...
        self.run_tab._start_worker([table_cfg], action)
        self.last_run_time[table] = now
        break  # Only one job at a time
```
The `break` means only one scheduled job runs per 60-second check cycle. If multiple jobs are due at the same time, only the first one runs — the others are deferred to the next check cycle. With hourly intervals, this means jobs could be delayed by up to 60 seconds per job.

More critically, `last_run_time` is only set for the job that actually ran. Jobs that were due but skipped (because another job was running) will be checked again on the next cycle — but since `last_run_time` wasn't updated, they'll be "due" again and will run. This is actually correct behavior (they should run ASAP), but the `break` combined with the `run_tab.worker.isRunning()` check at line 1156 means if a job is still running 60 seconds later, all other due jobs are skipped again.

**Recommended fix:** Remove the `break` and let multiple workers run concurrently (if the GUI supports it), or queue due jobs. At minimum, update `last_run_time` for all due jobs to prevent repeated logging.

---

### H-11: gui_client.py — _check_schedules doesn't pass window override to sync  
**File:** `client/gui_client.py`, lines 1189-1194  
**Severity:** HIGH  

```python
if action == 'sync':
    self.run_tab._start_worker([table_cfg], "sync")
```
The scheduled job's `window` field is read (line 1172) but never used. The table config's own `window` is used instead. If a user configures a job with `window = 'day'` but the table config has `window = 'month'`, the job will use 'month' — ignoring the scheduler's window setting.

**Recommended fix:** Clone the table config and override the window field before passing to the worker.

---

### H-12: gui_client.py — GuiLogHandler may cause thread-safety issues  
**File:** `client/gui_client.py`, lines 114-124, 149-153  
**Severity:** HIGH  

The `GuiLogHandler` emits a Qt signal (`log_signal`) from the worker thread. Qt signals across threads are generally safe if using `QueuedConnection` (which is the default for cross-thread connections). However, the handler is added to the **root logger** at line 153, which means ALL log messages from ALL loggers (including other libraries) will trigger the signal. This could cause:
- Excessive GUI updates if other libraries log frequently
- The handler is never removed if the worker thread crashes before reaching the `finally` block (line 246)
- Multiple workers could add multiple handlers if run in sequence

**Recommended fix:** Add the handler only to the `sap_replicate` logger, not the root logger. Ensure cleanup in all code paths.

---

### H-13: gui_client.py — _export_windows_task doesn't validate time format  
**File:** `client/gui_client.py`, lines 1209, 1283  
**Severity:** HIGH  

The `start_time` from the text field is passed directly to `schtasks /ST` without validation. If the user enters "2am" instead of "02:00", the schtasks command will fail with a cryptic error. The code also doesn't validate the HH:MM format.

**Recommended fix:** Validate with a regex or `datetime.strptime(start_time, '%H:%M')`.

---

## MEDIUM Findings

### M-01: Z_CDC_INIT — Log table KEYVALUES column is NVARCHAR(1000), may truncate long composite keys  
**File:** `abap/Z_CDC_INIT.abap`, line 132  
**Severity:** MEDIUM  

```abap
KEYVALUES NVARCHAR(1000),
```
If a table has many key fields or key values are long, the concatenated `key1|key2|...|keyN` string may exceed 1000 characters. The trigger will silently truncate the value, and Z_CDC_READ will not be able to find the original row.

**Recommended fix:** Use `NVARCHAR(5000)` or `NCLOB`, or validate key length during init.

---

### M-02: Z_CDC_READ — No ORDER BY in the "has_more" count query could be slow  
**File:** `abap/Z_CDC_READ.abap`, lines 373-375  
**Severity:** MEDIUM  

```abap
CONCATENATE 'SELECT COUNT(*) FROM ' lv_log_table
            ' WHERE SEQ > ' lv_max_seq
            INTO lv_sql.
```
This count query runs after every chunk. For large log tables, COUNT(*) with a WHERE clause can be slow. Since the LIMIT in the main query already restricts to `IV_CHUNK_SIZE`, a simpler approach would be to check if the last fetched chunk was exactly `IV_CHUNK_SIZE` rows — if so, there are likely more.

**Recommended fix:** Set `ev_has_more = 'X'` when `lv_count = iv_chunk_size` (the chunk was full), avoiding the extra query.

---

### M-03: Z_CDC_CLEANUP — `execute_update` return value assigned but may not be row count  
**File:** `abap/Z_CDC_CLEANUP.abap`, lines 98-101  
**Severity:** MEDIUM  

```abap
DATA(lo_result) = lo_sql_stmt->execute_update( lv_sql ).
ev_deleted = lo_result.
```
`cl_sql_statement=>execute_update()` returns an integer (number of affected rows) for DML statements. However, the return type is `INT` in some ADBC implementations and `INT4` in others. The assignment `ev_deleted = lo_result` should work, but if `execute_update` returns -1 (unknown), `ev_deleted` will be -1, which the client interprets as a warning (line 567 of sap_replicate.py checks `result.get('EV_ERROR')`, not `EV_DELETED`).

**Recommended fix:** Check for negative return values and handle appropriately.

---

### M-04: Z_EXPORT_TABLE — File size calculation is approximate and may be wrong  
**File:** `abap/Z_EXPORT_TABLE.abap`, lines 190, 472  
**Severity:** MEDIUM  

```abap
lv_size = strlen( lv_header ) + 2.  " +2 for line terminator
...
lv_size = lv_size + strlen( lv_row ) + 2.
```
The `+2` assumes a 2-byte line terminator (CRLF on Windows). On Linux/HANA, the line terminator is typically `\n` (1 byte). The size will be overestimated by 1 byte per line. Not critical for functionality, but `ev_file_size` will be inaccurate.

**Recommended fix:** Use `cl_abap_char_utilities=>newline` or detect the actual line separator.

---

### M-05: Z_EXPORT_TABLE — `OPEN DATASET` with `TEXT MODE ENCODING UTF-8` may add BOM  
**File:** `abap/Z_EXPORT_TABLE.abap`, line 179  
**Severity:** MEDIUM  

```abap
OPEN DATASET ev_file_name FOR OUTPUT IN TEXT MODE ENCODING UTF-8.
```
On some SAP systems, opening a file in TEXT MODE with UTF-8 encoding may prepend a BOM (Byte Order Mark, 3 bytes: EF BB BF). The BULK INSERT in MSSQL with `FIRSTROW = 2` expects the first row to be the header. If a BOM is present, the first byte sequence is attached to the first column name, causing a column mismatch.

**Recommended fix:** Use `ENCODING DEFAULT` or `ENCODING NON-UNICODE`, or explicitly handle BOM in the Python client.

---

### M-06: Z_EXPORT_TABLE — No check for sufficient disk space before writing  
**File:** `abap/Z_EXPORT_TABLE.abap`, lines 178-183  
**Severity:** MEDIUM  

The function opens a file and starts writing without checking available disk space. For large tables (ACDOCA can be 50M+ rows, several GB), the write could fill the disk, causing a partial file and a confusing error.

**Recommended fix:** Check available space or set a size limit, or at minimum catch the write error and clean up the partial file.

---

### M-07: sap_replicate.py — StateManager MERGE statement may fail on older SQL Server versions  
**File:** `client/sap_replicate.py`, lines 418-424  
**Severity:** MEDIUM  

The MERGE statement with `USING (SELECT ? AS ...)` syntax requires SQL Server 2008+. The parameterized values inside the USING clause may not work with all pyodbc/SQL Server combinations — some drivers don't support parameters in the USING clause of MERGE.

**Recommended fix:** Use `IF EXISTS ... UPDATE ELSE INSERT` pattern instead of MERGE for broader compatibility.

---

### M-08: sap_replicate.py — FullLoadReplicator logs progress every 100K rows but may never hit exact 100K  
**File:** `client/sap_replicate.py`, lines 837-838  
**Severity:** MEDIUM  

```python
if total_rows % 100000 == 0:
    log.info(f"  {table}: loaded {total_rows} rows...")
```
If `chunk_size` is 10,000, `total_rows` will be multiples of 10,000. The `% 100000` check will be true every 10 chunks (at 100K, 200K, etc.). But if the last chunk has fewer rows and the total is, say, 105,000, the modulo check won't trigger at 100,000 because the loop already exited. This is a minor logging issue, not a bug.

**Recommended fix:** Log at every chunk or every N chunks instead of every N rows.

---

### M-09: sap_replicate.py — FlatfileReplicator._bulk_insert_csv SQL injection in replace_window path  
**File:** `client/sap_replicate.py`, lines 1021-1026  
**Severity:** MEDIUM (elevated from CRITICAL C-10 since values come from config, not user input)

```python
where = f"WHERE [{date_field}] >= '{mssql_date_from}'"
if date_to:
    mssql_date_to = f"{date_to[:4]}-{date_to[4:6]}-{date_to[6:8]}"
    where += f" AND [{date_field}] <= '{mssql_date_to}'"
self.sql.execute(f"DELETE FROM dbo.{target_table} {where}")
```
The `date_field` and `target_table` are from config (not parameterized). The date values are interpolated (not parameterized). While the dates come from `_get_window_range` (internally generated), the `date_field` is user-config and unsanitized.

**Recommended fix:** Parameterize the date values and validate the field name.

---

### M-10: gui_client.py — SettingsTab._save doesn't include `flatfile` section in DEFAULT_CONFIG  
**File:** `client/gui_client.py`, lines 42-61, 419-422  
**Severity:** MEDIUM  

The `DEFAULT_CONFIG` at lines 42-61 does not include a `flatfile` key. When a new config is created (no existing config.json), the `flatfile` section won't be in the defaults. The `_load_values` method at line 396 uses `self.config.get('flatfile', {})` which handles this gracefully, but if the config is saved and reloaded, the `flatfile` section is added by `_save`. However, the `load()` method at line 85 only merges top-level keys from DEFAULT_CONFIG — so `flatfile` won't be in the loaded config if the file doesn't have it. This is handled by the `.get('flatfile', {})` calls, so it's not a crash, but it's inconsistent.

**Recommended fix:** Add `"flatfile": {"transfer_method": "scp", "smb_share": ""}` to DEFAULT_CONFIG.

---

### M-11: gui_client.py — TablesTab doesn't save `file_path` or `max_rows` fields  
**File:** `client/gui_client.py`, lines 511-630  
**Severity:** MEDIUM  

The `_read_row` method (line 596) reads 10 columns but doesn't include `file_path` or `max_rows`. The `config.example.json` has `file_path` and `fields` for flatfile tables, but the GUI only saves `fields`. The `file_path` defaults to `/usr/sap/tmp/` in the Python client (line 1223), so this works, but users can't configure per-table file paths via the GUI.

Additionally, the GUI doesn't have a column for `date_field` — only `delta_field`. The config.example.json uses `delta_field` for this purpose, which is correct, but the `date_field` key (line 1221) is never set from the GUI.

**Recommended fix:** Add `file_path` and `max_rows` columns or use the SettingsTab's flatfile config for file_path.

---

### M-12: gui_client.py — ScheduleTab.closeEvent never called  
**File:** `client/gui_client.py`, lines 1327-1328  
**Severity:** MEDIUM  

The `ScheduleTab` class defines a `closeEvent` method (line 1327), but `closeEvent` is only called on top-level widgets (QMainWindow, QDialog). `ScheduleTab` is a QWidget inside a QTabWidget — its `closeEvent` will never be called. The scheduler is properly stopped in `MainWindow.closeEvent` (line 1857), so this is just dead code.

**Recommended fix:** Remove the `closeEvent` from ScheduleTab, or connect it to the tab's destroyed signal.

---

### M-13: gui_client.py — _set_progress may crash if progress_table item is None  
**File:** `client/gui_client.py`, lines 760-761  
**Severity:** MEDIUM  

```python
if self.progress_table.item(i, 0).text() == table_name:
    self.progress_table.item(i, 1).setText(status)
```
If column 0 has a QTableWidgetItem but column 1 or 2 was somehow not set (e.g., due to a race condition or programmatic modification), `.item(i, 1)` could return `None`, and `.setText()` will raise `AttributeError`.

**Recommended fix:** Check for `None` before calling `.setText()`.

---

### M-14: sap_replicate.py — run_table doesn't pass window_override to flatfile mode  
**File:** `client/sap_replicate.py`, lines 1253-1262  
**Severity:** MEDIUM  

The `run_table` function accepts `window_override` (line 1211) and uses it for CDC and timeframe modes. For flatfile mode (line 1243), it doesn't pass `window` — the FlatfileReplicator.sync_table receives `window=window` from the local variable `window` (line 1218: `window = window_override or table_cfg.get('window', 'month')`), which is correct. But the GUI's SyncWorker.run() calls `run_table(t, sap, sql, state, self.config)` without mode/window overrides (line 225), so this is consistent.

Actually, looking more carefully: `run_table` at line 1257 passes `window=window` to the flatfile replicator. The `window` variable is set at line 1218. This is correct.

No issue — false alarm after closer inspection.

---

## LOW Findings

### L-01: Z_CDC_INIT — Comment says "Z_ + hash(6) + _CDC_TRG_INS = 3+6+13 = 22 chars" but actual is 3+6+12 = 21  
**File:** `abap/Z_CDC_INIT.abap`, line 78  
**Severity:** LOW  

The comment says `_CDC_TRG_INS` is 13 chars, but it's actually 12 characters: `_CDC_TRG_INS` = 12. The total would be 21, not 22. This is a minor comment error.

**Recommended fix:** Fix the comment.

---

### L-02: Z_CDC_INIT — Unreachable code at lines 322-324  
**File:** `abap/Z_CDC_INIT.abap`, lines 321-324  
**Severity:** LOW  

```abap
ev_trigger_exists = ' '.
IF ev_gap_detected = 'X'.
  * Trigger was re-created after a gap — client should do full load
ENDIF.
```
Setting `ev_trigger_exists = ' '` is redundant (it was already cleared at line 51 and only set to 'X' in the early-return path at line 201). The IF block has an empty body (just a comment). This is dead code.

**Recommended fix:** Remove or add the intended logic.

---

### L-03: Z_CDC_READ — `lt_dynamic` and `lo_data_ref` declared but never used  
**File:** `abap/Z_CDC_READ.abap`, lines 213-215  
**Severity:** LOW  

```abap
DATA: lo_table_descr TYPE REF TO cl_abap_tabledescr,
      lo_data_ref    TYPE REF TO data,
      lt_dynamic     TYPE REF TO data,
      ls_dynamic     TYPE REF TO data.
```
`lo_table_descr` is used at line 218, `ls_dynamic` is used at lines 219 and 275. But `lo_data_ref` and `lt_dynamic` are declared and never used.

**Recommended fix:** Remove unused declarations.

---

### L-04: Z_CDC_READ — `lv_field_value` declared but only used inside the INSERT/UPDATE branch  
**File:** `abap/Z_CDC_READ.abap`, line 48  
**Severity:** LOW  

`lv_field_value` is declared at the top of the function but only used inside the `ELSE` branch (INSERT/UPDATE processing, lines 283-334). For DELETE rows, it's unused. Not a bug, just scope wider than necessary.

**Recommended fix:** Move declaration closer to usage.

---

### L-05: Z_CDC_CLEANUP — Dead code after `RETURN` at line 140  
**File:** `abap/Z_CDC_CLEANUP.abap`, lines 143-149  
**Severity:** LOW  

The `RETURN` at line 140 (end of `IV_REMOVE_ALL = 'X'` block) exits the function. The code at lines 146-149 is only reachable if `iv_remove_all = ' '` AND `iv_up_to_seq = 0` (both conditions are not met by the earlier IF blocks). But the first IF at line 93 checks `iv_remove_all = ' ' AND iv_up_to_seq > 0` (returns), and the second IF at line 113 checks `iv_remove_all = 'X'` (returns). So the code at line 146 is reachable when `iv_remove_all = ' '` AND `iv_up_to_seq = 0`.

This is actually correct — it handles the "nothing to do" case. But the `RETURN` at line 140 could be removed since the code would fall through to the check at line 146 anyway. Minor structural issue.

**Recommended fix:** No action needed; logic is correct.

---

### L-06: Z_EXPORT_TABLE — `lv_field_types`, `ls_field_type`, `lv_type_kind` declared but never used  
**File:** `abap/Z_EXPORT_TABLE.abap`, lines 346-348  
**Severity:** LOW  

```abap
DATA: lt_field_types TYPE TABLE OF abap_componentdescr,
      ls_field_type  TYPE abap_componentdescr,
      lv_type_kind   TYPE abap_typekind.
```
`lt_field_types` and `ls_field_type` are declared but never used. `lv_type_kind` is used at line 375 (assigned via `READ TABLE`). The type map is built using `lt_type_map` (line 351) instead.

**Recommended fix:** Remove unused declarations.

---

### L-07: Z_EXPORT_TABLE — Date filter uses `>=` and `<=` but SAP date fields are CHAR(8), not DATE  
**File:** `abap/Z_EXPORT_TABLE.abap`, lines 89-97  
**Severity:** LOW  

```abap
CONCATENATE iv_date_field ' >= ''' iv_date_from ''''
            ' AND ' iv_date_field ' <= ''' iv_date_to ''''
            INTO lv_where.
```
SAP date fields (DATS) are stored as CHAR(8) in the format YYYYMMDD. The `>=` and `<=` comparison works correctly for string comparison because the YYYYMMDD format is lexicographically ordered. This is not a bug, but the comparison relies on the date format being zero-padded.

**Recommended fix:** No action needed; works correctly for standard SAP dates.

---

### L-08: Z_DELETE_FILE — `DELETE DATASET` with `sy-subrc` check inside TRY block is redundant  
**File:** `abap/Z_DELETE_FILE.abap`, lines 29-36  
**Severity:** LOW  

```abap
TRY.
    DELETE DATASET iv_file_path.
    IF sy-subrc <> 0.
      ev_error = 'File not found or cannot delete: ' && iv_file_path.
    ENDIF
  CATCH cx_root INTO DATA(lo_cx).
    ev_error = 'Error deleting file: ' && lo_cx->get_text( ).
ENDTRY.
```
`DELETE DATASET` sets `sy-subrc` but does not raise exceptions (in most ABAP versions). The TRY/CATCH is for unexpected runtime errors. Both paths can set `ev_error`, which is fine. Minor redundancy but not a bug.

**Recommended fix:** No action needed.

---

### L-09: sap_replicate.py — `load_config` opens file without encoding specification  
**File:** `client/sap_replicate.py`, lines 1204-1206  
**Severity:** LOW  

```python
def load_config(path: str) -> dict:
    with open(path, 'r') as f:
        return json.load(f)
```
No `encoding` parameter. On Windows, the default encoding may be CP1252, which can cause issues if the config file contains non-ASCII characters (e.g., in table descriptions or German comments).

**Recommended fix:** Use `encoding='utf-8'`.

---

### L-10: sap_replicate.py — Logging file handler created at import time, before config is loaded  
**File:** `client/sap_replicate.py`, lines 52-65  
**Severity:** LOW  

The log file is created at module import time using `datetime.now()`. If the process runs past midnight, the log continues writing to the previous day's file. Not a bug, but log rotation by day won't happen mid-run.

**Recommended fix:** Use `logging.handlers.TimedRotatingFileHandler` or accept the current behavior.

---

### L-11: sap_replicate.py — `shutil` imported but only used in FlatfileReplicator  
**File:** `client/sap_replicate.py`, line 29  
**Severity:** LOW  

`shutil` is a top-level import but only used inside `FlatfileReplicator._download_smb` and `_download_local`. Could be a lazy import inside those methods.

**Recommended fix:** Move import to method level or leave as-is (negligible overhead).

---

### L-12: gui_client.py — `QIcon` imported but never used  
**File:** `client/gui_client.py`, line 35  
**Severity:** LOW  

```python
from PySide6.QtGui import QFont, QColor, QAction, QIcon
```
`QIcon` is imported but never used in the file.

**Recommended fix:** Remove unused import.

---

### L-13: gui_client.py — `QSize` imported from QtCore but also available from QtWidgets  
**File:** `client/gui_client.py`, lines 27, 31  
**Severity:** LOW  

`QSize` is imported from `QtCore` (line 27) and used in `QTableWidgetItem` contexts. This is correct but could also come from `QtGui`. Minor style issue.

**Recommended fix:** No action needed.

---

### L-14: config.example.json — `smb_share` value has too many backslash escapes  
**File:** `client/config.example.json`, line 21  
**Severity:** LOW  

```json
"smb_share": "\\\\sap-server\\sap\\tmp"
```
In JSON, `\\` represents a single backslash. So this value is `\\sap-server\sap\tmp` — with TWO leading backslashes (UNC path). This is correct for a UNC path. But the Python code at line 867 stores it as `r'\\sap-server\sap\tmp'` (raw string). The config value will be the string `\\sap-server\sap\tmp` which is correct for UNC.

No bug, just confirming the JSON escaping is correct.

---

### L-15: README.md — Claims "drei ABAP-Funktionsbausteine" (three ABAP function modules) but there are five  
**File:** `README.md`, line 7  
**Severity:** LOW  

```markdown
**Code ist implementiert** — drei ABAP-Funktionsbausteine + Python-Client-Skript.
```
The project has five ABAP function modules: Z_CDC_INIT, Z_CDC_READ, Z_CDC_CLEANUP, Z_EXPORT_TABLE, Z_DELETE_FILE. The "drei" (three) likely refers to the three CDC modules, but Z_EXPORT_TABLE and Z_DELETE_FILE are also ABAP modules in this project.

**Recommended fix:** Update to "fünf ABAP-Funktionsbausteine" or clarify.

---

### L-16: README.md — Claims GUI has "3 Tabs" but it has 4  
**File:** `README.md`, line 52  
**Severity:** LOW  

```markdown
| `gui_client.py` | **GUI-Client (PySide6/Qt)** — professioneller Desktop-Client mit 3 Tabs |
```
The GUI has 4 tabs: Verbindungen, Tabellen, Ausführen, Zeitplan.

**Recommended fix:** Update to "4 Tabs".

---

### L-17: INSTALL.md — Window descriptions are incorrect  
**File:** `client/INSTALL.md`, lines 173-180  
**Severity:** LOW  

The Window table says:
- `day` = "Aktueller Tag (YYYYMMDD)"
- `week` = "Aktuelle Woche (Montag-basiert)"
- `month` = "Aktueller Monat (YYYYMM01)"
- `year` = "Aktuelles Jahr (YYYY0101)"

But the actual implementation loads current + previous period (e.g., day = yesterday + today). The documentation doesn't mention the overlap behavior.

**Recommended fix:** Update to reflect the overlap behavior documented in `docs/timeframe-delta.md`.

---

### L-18: docs/table-cdc.md — Trigger syntax has typo "REFERING" instead of "REFERENCING"  
**File:** `docs/table-cdc.md`, line 137  
**Severity:** LOW  

```sql
CREATE TRIGGER Z_MARA_CDC_DEL
AFTER DELETE ON MARA
REFERING OLD ROW AS old_row
```
Should be `REFERENCING OLD ROW AS old_row`. The ABAP source code (Z_CDC_INIT.abap line 301) correctly uses `REFERENCING`.

**Recommended fix:** Fix typo in documentation.

---

### L-19: docs/table-cdc.md — `IV_GAP_THRESHOLD_HOURS` parameter not documented  
**File:** `docs/table-cdc.md`, lines 52-58; `abap/Z_CDC_INIT.abap`, line 29  
**Severity:** LOW  

The `IV_GAP_THRESHOLD_HOURS` parameter (default 24) exists in the ABAP interface but is not mentioned in the documentation. The Python client never passes it (uses the default).

**Recommended fix:** Document the parameter.

---

### L-20: sap_replicate.py — `Z_READ_TABLE` mentioned but not included in this project  
**File:** `client/sap_replicate.py`, lines 16, 723, 798  
**Severity:** LOW  

The docstring at line 16 says "SAP-Funktionsbausteine: Z_CDC_INIT, Z_CDC_READ, Z_CDC_CLEANUP, Z_READ_TABLE" and lists "DDIC-Typen: ZSQL_FIELD, ZSQL_ROW (aus dem ODBC-Projekt)". `Z_READ_TABLE` and `Z_EXECUTE_SQL` are from the separate `sap-odbc-abap` project. This dependency is documented but could be clearer.

**Recommended fix:** Add a note about the external dependency on the ODBC project for Z_READ_TABLE and Z_EXECUTE_SQL.

---

### L-21: gui_client.py — Help text references `requirements.txt` but INSTALL.md references it too — consistent  
**File:** Multiple  
**Severity:** LOW (informational)

The `requirements.txt` file exists at `client/requirements.txt` and includes `pyrfc`, `pyodbc`, and `PySide6`. The `pyinstaller` dependency is commented out. This is consistent between the README and INSTALL.md.

No action needed.

---

### L-22: sap_replicate.py — `_get_window_range` duplicated in TimeframeReplicator and FlatfileReplicator  
**File:** `client/sap_replicate.py`, lines 634-682 and 869-902  
**Severity:** LOW  

The `_get_window_range` method is copy-pasted between `TimeframeReplicator` and `FlatfileReplicator`. Any bug fix or new window type must be applied in both places. The implementations are identical.

**Recommended fix:** Extract to a shared utility function or base class.

---

### L-23: gui_client.py — _show_help_dialog uses `dialog.exec()` which may be deprecated in PySide6  
**File:** `client/gui_client.py`, line 1527  
**Severity:** LOW  

```python
dialog.exec()
```
In PySide6, `QDialog.exec()` is deprecated in favor of `QDialog.exec()`. Wait, actually `exec()` is fine in PySide6 (it was `exec_()` that was deprecated in PyQt6, and PySide6 uses `exec()`). No issue.

**Recommended fix:** No action needed.

---

## Summary Statistics

| Severity | Count |
|---|---|
| CRITICAL | 11 |
| HIGH | 13 |
| MEDIUM | 14 |
| LOW | 23 |
| **Total** | **61** |

## Top Priority Fixes

1. **C-05**: `lv_char_val` undeclared — code won't compile
2. **C-04**: Keyset paging broken for composite keys — data loss
3. **C-03**: I/U operations converted to D when row not found — data corruption
4. **C-02**: SQL injection via KEYVALUES — data loss + security
5. **C-09**: CDC uses INSERT not UPSERT — crashes on duplicate keys
6. **C-10/C-11**: SQL injection via table names in f-strings — security
7. **H-01**: `has_more` bug causes infinite loop — client hangs
8. **H-04**: Inline DATA declarations not 7.00-compatible — compilation failure on older systems
9. **H-06**: OFFSET-based paging in Z_READ_TABLE — performance degradation
10. **H-09**: Worker `finished_all` counts wrong for non-sync actions — misleading UI