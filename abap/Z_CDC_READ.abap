*---------------------------------------------------------------------*
* Funktionsbaustein: Z_CDC_READ
* Zweck: Delta aus Log-Tabelle abholen
*        - Liest geänderte Sätze seit IV_FROM_SEQ
*        - JOIN mit Originaltabelle für vollständige Daten
*        - Chunking über IV_CHUNK_SIZE
*        - ET_DATA Format: OPERATION|feld1|feld2|...
*        - Bei DELETE: nur Key-Felder (Originalzeile existiert nicht mehr)
*---------------------------------------------------------------------*
* IMPORTING:
*   IV_TABLE       TYPE TABNAME    - SAP-Quelltabelle
*   IV_FROM_SEQ    TYPE I          - Ab dieser SEQ lesen (Lese-Pointer)
*   IV_CHUNK_SIZE  TYPE I          - Max. Zeilen pro Aufruf
*
* EXPORTING:
*   EV_ROW_COUNT   TYPE I          - Tatsächlich zurückgegebene Zeilen
*   EV_NEXT_SEQ    TYPE I          - Nächste zu lesende SEQ
*   EV_HAS_MORE    TYPE CHAR1      - 'X' = weitere Daten verfügbar
*   EV_ERROR       TYPE STRING     - Fehlermeldung
*
* TABLES:
*   ET_FIELDS      LIKE ZSQL_FIELD - Spaltenmetadaten
*   ET_DATA        LIKE ZSQL_ROW   - Pipe-delimited: OP|feld1|feld2|...
*---------------------------------------------------------------------*

