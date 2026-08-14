*---------------------------------------------------------------------*
* Funktionsbaustein: Z_CDC_INIT
* Zweck: CDC für eine Tabelle initialisieren
*        - Legt Log-Tabelle an (falls nicht vorhanden)
*        - Legt DB-Trigger an (falls nicht vorhanden)
*        - Idempotent: kann mehrfach aufgerufen werden
*        - Erkennt Lücken (Trigger war weg, Log-Einträge fehlen)
*---------------------------------------------------------------------*
* IMPORTING:
*   IV_TABLE       TYPE TABNAME    - SAP-Quelltabelle (z.B. 'MARA')
*   IV_KEYFIELDS   TYPE STRING     - Komma-separierte Keys (z.B. 'MATNR')
*   IV_GAP_THRESHOLD_HOURS TYPE I  - Lückenschwelle in Stunden (Default 24)
*
* EXPORTING:
*   EV_LOG_TABLE   TYPE TABNAME    - Name der Log-Tabelle
*   EV_TRIGGER_EXISTS TYPE CHAR1   - 'X' = Trigger existierte bereits
*   EV_GAP_DETECTED   TYPE CHAR1   - 'X' = Lücke erkannt (Trigger war weg)
*   EV_LAST_LOG_SEQ   TYPE I       - Letzte SEQ in Log-Tabelle
*   EV_LAST_LOG_TIME  TYPE TIMESTAMPL - Zeitstempel des letzten Log-Eintrags
*   EV_ERROR       TYPE STRING     - Fehlermeldung
*---------------------------------------------------------------------*

