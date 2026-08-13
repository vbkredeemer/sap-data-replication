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
        lv_ddobj TYPE REF TO cl_abap_tabledescr,
        lv_structdescr TYPE REF TO cl_abap_structdescr,
        lv_ddl TYPE string,
        lv_sql TYPE string,
        lv_exists TYPE i,
        lv_last_seq TYPE i,
        lv_last_time TYPE timestampl,
        lv_age_hours TYPE i.

  CLEAR: ev_error, ev_log_table, ev_trigger_exists, ev_gap_detected,
         ev_last_log_seq, ev_last_time.

  *---------------------------------------------------------------------
  * Validate inputs
  *---------------------------------------------------------------------
  IF iv_table IS INITIAL.
    ev_error = 'IV_TABLE is empty'.
    RETURN.
  ENDIF.

  IF iv_keyfields IS INITIAL.
    ev_error = 'IV_KEYFIELDS is empty — need at least one key field'.
    RETURN.
  ENDIF.

  *---------------------------------------------------------------------
  * Build names
  *---------------------------------------------------------------------
  CONCATENATE 'Z_' iv_table '_CDC_LOG' INTO lv_log_table.
  CONCATENATE 'Z_' iv_table '_CDC_TRG' INTO lv_trigger_name.
  CONCATENATE 'Z_' iv_table '_CDC_SEQ' INTO lv_seq_name.

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
                ' KEYVALUES NVARCHAR(1000),'
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

  *---------------------------------------------------------------------
  * Check if last log entry is old (gap detection)
  *---------------------------------------------------------------------
  TRY.
      SELECT MAX( seq ) FROM (lv_log_table) INTO lv_last_seq.
      IF lv_last_seq > 0.
        SELECT MAX( timestmp ) FROM (lv_log_table) INTO lv_last_time
          WHERE seq = lv_last_seq.
        ev_last_log_seq = lv_last_seq.
        ev_last_log_time = lv_last_time.

        * Check age — if last entry is older than 6 hours, likely a gap
        DATA(lv_now) = utclong_current( ).
        DATA(lv_diff) = utclong_diff_seconds( val2 = lv_now
                                              val1 = lv_last_time ).
        lv_age_hours = lv_diff / 3600.
        IF lv_age_hours > 6.
          ev_gap_detected = 'X'.
        ENDIF.
      ENDIF.
    CATCH cx_root.
      * Log table empty or error — no gap
  ENDTRY.

  *---------------------------------------------------------------------
  * Check if trigger exists (HANA system table)
  *---------------------------------------------------------------------
  DATA: lv_trigger_count TYPE i.

  TRY.
      DATA(lo_sql2) = NEW cl_sql_statement( ).
      DATA(lo_result) = lo_sql2->execute_query(
        |SELECT COUNT(*) FROM SYS.TRIGGERS WHERE TRIGGER_NAME = '{ lv_trigger_name }'|
      ).
      lo_result->next( ).
      DATA(lv_val) = lo_result->get_char( ).
      lv_trigger_count = lv_val.
      lo_result->close( ).
    CATCH cx_root.
      * Cannot check — assume trigger doesn't exist
      lv_trigger_count = 0.
  ENDTRY.

  IF lv_trigger_count > 0.
    ev_trigger_exists = 'X'.
    * Trigger already exists — nothing to do
    RETURN.
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
    IF lv_key_expr IS INITIAL.
      CONCATENATE ':new_row.' lv_key INTO lv_key_expr.
    ELSE.
      CONCATENATE lv_key_expr ' || ''|'' || :new_row.' lv_key INTO lv_key_expr.
    ENDIF.
  ENDLOOP.

  * Build old-row key expression (for DELETE trigger)
  LOOP AT lt_keyfields INTO lv_key.
    CONDENSE lv_key.
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

  *---------------------------------------------------------------------
  * Success
  *---------------------------------------------------------------------
  ev_trigger_exists = ' '.
  IF ev_gap_detected = 'X'.
    * Trigger was re-created after a gap — client should do full load
  ENDIF.

ENDFUNCTION.