FUNCTION Z_CDC_READ.
*"----------------------------------------------------------------------
*"*"Lokale Schnittstelle:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_FROM_SEQ) TYPE  I
*"     VALUE(IV_CHUNK_SIZE) TYPE  I
*"  EXPORTING
*"     VALUE(EV_ROW_COUNT) TYPE  I
*"     VALUE(EV_NEXT_SEQ) TYPE  I
*"     VALUE(EV_HAS_MORE) TYPE  CHAR1
*"     VALUE(EV_ERROR) TYPE  STRING
*"  TABLES
*"*"      ET_FIELDS LIKE  ZSQL_FIELD
*"*"      ET_DATA LIKE  ZSQL_ROW
*"----------------------------------------------------------------------

  DATA: lv_log_table     TYPE tabname,
        lv_sql           TYPE string,
        ls_field_cat     TYPE zsql_field,
        ls_data          TYPE zsql_row,
        lv_rowdata       TYPE string,
        lv_field_value   TYPE string,
        lv_check_table   TYPE string,
        lv_tabname       TYPE ddobjname,
        lv_tab_len       TYPE i,
        lv_hash_str      TYPE string,
        lv_short         TYPE string,
        lv_num_buffer    TYPE c LENGTH 50,
        lv_colpos        TYPE i,
        lo_sql_conn      TYPE REF TO cl_sql_connection,
        lo_sql_stmt      TYPE REF TO cl_sql_statement,
        lo_result        TYPE REF TO cl_sql_result_set,
        lv_count         TYPE i,
        lv_seq           TYPE i,
        lv_operation     TYPE c LENGTH 1,
        lv_keyvalues     TYPE string,
        lv_timestmp      TYPE string,
        lv_max_seq       TYPE i,
        lv_skip_entry    TYPE abap_bool,
        lo_struct_descr  TYPE REF TO cl_abap_structdescr,
        ls_dynamic       TYPE REF TO data,
        lt_keys          TYPE TABLE OF string,
        lv_key_value     TYPE string,
        lv_where         TYPE string,
        lv_key_idx       TYPE i,
        lv_remaining     TYPE i,
        lr_remaining     TYPE REF TO data.

  DATA: BEGIN OF ls_log_entry,
          seq       TYPE i,
          operation TYPE c LENGTH 1,
          keyvalues TYPE string,
          timestmp  TYPE string,
        END OF ls_log_entry,
        lr_log_entry TYPE REF TO data.

  FIELD-SYMBOLS: <fs_dynamic> TYPE ANY,
                 <fs_field>   TYPE ANY.

  CLEAR: ev_error, ev_row_count, ev_next_seq, ev_has_more.
  CLEAR: et_fields[], et_data[].

  "---------------------------------------------------------------------*
  " Validate table name — only alphanumeric and underscore allowed
  "---------------------------------------------------------------------*
  IF iv_table IS INITIAL.
    ev_error = 'IV_TABLE is empty'.
    RETURN.
  ENDIF.

  "---------------------------------------------------------------------*
  " Validate table name — only A-Z, 0-9, underscore and slash allowed
  "---------------------------------------------------------------------*
  lv_check_table = iv_table.
  CONDENSE lv_check_table NO-GAPS.
  TRANSLATE lv_check_table TO UPPER CASE.
  IF NOT lv_check_table CO 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/'.
    ev_error = |Invalid table name (only A-Z, 0-9, _, / allowed): { lv_check_table }|.
    RETURN.
  ENDIF.

  "---------------------------------------------------------------------*
  " Build log table name (must match Z_CDC_INIT logic)
  "---------------------------------------------------------------------*
  lv_tab_len = strlen( lv_check_table ).

  IF lv_tab_len > 18.
    CALL FUNCTION 'CALCULATE_HASH_FOR_CHAR'
      EXPORTING
        data       = lv_check_table
      IMPORTING
        hashstring = lv_hash_str
      EXCEPTIONS
        OTHERS     = 1.
    IF sy-subrc = 0 AND strlen( lv_hash_str ) >= 6.
      lv_short = lv_hash_str(6).
    ELSE.
      lv_short = lv_check_table(6).
    ENDIF.
    CONCATENATE 'Z_' lv_short '_CDC_LOG' INTO lv_log_table.
  ELSE.
    CONCATENATE 'Z_' lv_check_table '_CDC_LOG' INTO lv_log_table.
  ENDIF.

  "---------------------------------------------------------------------*
  " Validate chunk size
  "---------------------------------------------------------------------*
  IF iv_chunk_size <= 0.
    iv_chunk_size = 10000.
  ENDIF.

  "---------------------------------------------------------------------
  " Get table metadata via DDIF_NAMETAB_GET (flat list, includes .INCLUDE fields)
  "---------------------------------------------------------------------
  DATA: lt_nametab        TYPE TABLE OF dfies,
        ls_nametab        TYPE dfies,
        lt_ddic_keyfields TYPE TABLE OF dfies,
        ls_ddic_key       TYPE dfies.

  lv_tabname = lv_check_table.

  CALL FUNCTION 'DDIF_NAMETAB_GET'
    EXPORTING
      tabname   = lv_tabname
    TABLES
      dfies_tab = lt_nametab
    EXCEPTIONS
      OTHERS    = 1.

  IF sy-subrc <> 0.
    ev_error = 'Cannot get nametab for ' && lv_check_table.
    RETURN.
  ENDIF.

  " Filter out .INCLUDE entries (fieldname starts with '.')
  DELETE lt_nametab WHERE fieldname(1) = '.'.

  " Key fields for WHERE clause building
  lt_ddic_keyfields = lt_nametab.
  DELETE lt_ddic_keyfields WHERE keyflag <> 'X'.

  " Build ET_FIELDS from nametab (flat list, no .INCLUDE nesting issues)
  lv_colpos = 1.
  LOOP AT lt_nametab INTO ls_nametab.
    CLEAR ls_field_cat.
    ls_field_cat-fieldname = ls_nametab-fieldname.
    ls_field_cat-colpos = lv_colpos.
    lv_colpos = lv_colpos + 1.

    " Map ABAP type to DATATYPE using INTTYPE
    CASE ls_nametab-inttype.
      WHEN 'C' OR 'g'.
        ls_field_cat-datatype = 'C'.
      WHEN 'D'.
        ls_field_cat-datatype = 'D'.
      WHEN 'T'.
        ls_field_cat-datatype = 'T'.
      WHEN 'X' OR 'y'.
        ls_field_cat-datatype = 'X'.
      WHEN 'P'.
        ls_field_cat-datatype = 'P'.
      WHEN 'F'.
        ls_field_cat-datatype = 'F'.
      WHEN 'I'.
        ls_field_cat-datatype = 'I'.
      WHEN 's'.
        ls_field_cat-datatype = 'INT2'.
      WHEN 'b'.
        ls_field_cat-datatype = 'INT1'.
      WHEN 'N'.
        ls_field_cat-datatype = 'N'.
      WHEN OTHERS.
        ls_field_cat-datatype = 'C'.
    ENDCASE.

    ls_field_cat-length = ls_nametab-leng.
    ls_field_cat-decimals = ls_nametab-decimals.
    APPEND ls_field_cat TO et_fields.
  ENDLOOP.

  "---------------------------------------------------------------------
  " Read log table entries via ADBC
  " Then for each log entry, read the original row
  "---------------------------------------------------------------------
  TRY.
      lo_sql_conn = cl_sql_connection=>get_connection( ).
      lo_sql_stmt = lo_sql_conn->create_statement( ).
    CATCH cx_root INTO DATA(lo_cx_conn).
      ev_error = 'Cannot get SQL connection: ' && lo_cx_conn->get_text( ).
      RETURN.
  ENDTRY.

  "---------------------------------------------------------------------*
  " Query log table: get SEQ, OPERATION, KEYVALUES, TIMESTMP
  "---------------------------------------------------------------------*
  lv_sql = |SELECT SEQ, OPERATION, KEYVALUES, TIMESTMP FROM { lv_log_table }| &&
           | WHERE SEQ > { iv_from_seq } ORDER BY SEQ ASC|.

  TRY.
      lo_result = lo_sql_stmt->execute_query( lv_sql ).
    CATCH cx_root INTO lo_cx_conn.
      ev_error = 'Cannot query log table: ' && lo_cx_conn->get_text( ).
      RETURN.
  ENDTRY.

  " Bind structure to result set for ADBC row fetching
  GET REFERENCE OF ls_log_entry INTO lr_log_entry.
  lo_result->set_param_struct( lr_log_entry ).

  "---------------------------------------------------------------------
  " Dynamic structure for original table via RTTS
  "---------------------------------------------------------------------
  TRY.
      lo_struct_descr ?= cl_abap_structdescr=>describe_by_name( lv_tabname ).
    CATCH cx_root INTO DATA(lo_cx_desc).
      ev_error = 'Cannot describe table ' && lv_check_table && ': ' && lo_cx_desc->get_text( ).
      RETURN.
  ENDTRY.

  CREATE DATA ls_dynamic TYPE HANDLE lo_struct_descr.
  ASSIGN ls_dynamic->* TO <fs_dynamic>.

  lv_count = 0.

  "---------------------------------------------------------------------
  " Fetch log entries and build result rows
  "---------------------------------------------------------------------
  WHILE lo_result->next( ) > 0.
    lv_seq       = ls_log_entry-seq.
    lv_operation = ls_log_entry-operation.
    lv_keyvalues = ls_log_entry-keyvalues.
    lv_timestmp  = ls_log_entry-timestmp.

    lv_max_seq = lv_seq.

    "-------------------------------------------------------------------*
    " Build row data
    "-------------------------------------------------------------------*
    CLEAR lv_rowdata.

    " Prefix with operation
    lv_rowdata = lv_operation.

    IF lv_operation = 'D'.
      " DELETE: only key values
      CONCATENATE lv_rowdata lv_keyvalues INTO lv_rowdata SEPARATED BY '|'.
    ELSE.
      " INSERT or UPDATE: read original row
      SPLIT lv_keyvalues AT '|' INTO TABLE lt_keys.

      " Build WHERE from key fields and key values
      CLEAR lv_where.

      lv_key_idx = 0.
      CLEAR lv_skip_entry.
      LOOP AT lt_ddic_keyfields INTO ls_ddic_key.
        lv_key_idx = lv_key_idx + 1.
        READ TABLE lt_keys INTO lv_key_value INDEX lv_key_idx.
        IF sy-subrc <> 0.
          " Key value missing — skip this malformed entry
          lv_skip_entry = abap_true.
          EXIT.
        ENDIF.
        " Escape single quotes to prevent SQL injection
        REPLACE ALL OCCURRENCES OF '''' IN lv_key_value WITH ''''''.
        IF lv_where IS INITIAL.
          CONCATENATE ls_ddic_key-fieldname ' = ''' lv_key_value '''' INTO lv_where.
        ELSE.
          CONCATENATE lv_where ' AND ' ls_ddic_key-fieldname ' = ''' lv_key_value '''' INTO lv_where.
        ENDIF.
      ENDLOOP.

      IF lv_skip_entry = abap_true.
        CONTINUE.
      ENDIF.

      " Read original row
      TRY.
          SELECT SINGLE * FROM (lv_check_table) INTO <fs_dynamic>
            WHERE (lv_where).
          IF sy-subrc = 0.
            " Build pipe-delimited row from all fields in ET_FIELDS
            LOOP AT et_fields INTO ls_field_cat.
              ASSIGN COMPONENT ls_field_cat-fieldname OF STRUCTURE <fs_dynamic> TO <fs_field>.
              IF sy-subrc = 0.
                CLEAR lv_field_value.
                CASE ls_field_cat-datatype.
                  WHEN 'D'.
                    " SAP DATE YYYYMMDD → MSSQL YYYY-MM-DD
                    IF <fs_field> IS NOT INITIAL.
                      DATA(lv_d) = |{ <fs_field> }|.
                      IF strlen( lv_d ) = 8.
                        CONCATENATE lv_d(4) '-' lv_d+4(2) '-' lv_d+6(2) INTO lv_field_value.
                      ELSE.
                        lv_field_value = lv_d.
                      ENDIF.
                    ENDIF.

                  WHEN 'T'.
                    " SAP TIME HHMMSS → MSSQL HH:MM:SS
                    IF <fs_field> IS NOT INITIAL.
                      DATA(lv_t) = |{ <fs_field> }|.
                      IF strlen( lv_t ) = 6.
                        CONCATENATE lv_t(2) ':' lv_t+2(2) ':' lv_t+4(2) INTO lv_field_value.
                      ELSE.
                        lv_field_value = lv_t.
                      ENDIF.
                    ENDIF.

                  WHEN 'I' OR 'INT1' OR 'INT2'.
                    lv_field_value = |{ <fs_field> }|.
                    CONDENSE lv_field_value.

                  WHEN 'P'.
                    " Packed decimal with dot, no thousand separators
                    IF <fs_field> IS NOT INITIAL.
                      WRITE <fs_field> TO lv_num_buffer NO-GROUPING NO-SIGN.
                      lv_field_value = lv_num_buffer.
                      IF <fs_field> < 0.
                        CONCATENATE '-' lv_field_value INTO lv_field_value.
                      ENDIF.
                      REPLACE ALL OCCURRENCES OF ',' IN lv_field_value WITH '.'.
                      CONDENSE lv_field_value NO-GAPS.
                    ENDIF.

                  WHEN 'F'.
                    " Float with dot notation
                    IF <fs_field> IS NOT INITIAL.
                      WRITE <fs_field> TO lv_num_buffer NO-GROUPING NO-SIGN.
                      lv_field_value = lv_num_buffer.
                      IF <fs_field> < 0.
                        CONCATENATE '-' lv_field_value INTO lv_field_value.
                      ENDIF.
                      REPLACE ALL OCCURRENCES OF ',' IN lv_field_value WITH '.'.
                      CONDENSE lv_field_value NO-GAPS.
                    ENDIF.

                  WHEN 'X'.
                    " RAW → hex string with 0x prefix
                    DATA(lv_x) = |{ <fs_field> }|.
                    CONCATENATE '0x' lv_x INTO lv_field_value.

                  WHEN OTHERS.
                    " CHAR/STRING — remove trailing spaces only (preserve leading)
                    lv_field_value = <fs_field>.
                    SHIFT lv_field_value RIGHT DELETING TRAILING space.
                ENDCASE.

                CONCATENATE lv_rowdata lv_field_value INTO lv_rowdata SEPARATED BY '|'.
              ELSE.
                CONCATENATE lv_rowdata '' INTO lv_rowdata SEPARATED BY '|'.
              ENDIF.
            ENDLOOP.
          ELSE.
            " Row not found — INSERT/UPDATE row was deleted before we could read it
            CONTINUE.
          ENDIF.
        CATCH cx_root.
          CONTINUE.
      ENDTRY.
    ENDIF.

    ls_data-rowdata = lv_rowdata.
    APPEND ls_data TO et_data.
    lv_count = lv_count + 1.
    IF lv_count >= iv_chunk_size.
      EXIT.
    ENDIF.
  ENDWHILE.

  TRY.
      lo_result->close( ).
    CATCH cx_root.
  ENDTRY.

  "---------------------------------------------------------------------*
  " Set return values
  "---------------------------------------------------------------------*
  ev_row_count = lv_count.
  IF lv_count = 0.
    IF lv_max_seq > 0.
      ev_next_seq = lv_max_seq + 1.
      ev_has_more = 'X'.
    ELSE.
      ev_next_seq = iv_from_seq.
      ev_has_more = ' '.
    ENDIF.
  ELSE.
    IF lv_max_seq < iv_from_seq.
      ev_next_seq = iv_from_seq.
    ELSE.
      ev_next_seq = lv_max_seq + 1.
    ENDIF.

    lv_sql = |SELECT COUNT(*) FROM { lv_log_table } WHERE SEQ > { lv_max_seq }|.

    TRY.
        lo_result = lo_sql_stmt->execute_query( lv_sql ).
        GET REFERENCE OF lv_remaining INTO lr_remaining.
        lo_result->set_param( lr_remaining ).
        IF lo_result->next( ) > 0.
          " lv_remaining is populated directly
        ELSE.
          lv_remaining = 0.
        ENDIF.
        lo_result->close( ).
      CATCH cx_root.
        lv_remaining = 0.
    ENDTRY.

    IF lv_remaining > 0.
      ev_has_more = 'X'.
    ENDIF.
  ENDIF.

ENDFUNCTION.