FUNCTION Z_CDC_INIT.
*"----------------------------------------------------------------------
*"*"Lokale Schnittstelle:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_KEYFIELDS) TYPE  STRING
*"     VALUE(IV_GAP_THRESHOLD_HOURS) TYPE  I DEFAULT 24
*"  EXPORTING
*"     VALUE(EV_LOG_TABLE) TYPE  TABNAME
*"     VALUE(EV_TRIGGER_EXISTS) TYPE  CHAR1
*"     VALUE(EV_GAP_DETECTED) TYPE  CHAR1
*"     VALUE(EV_LAST_LOG_SEQ) TYPE  I
*"     VALUE(EV_LAST_LOG_TIME) TYPE  TIMESTAMPL
*"     VALUE(EV_ERROR) TYPE  STRING
*"----------------------------------------------------------------------

  DATA: lv_log_table TYPE tabname,
        lv_trigger_name TYPE string,
        lv_seq_name TYPE string,
        lv_sql TYPE string,
        lv_exists TYPE i,
        lv_last_seq TYPE i,
        lv_last_time TYPE timestampl,
        lv_age_hours TYPE p DECIMALS 2.

  CLEAR: ev_error, ev_log_table, ev_trigger_exists, ev_gap_detected,
         ev_last_log_seq, ev_last_log_time.

  * Validate table name — only alphanumeric and underscore allowed
  IF iv_table IS INITIAL OR iv_table CN 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_' <> 0.
    ev_error = 'Invalid table name (only A-Z, 0-9, underscore allowed): ' && iv_table.
    RETURN.
  ENDIF.

  *---------------------------------------------------------------------*
  * Validate inputs
  *---------------------------------------------------------------------*
  IF iv_keyfields IS INITIAL.
    ev_error = 'IV_KEYFIELDS is empty — need at least one key field'.
    RETURN.
  ENDIF.

  *---------------------------------------------------------------------*
  * Build names
  * HANA trigger names max 32 chars — hash if too long
  *---------------------------------------------------------------------*
  DATA: lv_tab_len TYPE i,
        lv_hash    TYPE i.

  lv_tab_len = strlen( iv_table ).

  IF lv_tab_len > 18.
    * Table name too long — use hash to keep trigger name < 32 chars
    * Z_ + hash(6) + _CDC_TRG_INS = 3+6+13 = 22 chars (safe)
    CALL FUNCTION 'CALCULATE_HASH_FOR_CHAR'
      EXPORTING
        data = iv_table
      IMPORTING
        hashstring = DATA(lv_hash_str)
      EXCEPTIONS
        OTHERS = 1.
    IF sy-subrc = 0 AND strlen( lv_hash_str ) >= 6.
      DATA(lv_short) = lv_hash_str(6).
    ELSE.
      lv_short = iv_table(6).
    ENDIF.
    CONCATENATE 'Z_' lv_short '_CDC_LOG' INTO lv_log_table.
    CONCATENATE 'Z_' lv_short '_CDC_TRG' INTO lv_trigger_name.
    CONCATENATE 'Z_' lv_short '_CDC_SEQ' INTO lv_seq_name.
  ELSE.
    CONCATENATE 'Z_' iv_table '_CDC_LOG' INTO lv_log_table.
    CONCATENATE 'Z_' iv_table '_CDC_TRG' INTO lv_trigger_name.
    CONCATENATE 'Z_' iv_table '_CDC_SEQ' INTO lv_seq_name.
  ENDIF.

  ev_log_table = lv_log_table.

  *---------------------------------------------------------------------
  * Check if log table exists (via dynamic SELECT)
  *---------------------------------------------------------------------
  TRY.
      SELECT COUNT(*) FROM (lv_log_table) INTO lv_exists.
    CATCH cx_root.
      lv_exists = 0.
  ENDTRY.

  *---------------------------------------------------------------------
  * Create log table if it doesn't exist
  * We use ADBC to execute DDL on HANA
  *---------------------------------------------------------------------
  IF lv_exists = 0.
    * Create sequence for auto-increment
    CONCATENATE 'CREATE SEQUENCE ' lv_seq_name
                ' START WITH 1 INCREMENT BY 1 NO CACHE'
                INTO lv_sql SEPARATED BY space.

    TRY.
        DATA(lo_sql) = NEW cl_sql_statement( ).
        lo_sql->execute_ddl( lv_sql ).
      CATCH cx_root INTO DATA(lo_cx).
        * Sequence might already exist — ignore
    ENDTRY.

    * Create log table via ADBC (HANA DDL)
    CONCATENATE 'CREATE COLUMN TABLE ' lv_log_table ' ('
                ' SEQ INTEGER NOT NULL,'
                ' OPERATION VARCHAR(1) NOT NULL,'
                ' KEYVALUES NVARCHAR(5000),'
                ' TIMESTMP TIMESTAMP NOT NULL,'
                ' PRIMARY KEY (SEQ)'
                ')'
                INTO lv_sql.

    TRY.
        lo_sql = NEW cl_sql_statement( ).
        lo_sql->execute_ddl( lv_sql ).
      CATCH cx_root INTO lo_cx.
        ev_error = 'Cannot create log table: ' && lo_cx->get_text( ).
        RETURN.
    ENDTRY.
  ENDIF.

  *---------------------------------------------------------------------*
  * Check if last log entry is old (gap detection)
  * Only flag gap if trigger is MISSING and log has entries
  *---------------------------------------------------------------------*
  TRY.
      SELECT MAX( seq ) FROM (lv_log_table) INTO lv_last_seq.
      IF lv_last_seq > 0.
        SELECT MAX( timestmp ) FROM (lv_log_table) INTO lv_last_time
          WHERE seq = lv_last_seq.
        ev_last_log_seq = lv_last_seq.
        ev_last_log_time = lv_last_time.

        * Calculate age in hours using cl_abap_tstmp (7.00+ compatible)
        DATA: lv_now_tstmp TYPE timestampl.
        GET TIME STAMP FIELD lv_now_tstmp.
        TRY.
            DATA(lv_diff_secs) = cl_abap_tstmp=>subtractsecs(
              tstmp1 = lv_now_tstmp
              tstmp2 = lv_last_time ).
            lv_age_hours = lv_diff_secs / 3600.
          CATCH cx_root.
            * Cannot calculate — don't flag gap
            lv_age_hours = 0.
        ENDTRY.

        * Gap is only suspicious if:
        *   1. The trigger is missing (checked below)
        *   2. The last log entry is older than threshold
        * We store the age but only set EV_GAP_DETECTED after checking trigger
      ENDIF.
    CATCH cx_root.
      * Log table empty or error — no gap
  ENDTRY.

  *---------------------------------------------------------------------*
  * Check if trigger exists (HANA system table)
  * Search for the INSERT trigger (sufficient — all three are created together)
  *---------------------------------------------------------------------*
  DATA: lv_trigger_count TYPE i.
  DATA: lv_trigger_full TYPE string.
  CONCATENATE lv_trigger_name '_INS' INTO lv_trigger_full.

  TRY.
      DATA(lo_sql2) = NEW cl_sql_statement( ).
      DATA(lo_result) = lo_sql2->execute_query(
        |SELECT COUNT(*) FROM SYS.TRIGGERS WHERE TRIGGER_NAME = '{ lv_trigger_full }'|
      ).
      lo_result->next( ).
      lv_trigger_count = lo_result->get_int( ).
      lo_result->close( ).
    CATCH cx_root.
      * Cannot check — assume trigger doesn't exist
      lv_trigger_count = 0.
  ENDTRY.

  IF lv_trigger_count > 0.
    ev_trigger_exists = 'X'.
    * Trigger exists — no gap even if log is old
    RETURN.
  ENDIF.

  *---------------------------------------------------------------------*
  * Trigger does NOT exist — check if there's a gap
  * A gap is only detected if:
  *   - Log table has entries (lv_last_seq > 0)
  *   - AND last entry is older than threshold (default 24h)
  *   - AND trigger is missing (we're here because it's missing)
  *---------------------------------------------------------------------*
  IF lv_last_seq > 0 AND lv_age_hours > iv_gap_threshold_hours.
    ev_gap_detected = 'X'.
  ENDIF.

  *---------------------------------------------------------------------
  * Create triggers (INSERT, UPDATE, DELETE)
  * Log only the key fields + operation, not the full row
  *---------------------------------------------------------------------
  * Build the key extraction expression for the trigger
  * For single key: :new_row.MATNR
  * For composite key: :new_row.MANDT || '|' || :new_row.MATNR

  DATA: lt_keyfields TYPE TABLE OF string,
        lv_key        TYPE string,
        lv_key_expr   TYPE string,
        lv_key_expr_old TYPE string.

  SPLIT iv_keyfields AT ',' INTO TABLE lt_keyfields.

  * Build new-row key expression
  LOOP AT lt_keyfields INTO lv_key.
    CONDENSE lv_key.
    IF lv_key CN 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_' <> 0.
      ev_error = 'Invalid key field name (only A-Z, 0-9, underscore allowed): ' && lv_key.
      RETURN.
    ENDIF.
    IF lv_key_expr IS INITIAL.
      CONCATENATE ':new_row.' lv_key INTO lv_key_expr.
    ELSE.
      CONCATENATE lv_key_expr ' || ''|'' || :new_row.' lv_key INTO lv_key_expr.
    ENDIF.
  ENDLOOP.

  * Build old-row key expression (for DELETE trigger)
  LOOP AT lt_keyfields INTO lv_key.
    CONDENSE lv_key.
    IF lv_key CN 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_' <> 0.
      ev_error = 'Invalid key field name (only A-Z, 0-9, underscore allowed): ' && lv_key.
      RETURN.
    ENDIF.
    IF lv_key_expr_old IS INITIAL.
      CONCATENATE ':old_row.' lv_key INTO lv_key_expr_old.
    ELSE.
      CONCATENATE lv_key_expr_old ' || ''|'' || :old_row.' lv_key INTO lv_key_expr_old.
    ENDIF.
  ENDLOOP.

  *---------------------------------------------------------------------
  * Create INSERT trigger
  *---------------------------------------------------------------------
  CONCATENATE 'CREATE TRIGGER ' lv_trigger_name '_INS '
              'AFTER INSERT ON ' iv_table ' '
              'REFERENCING NEW ROW AS new_row '
              'FOR EACH ROW '
              'BEGIN '
              '  INSERT INTO ' lv_log_table ' '
              '  (SEQ, OPERATION, KEYVALUES, TIMESTMP) '
              '  VALUES (' lv_seq_name '.NEXTVAL, ''I'', ' lv_key_expr ', CURRENT_TIMESTAMP); '
              'END'
              INTO lv_sql.

  TRY.
      lo_sql = NEW cl_sql_statement( ).
      lo_sql->execute_ddl( lv_sql ).
    CATCH cx_root INTO lo_cx.
      ev_error = 'Cannot create INSERT trigger: ' && lo_cx->get_text( ).
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------
  * Create UPDATE trigger
  *---------------------------------------------------------------------
  CONCATENATE 'CREATE TRIGGER ' lv_trigger_name '_UPD '
              'AFTER UPDATE ON ' iv_table ' '
              'REFERENCING NEW ROW AS new_row '
              'FOR EACH ROW '
              'BEGIN '
              '  INSERT INTO ' lv_log_table ' '
              '  (SEQ, OPERATION, KEYVALUES, TIMESTMP) '
              '  VALUES (' lv_seq_name '.NEXTVAL, ''U'', ' lv_key_expr ', CURRENT_TIMESTAMP); '
              'END'
              INTO lv_sql.

  TRY.
      lo_sql = NEW cl_sql_statement( ).
      lo_sql->execute_ddl( lv_sql ).
    CATCH cx_root INTO lo_cx.
      ev_error = 'Cannot create UPDATE trigger: ' && lo_cx->get_text( ).
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------
  * Create DELETE trigger
  *---------------------------------------------------------------------
  CONCATENATE 'CREATE TRIGGER ' lv_trigger_name '_DEL '
              'AFTER DELETE ON ' iv_table ' '
              'REFERENCING OLD ROW AS old_row '
              'FOR EACH ROW '
              'BEGIN '
              '  INSERT INTO ' lv_log_table ' '
              '  (SEQ, OPERATION, KEYVALUES, TIMESTMP) '
              '  VALUES (' lv_seq_name '.NEXTVAL, ''D'', ' lv_key_expr_old ', CURRENT_TIMESTAMP); '
              'END'
              INTO lv_sql.

  TRY.
      lo_sql = NEW cl_sql_statement( ).
      lo_sql->execute_ddl( lv_sql ).
    CATCH cx_root INTO lo_cx.
      ev_error = 'Cannot create DELETE trigger: ' && lo_cx->get_text( ).
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------*
  * Success — triggers created
  *---------------------------------------------------------------------*

ENDFUNCTION.