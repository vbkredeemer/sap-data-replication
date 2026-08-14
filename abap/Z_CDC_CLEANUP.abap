*---------------------------------------------------------------------*
* Funktionsbaustein: Z_CDC_CLEANUP
* Zweck: Log-Tabelle aufräumen oder CDC komplett entfernen
*
* Modi:
*   1. IV_UP_TO_SEQ > 0, IV_REMOVE_ALL = ' '
*      → Löscht Log-Einträge bis IV_UP_TO_SEQ (nach erfolgreichem Sync)
*
*   2. IV_REMOVE_ALL = 'X'
*      → Entfernt Trigger, Sequence und Log-Tabelle komplett
*---------------------------------------------------------------------*
* IMPORTING:
*   IV_TABLE       TYPE TABNAME    - SAP-Quelltabelle
*   IV_UP_TO_SEQ   TYPE I          - Log bis zu dieser SEQ löschen
*   IV_REMOVE_ALL  TYPE CHAR1      - 'X' = alles entfernen (Trigger+Log+Seq)
*
* EXPORTING:
*   EV_DELETED     TYPE I          - Anzahl gelöschter Log-Einträge
*   EV_ERROR       TYPE STRING     - Fehlermeldung
*---------------------------------------------------------------------*

FUNCTION Z_CDC_CLEANUP.
*"----------------------------------------------------------------------
*"*"Lokale Schnittstelle:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_UP_TO_SEQ) TYPE  I DEFAULT 0
*"     VALUE(IV_REMOVE_ALL) TYPE  CHAR1 DEFAULT ' '
*"  EXPORTING
*"     VALUE(EV_DELETED) TYPE  I
*"     VALUE(EV_ERROR) TYPE  STRING
*"----------------------------------------------------------------------

  DATA: lv_log_table TYPE tabname,
        lv_trigger_name TYPE string,
        lv_seq_name TYPE string,
        lv_sql TYPE string,
        lv_check_table TYPE string.

  CLEAR: ev_error, ev_deleted.

  "---------------------------------------------------------------------*
  " Validate
  "---------------------------------------------------------------------*
  IF iv_table IS INITIAL.
    ev_error = 'IV_TABLE is empty'.
    RETURN.
  ENDIF.

  " Validate table name — only A-Z, 0-9, underscore and slash (for namespaces like /BIC/) allowed
  lv_check_table = iv_table.
  CONDENSE lv_check_table NO-GAPS.
  TRANSLATE lv_check_table TO UPPER CASE.
  IF NOT lv_check_table CO 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/'.
    ev_error = |Invalid table name (only A-Z, 0-9, _, / allowed): { lv_check_table }|.
    RETURN.
  ENDIF.

  "---------------------------------------------------------------------*
  " Build names (must match Z_CDC_INIT logic)
  "---------------------------------------------------------------------*
  DATA: lv_tab_len TYPE i.
  lv_tab_len = strlen( lv_check_table ).

  IF lv_tab_len > 18.
    CALL FUNCTION 'CALCULATE_HASH_FOR_CHAR'
      EXPORTING
        data = lv_check_table
      IMPORTING
        hashstring = DATA(lv_hash_str)
      EXCEPTIONS
        OTHERS = 1.
    IF sy-subrc = 0 AND strlen( lv_hash_str ) >= 6.
      DATA(lv_short) = lv_hash_str(6).
    ELSE.
      lv_short = lv_check_table(6).
    ENDIF.
    CONCATENATE 'Z_' lv_short '_CDC_LOG' INTO lv_log_table.
    CONCATENATE 'Z_' lv_short '_CDC_TRG' INTO lv_trigger_name.
    CONCATENATE 'Z_' lv_short '_CDC_SEQ' INTO lv_seq_name.
  ELSE.
    CONCATENATE 'Z_' lv_check_table '_CDC_LOG' INTO lv_log_table.
    CONCATENATE 'Z_' lv_check_table '_CDC_TRG' INTO lv_trigger_name.
    CONCATENATE 'Z_' lv_check_table '_CDC_SEQ' INTO lv_seq_name.
  ENDIF.

  "---------------------------------------------------------------------
  " Get SQL connection
  "---------------------------------------------------------------------
  DATA: lo_sql_stmt TYPE REF TO cl_sql_statement.

  TRY.
      DATA(lo_sql_conn) = cl_sql_connection=>get_connection( ).
      lo_sql_stmt = lo_sql_conn->create_statement( ).
    CATCH cx_root INTO DATA(lo_cx_conn).
      ev_error = 'Cannot get SQL connection: ' && lo_cx_conn->get_text( ).
      RETURN.
  ENDTRY.

  "---------------------------------------------------------------------
  " Mode 1: Cleanup log entries up to IV_UP_TO_SEQ
  "---------------------------------------------------------------------
  IF iv_remove_all = ' ' AND iv_up_to_seq > 0.
    CONCATENATE 'DELETE FROM ' lv_log_table
                ' WHERE SEQ <= ' iv_up_to_seq
                INTO lv_sql.

    TRY.
        DATA(lo_result) = lo_sql_stmt->execute_update( lv_sql ).
        " execute_update returns affected rows for DML
        ev_deleted = lo_result.
      CATCH cx_root INTO lo_cx_conn.
        ev_error = 'Cannot delete log entries: ' && lo_cx_conn->get_text( ).
        RETURN.
    ENDTRY.

    RETURN.
  ENDIF.

  "---------------------------------------------------------------------
  " Mode 2: Remove all (triggers, sequence, log table)
  "---------------------------------------------------------------------
  IF iv_remove_all = 'X'.

    " Drop INSERT trigger
    CONCATENATE 'DROP TRIGGER ' lv_trigger_name '_INS' INTO lv_sql.
    TRY. lo_sql_stmt->execute_ddl( lv_sql ). CATCH cx_root. ENDTRY.

    " Drop UPDATE trigger
    CONCATENATE 'DROP TRIGGER ' lv_trigger_name '_UPD' INTO lv_sql.
    TRY. lo_sql_stmt->execute_ddl( lv_sql ). CATCH cx_root. ENDTRY.

    " Drop DELETE trigger
    CONCATENATE 'DROP TRIGGER ' lv_trigger_name '_DEL' INTO lv_sql.
    TRY. lo_sql_stmt->execute_ddl( lv_sql ). CATCH cx_root. ENDTRY.

    " Drop sequence
    CONCATENATE 'DROP SEQUENCE ' lv_seq_name INTO lv_sql.
    TRY. lo_sql_stmt->execute_ddl( lv_sql ). CATCH cx_root. ENDTRY.

    " Drop log table
    CONCATENATE 'DROP TABLE ' lv_log_table INTO lv_sql.
    TRY.
        lo_sql_stmt->execute_ddl( lv_sql ).
      CATCH cx_root INTO lo_cx_conn.
        ev_error = 'Cannot drop log table: ' && lo_cx_conn->get_text( ).
        RETURN.
    ENDTRY.

    RETURN.
  ENDIF.

  "---------------------------------------------------------------------
  " Nothing to do
  "---------------------------------------------------------------------
  IF iv_remove_all = ' ' AND iv_up_to_seq = 0.
    ev_error = 'Either IV_UP_TO_SEQ > 0 or IV_REMOVE_ALL = ''X'' required'.
    RETURN.
  ENDIF.

ENDFUNCTION.