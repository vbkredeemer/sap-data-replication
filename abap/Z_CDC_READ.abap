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
*   ET_FIELDS      STRUCTURE ZSQL_FIELD - Spaltenmetadaten
*   ET_DATA        STRUCTURE ZSQL_ROW   - Pipe-delimited: OP|feld1|feld2|...
*---------------------------------------------------------------------*

FUNCTION Z_CDC_READ.
*"----------------------------------------------------------------------
*"*"Lokale Schnittstelle:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_FROM_SEQ) TYPE  I DEFAULT 0
*"     VALUE(IV_CHUNK_SIZE) TYPE  I DEFAULT 10000
*"  EXPORTING
*"     VALUE(EV_ROW_COUNT) TYPE  I
*"     VALUE(EV_NEXT_SEQ) TYPE  I
*"     VALUE(EV_HAS_MORE) TYPE  CHAR1
*"     VALUE(EV_ERROR) TYPE  STRING
*"  TABLES
*"     ET_FIELDS STRUCTURE ZSQL_FIELD
*"     ET_DATA STRUCTURE ZSQL_ROW
*"----------------------------------------------------------------------

  DATA: lv_log_table TYPE tabname,
        lv_sql TYPE string,
        lt_keyfields TYPE TABLE OF string,
        ls_field_cat TYPE ZSQL_FIELD,
        ls_data TYPE ZSQL_ROW,
        lv_rowdata TYPE string,
        lv_field_value TYPE string.

  CLEAR: ev_error, ev_row_count, ev_next_seq, ev_has_more.
  CLEAR: et_fields[], et_data[].

  *---------------------------------------------------------------------
  * Build log table name
  *---------------------------------------------------------------------
  CONCATENATE 'Z_' iv_table '_CDC_LOG' INTO lv_log_table.

  *---------------------------------------------------------------------
  * Validate
  *---------------------------------------------------------------------
  IF iv_table IS INITIAL.
    ev_error = 'IV_TABLE is empty'.
    RETURN.
  ENDIF.

  IF iv_chunk_size <= 0.
    iv_chunk_size = 10000.
  ENDIF.

  *---------------------------------------------------------------------
  * Get table metadata via RTTS
  *---------------------------------------------------------------------
  DATA: lo_struct_descr TYPE REF TO cl_abap_structdescr,
        lt_components   TYPE cl_abap_structdescr=>component_table,
        ls_component    TYPE abap_componentdescr.

  TRY.
      lo_struct_descr ?= cl_abap_structdescr=>describe_by_name( iv_table ).
    CATCH cx_root.
      ev_error = 'Cannot describe table ' && iv_table.
      RETURN.
  ENDTRY.

  lt_components = lo_struct_descr->get_components( ).

  *---------------------------------------------------------------------
  * Build ET_FIELDS metadata
  *---------------------------------------------------------------------
  DATA: lv_colpos TYPE i.
  lv_colpos = 1.

  LOOP AT lt_components INTO ls_component.
    CLEAR ls_field_cat.
    ls_field_cat-fieldname = ls_component-name.

    CASE ls_component-type_kind.
      WHEN cl_abap_structdescr=>typekind_char
        OR cl_abap_structdescr=>typekind_string.
        ls_field_cat-datatype = 'C'.
      WHEN cl_abap_structdescr=>typekind_int.
        ls_field_cat-datatype = 'I'.
      WHEN cl_abap_structdescr=>typekind_int2.
        ls_field_cat-datatype = 'INT2'.
      WHEN cl_abap_structdescr=>typekind_int1.
        ls_field_cat-datatype = 'INT1'.
      WHEN cl_abap_structdescr=>typekind_packed.
        ls_field_cat-datatype = 'P'.
      WHEN cl_abap_structdescr=>typekind_float.
        ls_field_cat-datatype = 'F'.
      WHEN cl_abap_structdescr=>typekind_date.
        ls_field_cat-datatype = 'D'.
      WHEN cl_abap_structdescr=>typekind_time.
        ls_field_cat-datatype = 'T'.
      WHEN cl_abap_structdescr=>typekind_hex.
        ls_field_cat-datatype = 'X'.
      WHEN OTHERS.
        ls_field_cat-datatype = 'C'.
    ENDCASE.

    ls_field_cat-length = ls_component-length.
    ls_field_cat-decimals = ls_component-decimals.
    ls_field_cat-colpos = lv_colpos.
    lv_colpos = lv_colpos + 1.
    APPEND ls_field_cat TO et_fields.
  ENDLOOP.

  *---------------------------------------------------------------------
  * Read log table entries via ADBC
  * Then for each log entry, read the original row
  *---------------------------------------------------------------------
  DATA: lo_sql_conn TYPE REF TO cl_sql_connection,
        lo_sql_stmt TYPE REF TO cl_sql_statement,
        lo_result   TYPE REF TO cl_sql_result_cursor,
        lv_count    TYPE i.

  TRY.
      lo_sql_conn = cl_sql_connection=>get_connection( ).
      lo_sql_stmt = lo_sql_conn->create_statement( ).
    CATCH cx_root INTO DATA(lo_cx_conn).
      ev_error = 'Cannot get SQL connection: ' && lo_cx_conn->get_text( ).
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------
  * Query log table: get SEQ, OPERATION, KEYVALUES
  *---------------------------------------------------------------------
  CONCATENATE 'SELECT SEQ, OPERATION, KEYVALUES, TIMESTMP FROM '
              lv_log_table
              ' WHERE SEQ > ' iv_from_seq
              ' ORDER BY SEQ ASC'
              ' LIMIT ' iv_chunk_size
              INTO lv_sql.

  TRY.
      lo_result = lo_sql_stmt->execute_query( lv_sql ).
    CATCH cx_root INTO lo_cx_conn.
      ev_error = 'Cannot query log table: ' && lo_cx_conn->get_text( ).
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------
  * Fetch log entries and build result rows
  *---------------------------------------------------------------------
  DATA: lv_seq        TYPE i,
        lv_operation  TYPE c LENGTH 1,
        lv_keyvalues  TYPE string,
        lv_timestmp   TYPE string,
        lv_max_seq    TYPE i.

  FIELD-SYMBOLS: <fs_dynamic> TYPE ANY,
                 <fs_field>   TYPE ANY.

  * Create dynamic structure for original table
  DATA: lo_table_descr TYPE REF TO cl_abap_tabledescr,
        lo_data_ref    TYPE REF TO data,
        lt_dynamic     TYPE REF TO data,
        ls_dynamic     TYPE REF TO data.

  lo_table_descr = cl_abap_tabledescr=>create( lo_struct_descr ).
  CREATE DATA ls_dynamic TYPE HANDLE lo_struct_descr.

  lv_count = 0.

  WHILE lo_result->next( ) = 0.
    * Read log columns
    DATA(lv_seq_str) = lo_result->get_char( ).
    lv_seq = lv_seq_str.
    lv_operation = lo_result->get_char( ).
    lv_keyvalues = lo_result->get_char( ).
    lv_timestmp = lo_result->get_char( ).

    lv_max_seq = lv_seq.
    lv_count = lv_count + 1.

    *-------------------------------------------------------------------
    * Build row data
    *-------------------------------------------------------------------
    CLEAR lv_rowdata.

    * Prefix with operation
    lv_rowdata = lv_operation.

    IF lv_operation = 'D'.
      * DELETE: only key values
      CONCATENATE lv_rowdata lv_keyvalues INTO lv_rowdata SEPARATED BY '|'.
    ELSE.
      * INSERT or UPDATE: read original row
      * Parse keyvalues and build WHERE clause
      DATA: lt_keys TYPE TABLE OF string,
            lv_key_field TYPE string,
            lv_key_value TYPE string,
            lv_where TYPE string,
            lv_key_idx TYPE i.

      SPLIT lv_keyvalues AT '|' INTO TABLE lt_keys.

      LOOP AT lt_keyfields INTO DATA(lv_kf).
        " Not used — we use field names from DDIC
      ENDLOOP.

      * Build WHERE from key fields and key values
      * Get key field names from DDIC
      DATA: lt_ddic_keyfields TYPE TABLE OF dfies,
            ls_ddic_key       TYPE dfies.

      CALL FUNCTION 'DDIF_NAMETAB_GET'
        EXPORTING
          tabname   = iv_table
        TABLES
          dfies_tab = lt_ddic_keyfields
        EXCEPTIONS
          OTHERS    = 1.

      * Filter to key fields only
      DELETE lt_ddic_keyfields WHERE keyflag <> 'X'.

      lv_key_idx = 0.
      LOOP AT lt_ddic_keyfields INTO ls_ddic_key.
        lv_key_idx = lv_key_idx + 1.
        READ TABLE lt_keys INTO lv_key_value INDEX lv_key_idx.
        IF sy-subrc <> 0.
          lv_key_value = ''.
        ENDIF.
        IF lv_where IS INITIAL.
          CONCATENATE ls_ddic_key-fieldname ' = ''' lv_key_value '''' INTO lv_where.
        ELSE.
          CONCATENATE lv_where ' AND ' ls_ddic_key-fieldname ' = ''' lv_key_value '''' INTO lv_where SEPARATED BY space.
        ENDIF.
      ENDLOOP.

      * Read original row
      TRY.
          SELECT SINGLE * FROM (iv_table) INTO ls_dynamic
            WHERE (lv_where).
          IF sy-subrc = 0.
            * Build pipe-delimited row from all fields in ET_FIELDS
            * Type-aware conversion to MSSQL-compatible formats
            LOOP AT et_fields INTO ls_field_cat.
              ASSIGN COMPONENT ls_field_cat-fieldname OF STRUCTURE ls_dynamic TO <fs_field>.
              IF sy-subrc = 0.
                CLEAR lv_field_value.
                CASE ls_field_cat-datatype.
                  WHEN 'D'.
                    * SAP DATE YYYYMMDD → MSSQL YYYY-MM-DD
                    IF <fs_field> IS NOT INITIAL.
                      DATA(lv_d) = |{ <fs_field> }|.
                      IF strlen( lv_d ) = 8.
                        CONCATENATE lv_d(4) '-' lv_d+4(2) '-' lv_d+6(2) INTO lv_field_value.
                      ELSE.
                        lv_field_value = lv_d.
                      ENDIF.
                    ENDIF.
                  WHEN 'T'.
                    * SAP TIME HHMMSS → MSSQL HH:MM:SS
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
                    * Packed decimal with dot, no thousand separators
                    IF <fs_field> IS NOT INITIAL.
                      WRITE <fs_field> TO lv_field_value NO-GROUPING.
                      REPLACE ALL OCCURRENCES OF ',' IN lv_field_value WITH '.'.
                      CONDENSE lv_field_value.
                      SHIFT lv_field_value LEFT DELETING LEADING SPACE.
                    ENDIF.
                  WHEN 'F'.
                    * Float with dot notation
                    IF <fs_field> IS NOT INITIAL.
                      WRITE <fs_field> TO lv_field_value NO-GROUPING.
                      REPLACE ALL OCCURRENCES OF ',' IN lv_field_value WITH '.'.
                      CONDENSE lv_field_value.
                      SHIFT lv_field_value LEFT DELETING LEADING SPACE.
                    ENDIF.
                  WHEN 'X'.
                    * RAW → hex string with 0x prefix
                    DATA(lv_x) = |{ <fs_field> }|.
                    CONCATENATE '0x' lv_x INTO lv_field_value.
                  WHEN OTHERS.
                    * CHAR/STRING — remove leading/trailing spaces
                    lv_field_value = |{ <fs_field> }|.
                    SHIFT lv_field_value RIGHT DELETING TRAILING space.
                    SHIFT lv_field_value LEFT DELETING LEADING space.
                ENDCASE.
                CONCATENATE lv_rowdata lv_field_value INTO lv_rowdata SEPARATED BY '|'.
              ELSE.
                CONCATENATE lv_rowdata '' INTO lv_rowdata SEPARATED BY '|'.
              ENDIF.
            ENDLOOP.
          ELSE.
            * Row not found — treat as delete
            lv_rowdata = 'D'.
            CONCATENATE lv_rowdata lv_keyvalues INTO lv_rowdata SEPARATED BY '|'.
          ENDIF.
        CATCH cx_root.
          * Error reading original row — log as delete
          lv_rowdata = 'D'.
          CONCATENATE lv_rowdata lv_keyvalues INTO lv_rowdata SEPARATED BY '|'.
      ENDTRY.
    ENDIF.

    ls_data-rowdata = lv_rowdata.
    APPEND ls_data TO et_data.
  ENDWHILE.

  TRY.
      lo_result->close( ).
    CATCH cx_root.
  ENDTRY.

  *---------------------------------------------------------------------
  * Set return values
  *---------------------------------------------------------------------
  ev_row_count = lv_count.
  ev_next_seq = lv_max_seq + 1.

  * Check if there are more entries
  DATA: lv_remaining TYPE i.
  CONCATENATE 'SELECT COUNT(*) FROM ' lv_log_table
              ' WHERE SEQ > ' lv_max_seq
              INTO lv_sql.

  TRY.
      lo_result = lo_sql_stmt->execute_query( lv_sql ).
      lo_result->next( ).
      DATA(lv_rem_str) = lo_result->get_char( ).
      lv_remaining = lv_rem_str.
      lo_result->close( ).
    CATCH cx_root.
      lv_remaining = 0.
  ENDTRY.

  IF lv_remaining > 0.
    ev_has_more = 'X'.
  ENDIF.

ENDFUNCTION.