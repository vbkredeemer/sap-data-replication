*---------------------------------------------------------------------*
* Funktionsbaustein: Z_EXPORT_TABLE
* Zweck: Exportiert eine SAP-Tabelle als CSV-Flatfile auf das
*        SAP-Server-Dateisystem. Wird von Python-Client aufgerufen.
*
*        - Dynamisches SELECT mit optionalem Datumsfilter
*        - CSV-Datei mit Pipe-Delimiter (|) und Header-Zeile
*        - Asynchrone Ausführung als Background-Job möglich
*        - Gibt Dateipfad und Zeilenanzahl zurück
*---------------------------------------------------------------------*
* IMPORTING:
*   IV_TABLE       TYPE TABNAME     - SAP-Quelltabelle (z.B. 'MARA')
*   IV_DATE_FIELD  TYPE STRING      - Datumsfeld für Filter (z.B. 'AEDAT'), optional
*   IV_DATE_FROM   TYPE DATUM       - Von-Datum (YYYYMMDD), optional
*   IV_DATE_TO     TYPE DATUM       - Bis-Datum (YYYYMMDD), optional
*   IV_FIELDS      TYPE STRING      - Feldliste komma-separiert (z.B. 'MATNR,MTART'), '*' = alle
*   IV_MAX_ROWS    TYPE I           - Max. Zeilen (0 = alle)
*   IV_FILE_PATH   TYPE STRING      - Verzeichnis für CSV (z.B. '/usr/sap/trans/data/')
*                                    Wenn leer: Default-Verzeichnis wird verwendet
*
* EXPORTING:
*   EV_FILE_NAME   TYPE STRING      - Vollständiger Dateipfad der erzeugten CSV
*   EV_ROW_COUNT   TYPE I           - Anzahl geschriebener Zeilen
*   EV_FILE_SIZE   TYPE I           - Dateigröße in Bytes (approx.)
*   EV_ERROR       TYPE STRING      - Fehlermeldung
*---------------------------------------------------------------------*
* Die CSV-Datei hat folgendes Format:
*   Zeile 1: Header mit Feldnamen (pipe-delimited)
*   Zeile 2+: Daten (pipe-delimited)
*   Kein Quoting, keine Escape-Sequenzen — Pipe ist sicher da SAP-Felder
*   normalerweise keine Pipes enthalten.
*---------------------------------------------------------------------*

FUNCTION Z_EXPORT_TABLE.
*"----------------------------------------------------------------------
*"*"Lokale Schnittstelle:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_DATE_FIELD) TYPE  STRING
*"     VALUE(IV_DATE_FROM) TYPE  DATUM OPTIONAL
*"     VALUE(IV_DATE_TO) TYPE  DATUM OPTIONAL
*"     VALUE(IV_FIELDS) TYPE  STRING
*"     VALUE(IV_MAX_ROWS) TYPE  I
*"     VALUE(IV_FILE_PATH) TYPE  STRING
*"  EXPORTING
*"     REFERENCE(EV_FILE_NAME) TYPE  STRING
*"     REFERENCE(EV_ROW_COUNT) TYPE  I
*"     REFERENCE(EV_FILE_SIZE) TYPE  I
*"     REFERENCE(EV_ERROR) TYPE  STRING
*"----------------------------------------------------------------------

  DATA: lv_select        TYPE string,
        lv_where         TYPE string,
        lv_fields        TYPE string,
        lv_file          TYPE string,
        lv_filename      TYPE string,
        lv_timestamp     TYPE string,
        lv_header        TYPE string,
        lv_row           TYPE string,
        lv_char_val      TYPE string,
        lv_count         TYPE i,
        lv_size          TYPE i,
        lv_check_table   TYPE string,
        lv_check_field   TYPE string,
        lv_tabname       TYPE ddobjname,
        lv_file_len      TYPE i,
        lv_last_pos      TYPE i,
        lv_date_str      TYPE c LENGTH 8,
        lv_time_str      TYPE c LENGTH 6,
        lv_num_buffer    TYPE c LENGTH 50,
        lv_max_fetch     TYPE i.

  CLEAR: ev_error, ev_file_name, ev_row_count, ev_file_size.

  "---------------------------------------------------------------------
  " Validate inputs
  "---------------------------------------------------------------------
  IF iv_table IS INITIAL.
    ev_error = 'IV_TABLE is empty'.
    RETURN.
  ENDIF.

  lv_check_table = iv_table.
  CONDENSE lv_check_table NO-GAPS.
  TRANSLATE lv_check_table TO UPPER CASE.
  IF NOT lv_check_table CO 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/'.
    ev_error = |Invalid table name (only A-Z, 0-9, _, / allowed): { lv_check_table }|.
    RETURN.
  ENDIF.

  lv_tabname = lv_check_table.

  "---------------------------------------------------------------------
  " Build field list
  "---------------------------------------------------------------------
  IF iv_fields IS INITIAL OR iv_fields = '*'.
    lv_fields = '*'.
  ELSE.
    lv_fields = iv_fields.
  ENDIF.

  "---------------------------------------------------------------------
  " Validate date field name if provided
  "---------------------------------------------------------------------
  IF iv_date_field IS NOT INITIAL.
    lv_check_field = iv_date_field.
    CONDENSE lv_check_field NO-GAPS.
    TRANSLATE lv_check_field TO UPPER CASE.
    IF NOT lv_check_field CO 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/'.
      ev_error = |Invalid date field name (only A-Z, 0-9, _, / allowed): { lv_check_field }|.
      RETURN.
    ENDIF.
  ENDIF.

  "---------------------------------------------------------------------
  " Build WHERE clause for date filter
  "---------------------------------------------------------------------
  CLEAR lv_where.

  IF lv_check_field IS NOT INITIAL.
    IF iv_date_from IS NOT INITIAL AND iv_date_to IS NOT INITIAL.
      CONCATENATE lv_check_field ' >= ''' iv_date_from ''''
                  ' AND ' lv_check_field ' <= ''' iv_date_to ''''
                  INTO lv_where.
    ELSEIF iv_date_from IS NOT INITIAL.
      CONCATENATE lv_check_field ' >= ''' iv_date_from ''''
                  INTO lv_where.
    ELSEIF iv_date_to IS NOT INITIAL.
      CONCATENATE lv_check_field ' <= ''' iv_date_to ''''
                  INTO lv_where.
    ENDIF.
  ENDIF.

  "---------------------------------------------------------------------
  " Build file path
  "---------------------------------------------------------------------
  IF iv_file_path IS INITIAL.
    lv_file = '/usr/sap/tmp/'.
  ELSE.
    lv_file = iv_file_path.
  ENDIF.

  " Ensure trailing slash
  lv_file_len = strlen( lv_file ).
  IF lv_file_len > 0.
    lv_last_pos = lv_file_len - 1.
    IF lv_file+lv_last_pos(1) <> '/'.
      CONCATENATE lv_file '/' INTO lv_file.
    ENDIF.
  ELSE.
    lv_file = '/usr/sap/tmp/'.
  ENDIF.

  " Build filename: TABLE_YYYYMMDD_HHMMSS.csv
  GET TIME STAMP FIELD DATA(lv_ts).
  CONVERT TIME STAMP lv_ts TIME ZONE sy-zonlo INTO DATE DATA(lv_date) TIME DATA(lv_time).

  lv_date_str = lv_date.
  lv_time_str = lv_time.

  CONCATENATE lv_check_table '_' lv_date_str '_' lv_time_str '.csv'
              INTO lv_filename.
  CONCATENATE lv_file lv_filename INTO ev_file_name.

  "---------------------------------------------------------------------*
  " Get table metadata via RTTS for dynamic structure creation
  " AND use DDIF_NAMETAB_GET for flat field list (handles .INCLUDE)
  "---------------------------------------------------------------------*
  DATA: lo_struct_descr TYPE REF TO cl_abap_structdescr.

  SELECT SINGLE tabname FROM dd02l INTO @DATA(lv_exists)
    WHERE tabname = @lv_check_table
      AND as4local = 'A'.
  IF sy-subrc <> 0.
    ev_error = |Table { lv_check_table } does not exist in DDIC|.
    RETURN.
  ENDIF.

  TRY.
      lo_struct_descr ?= cl_abap_structdescr=>describe_by_name( lv_tabname ).
    CATCH cx_root.
      ev_error = 'Cannot describe table ' && lv_check_table.
      RETURN.
  ENDTRY.

  DATA: lt_nametab TYPE TABLE OF dfies,
        ls_nametab TYPE dfies.

  CALL FUNCTION 'DDIF_NAMETAB_GET'
    EXPORTING
      tabname   = lv_tabname
    TABLES
      dfies_tab = lt_nametab
    EXCEPTIONS
      OTHERS    = 1.

  IF sy-subrc <> 0.
    ev_error = |Cannot get nametab for { lv_check_table }|.
    RETURN.
  ENDIF.

  DELETE lt_nametab WHERE fieldname(1) = '.'.

  "---------------------------------------------------------------------
  " Determine which fields to export
  "---------------------------------------------------------------------
  DATA: lt_export_fields TYPE TABLE OF string,
        lv_all_fields    TYPE abap_bool,
        lv_fieldname     TYPE string.

  IF lv_fields = '*'.
    lv_all_fields = abap_true.
    LOOP AT lt_nametab INTO ls_nametab.
      APPEND ls_nametab-fieldname TO lt_export_fields.
    ENDLOOP.
  ELSE.
    lv_all_fields = abap_false.
    SPLIT lv_fields AT ',' INTO TABLE lt_export_fields.
  ENDIF.

  "---------------------------------------------------------------------*
  " Validate field names — only alphanumeric and underscore allowed
  "---------------------------------------------------------------------*
  LOOP AT lt_export_fields ASSIGNING FIELD-SYMBOL(<fs_export_field>).
    CONDENSE <fs_export_field> NO-GAPS.
    TRANSLATE <fs_export_field> TO UPPER CASE.
    IF NOT <fs_export_field> CO 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/'.
      ev_error = |Invalid field name (only A-Z, 0-9, _, / allowed): { <fs_export_field> }|.
      RETURN.
    ENDIF.
  ENDLOOP.

  "---------------------------------------------------------------------
  " Build CSV header line
  "---------------------------------------------------------------------
  CLEAR lv_header.
  LOOP AT lt_export_fields INTO lv_fieldname.
    CONDENSE lv_fieldname.
    IF lv_header IS INITIAL.
      lv_header = lv_fieldname.
    ELSE.
      CONCATENATE lv_header lv_fieldname INTO lv_header SEPARATED BY '|'.
    ENDIF.
  ENDLOOP.

  "---------------------------------------------------------------------
  " Open file for writing
  "---------------------------------------------------------------------
  TRY.
      OPEN DATASET ev_file_name FOR OUTPUT IN TEXT MODE ENCODING DEFAULT.
    CATCH cx_root INTO DATA(lo_cx_open).
      ev_error = 'Cannot open file: ' && lo_cx_open->get_text( ).
      RETURN.
  ENDTRY.

  "---------------------------------------------------------------------
  " Write header line
  "---------------------------------------------------------------------
  TRY.
      TRANSFER lv_header TO ev_file_name.
      lv_size = strlen( lv_header ) + 2.
    CATCH cx_root INTO lo_cx_open.
      ev_error = 'Cannot write header: ' && lo_cx_open->get_text( ).
      CLOSE DATASET ev_file_name.
      RETURN.
  ENDTRY.

  "---------------------------------------------------------------------
  " Build dynamic SELECT and fetch data in blocks
  "---------------------------------------------------------------------
  DATA: lo_table_descr TYPE REF TO cl_abap_tabledescr,
        lo_data_ref    TYPE REF TO data,
        lt_dynamic     TYPE REF TO data,
        ls_dynamic     TYPE REF TO data.

  lo_table_descr = cl_abap_tabledescr=>create( lo_struct_descr ).
  CREATE DATA lt_dynamic TYPE HANDLE lo_table_descr.
  CREATE DATA ls_dynamic TYPE HANDLE lo_struct_descr.

  FIELD-SYMBOLS: <ft_dynamic> TYPE STANDARD TABLE,
                 <fs_dynamic> TYPE ANY,
                 <ff_field>   TYPE ANY.

  ASSIGN lt_dynamic->* TO <ft_dynamic>.

  "---------------------------------------------------------------------*
  " Execute SELECT in blocks of 50000 rows for memory efficiency
  "---------------------------------------------------------------------*
  DATA: lv_block_size  TYPE i VALUE 50000,
        lv_total       TYPE i VALUE 0,
        lv_done        TYPE abap_bool VALUE abap_false.

  " Get primary key fields for keyset paging
  DATA: lt_pk_fields TYPE TABLE OF dfies,
        ls_pk_field  TYPE dfies,
        lv_pk_where  TYPE string,
        lv_last_key  TYPE string,
        lv_first_block TYPE abap_bool VALUE abap_true,
        lv_single_pass TYPE abap_bool VALUE abap_false.

  CALL FUNCTION 'DDIF_NAMETAB_GET'
    EXPORTING
      tabname   = lv_tabname
    TABLES
      dfies_tab = lt_pk_fields
    EXCEPTIONS
      OTHERS    = 1.

  DELETE lt_pk_fields WHERE fieldname(1) = '.'.
  DELETE lt_pk_fields WHERE keyflag <> 'X'.

  DATA: lv_keyset_field TYPE dfies.
  LOOP AT lt_pk_fields INTO ls_pk_field.
    IF ls_pk_field-fieldname <> 'MANDT'.
      lv_keyset_field = ls_pk_field.
      EXIT.
    ENDIF.
  ENDLOOP.

  DATA: lv_select_fields TYPE string.
  DATA: lv_keyset_added  TYPE abap_bool VALUE abap_false.
  IF lv_all_fields = abap_true.
    lv_select_fields = '*'.
  ELSE.
    lv_select_fields = lv_fields.
    IF lv_keyset_field-fieldname IS NOT INITIAL.
      READ TABLE lt_export_fields TRANSPORTING NO FIELDS
        WITH KEY table_line = lv_keyset_field-fieldname.
      IF sy-subrc <> 0.
        LOOP AT lt_export_fields INTO lv_fieldname.
          CONDENSE lv_fieldname.
          IF lv_fieldname = lv_keyset_field-fieldname.
            sy-subrc = 0.
            EXIT.
          ENDIF.
        ENDLOOP.
      ENDIF.
      IF sy-subrc <> 0.
        CONCATENATE lv_select_fields lv_keyset_field-fieldname
          INTO lv_select_fields SEPARATED BY ','.
        lv_keyset_added = abap_true.
      ENDIF.
    ENDIF.
  ENDIF.

  DATA: lv_orderby TYPE string.
  IF lines( lt_pk_fields ) > 0.
    LOOP AT lt_pk_fields INTO ls_pk_field.
      IF ls_pk_field-fieldname <> 'MANDT'.
        IF lv_orderby IS INITIAL.
          lv_orderby = ls_pk_field-fieldname.
        ELSE.
          CONCATENATE lv_orderby ls_pk_field-fieldname INTO lv_orderby SEPARATED BY ', '.
        ENDIF.
      ENDIF.
    ENDLOOP.
  ENDIF.

  IF lv_orderby IS INITIAL.
    READ TABLE lt_nametab INTO ls_nametab INDEX 1.
    IF sy-subrc = 0.
      lv_orderby = ls_nametab-fieldname.
    ENDIF.
  ENDIF.

  IF lv_orderby IS INITIAL.
    lv_orderby = 'PRIMARY KEY'.
  ENDIF.

  " M2 safeguard: If no non-MANDT PK field was found for keyset paging,
  " the WHERE clause cannot advance between iterations, causing an infinite
  " loop (same block fetched forever). Tables with only MANDT as PK are
  " not supported for keyset paging — fall back to single-pass mode.
  IF lv_keyset_field-fieldname IS INITIAL.
    lv_single_pass = abap_true.
  ENDIF.

  " Build field type map for conversion (before WHILE loop — static, no need to rebuild)
  " Use nametab (flat field list) instead of components to handle .INCLUDE structures
  DATA: lt_type_map TYPE TABLE OF abap_typekind,
        lv_type_kind   TYPE abap_typekind,
        lv_type_idx TYPE i.

  LOOP AT lt_export_fields INTO lv_fieldname.
    CONDENSE lv_fieldname.
    READ TABLE lt_nametab INTO ls_nametab
      WITH KEY fieldname = lv_fieldname.
    IF sy-subrc = 0.
      CASE ls_nametab-inttype.
        WHEN 'C' OR 'g'.
          lv_type_kind = cl_abap_structdescr=>typekind_char.
        WHEN 'N'.
          lv_type_kind = cl_abap_structdescr=>typekind_num.
        WHEN 'D'.
          lv_type_kind = cl_abap_structdescr=>typekind_date.
        WHEN 'T'.
          lv_type_kind = cl_abap_structdescr=>typekind_time.
        WHEN 'P'.
          lv_type_kind = cl_abap_structdescr=>typekind_packed.
        WHEN 'F'.
          lv_type_kind = cl_abap_structdescr=>typekind_float.
        WHEN 'I'.
          lv_type_kind = cl_abap_structdescr=>typekind_int.
        WHEN 's'.
          lv_type_kind = cl_abap_structdescr=>typekind_int2.
        WHEN 'b'.
          lv_type_kind = cl_abap_structdescr=>typekind_int1.
        WHEN 'X' OR 'y'.
          lv_type_kind = cl_abap_structdescr=>typekind_hex.
        WHEN OTHERS.
          lv_type_kind = cl_abap_structdescr=>typekind_char.
      ENDCASE.
      APPEND lv_type_kind TO lt_type_map.
    ELSE.
      APPEND cl_abap_structdescr=>typekind_char TO lt_type_map.
    ENDIF.
  ENDLOOP.

  WHILE lv_done = abap_false.

    CLEAR lv_pk_where.
    IF lv_first_block = abap_false AND lv_last_key IS NOT INITIAL AND lines( lt_pk_fields ) > 0.
      IF lv_keyset_field-fieldname IS NOT INITIAL.
        CONCATENATE lv_keyset_field-fieldname ' > ''' lv_last_key ''''
          INTO lv_pk_where.
        IF lv_where IS NOT INITIAL.
          CONCATENATE lv_where ' AND ' lv_pk_where
            INTO lv_pk_where.
        ENDIF.
      ELSE.
        lv_pk_where = lv_where.
      ENDIF.
    ELSE.
      lv_pk_where = lv_where.
    ENDIF.

    " Build and execute dynamic SELECT (ORDER BY strictly before INTO)
    TRY.
        IF lv_pk_where IS NOT INITIAL.
          IF iv_max_rows > 0.
            lv_max_fetch = iv_max_rows - lv_total.
            IF lv_max_fetch <= 0.
              lv_done = abap_true.
              EXIT.
            ENDIF.
            IF lv_max_fetch > lv_block_size.
              lv_max_fetch = lv_block_size.
            ENDIF.

            SELECT (lv_select_fields) FROM (lv_check_table)
              WHERE (lv_pk_where)
              ORDER BY (lv_orderby)
              INTO CORRESPONDING FIELDS OF TABLE @<ft_dynamic>
              UP TO @lv_max_fetch ROWS.
          ELSE.
            SELECT (lv_select_fields) FROM (lv_check_table)
              WHERE (lv_pk_where)
              ORDER BY (lv_orderby)
              INTO CORRESPONDING FIELDS OF TABLE @<ft_dynamic>
              UP TO @lv_block_size ROWS.
          ENDIF.
        ELSE.
          IF iv_max_rows > 0.
            lv_max_fetch = iv_max_rows - lv_total.
            IF lv_max_fetch <= 0.
              lv_done = abap_true.
              EXIT.
            ENDIF.
            IF lv_max_fetch > lv_block_size.
              lv_max_fetch = lv_block_size.
            ENDIF.

            SELECT (lv_select_fields) FROM (lv_check_table)
              ORDER BY (lv_orderby)
              INTO CORRESPONDING FIELDS OF TABLE @<ft_dynamic>
              UP TO @lv_max_fetch ROWS.
          ELSE.
            SELECT (lv_select_fields) FROM (lv_check_table)
              ORDER BY (lv_orderby)
              INTO CORRESPONDING FIELDS OF TABLE @<ft_dynamic>
              UP TO @lv_block_size ROWS.
          ENDIF.
        ENDIF.

      CATCH cx_sy_dynamic_osql_error INTO DATA(lo_sql_err).
        ev_error = 'SQL error: ' && lo_sql_err->get_text( ).
        CLOSE DATASET ev_file_name.
        RETURN.
      CATCH cx_root INTO DATA(lo_cx_sel).
        ev_error = 'SELECT error: ' && lo_cx_sel->get_text( ).
        CLOSE DATASET ev_file_name.
        RETURN.
    ENDTRY.

    IF lines( <ft_dynamic> ) = 0.
      lv_done = abap_true.
      EXIT.
    ENDIF.

    "-------------------------------------------------------------------*
    " Write rows to CSV with type-aware conversion
    "-------------------------------------------------------------------*
    LOOP AT <ft_dynamic> ASSIGNING <fs_dynamic>.
      CLEAR lv_row.
      lv_type_idx = 0.

      LOOP AT lt_export_fields INTO lv_fieldname.
        CONDENSE lv_fieldname.
        lv_type_idx = lv_type_idx + 1.

        ASSIGN COMPONENT lv_fieldname OF STRUCTURE <fs_dynamic> TO <ff_field>.
        IF sy-subrc = 0.
          READ TABLE lt_type_map INTO lv_type_kind INDEX lv_type_idx.
          IF sy-subrc <> 0.
            lv_type_kind = cl_abap_structdescr=>typekind_char.
          ENDIF.

          CLEAR lv_char_val.

          CASE lv_type_kind.
            WHEN cl_abap_structdescr=>typekind_date.
              IF <ff_field> IS NOT INITIAL.
                lv_date_str = <ff_field>.
                IF strlen( lv_date_str ) = 8.
                  CONCATENATE lv_date_str(4) '-' lv_date_str+4(2) '-' lv_date_str+6(2)
                    INTO lv_char_val.
                ELSE.
                  lv_char_val = lv_date_str.
                ENDIF.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_time.
              IF <ff_field> IS NOT INITIAL.
                lv_time_str = <ff_field>.
                IF strlen( lv_time_str ) = 6.
                  CONCATENATE lv_time_str(2) ':' lv_time_str+2(2) ':' lv_time_str+4(2)
                    INTO lv_char_val.
                ELSE.
                  lv_char_val = lv_time_str.
                ENDIF.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_packed.
              IF <ff_field> IS NOT INITIAL.
                WRITE <ff_field> TO lv_num_buffer NO-GROUPING NO-SIGN.
                lv_char_val = lv_num_buffer.
                IF <ff_field> < 0.
                  CONCATENATE '-' lv_char_val INTO lv_char_val.
                ENDIF.
                REPLACE ALL OCCURRENCES OF ',' IN lv_char_val WITH '.'.
                CONDENSE lv_char_val NO-GAPS.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_int
              OR cl_abap_structdescr=>typekind_int2
              OR cl_abap_structdescr=>typekind_int1.
              lv_char_val = |{ <ff_field> }|.
              CONDENSE lv_char_val.

            WHEN cl_abap_structdescr=>typekind_float.
              IF <ff_field> IS NOT INITIAL.
                WRITE <ff_field> TO lv_num_buffer NO-GROUPING NO-SIGN.
                lv_char_val = lv_num_buffer.
                IF <ff_field> < 0.
                  CONCATENATE '-' lv_char_val INTO lv_char_val.
                ENDIF.
                REPLACE ALL OCCURRENCES OF ',' IN lv_char_val WITH '.'.
                CONDENSE lv_char_val NO-GAPS.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_hex.
              DATA(lv_hex_str) = |{ <ff_field> }|.
              CONCATENATE '0x' lv_hex_str INTO lv_char_val.

            WHEN cl_abap_structdescr=>typekind_char
              OR cl_abap_structdescr=>typekind_string.
              " CHAR/STRING — as-is, but remove trailing spaces
              lv_char_val = |{ <ff_field> }|.
              " Don't CONDENSE — preserves internal spaces, only removes trailing
              SHIFT lv_char_val RIGHT DELETING TRAILING space.

            WHEN OTHERS.
              lv_char_val = |{ <ff_field> }|.
              CONDENSE lv_char_val.
          ENDCASE.

          IF lv_row IS INITIAL.
            lv_row = lv_char_val.
          ELSE.
            CONCATENATE lv_row lv_char_val INTO lv_row SEPARATED BY '|'.
          ENDIF.
        ELSE.
          IF lv_row IS INITIAL.
            lv_row = ''.
          ELSE.
            CONCATENATE lv_row '' INTO lv_row SEPARATED BY '|'.
          ENDIF.
        ENDIF.
      ENDLOOP.

      TRY.
          TRANSFER lv_row TO ev_file_name.
          lv_size = lv_size + strlen( lv_row ) + 2.
          lv_total = lv_total + 1.
        CATCH cx_root INTO lo_cx_open.
          ev_error = 'Cannot write row: ' && lo_cx_open->get_text( ).
          CLOSE DATASET ev_file_name.
          RETURN.
      ENDTRY.
    ENDLOOP.

    IF lines( <ft_dynamic> ) < lv_block_size.
      lv_done = abap_true.
    ENDIF.

    " M2 safeguard: single-pass mode for tables with no keyset field
    " (only MANDT PK) — can't do keyset paging, so stop after first block
    IF lv_done = abap_false AND lv_single_pass = abap_true.
      lv_done = abap_true.
    ENDIF.

    " Remember last key for keyset paging (first non-MANDT PK field)
    " lv_keyset_field was determined before the loop; reuse it here
    IF lv_done = abap_false AND lv_keyset_field-fieldname IS NOT INITIAL.
      DESCRIBE TABLE <ft_dynamic> LINES DATA(lv_line_count).
      READ TABLE <ft_dynamic> ASSIGNING <fs_dynamic> INDEX lv_line_count.
      IF sy-subrc = 0.
        ASSIGN COMPONENT lv_keyset_field-fieldname OF STRUCTURE <fs_dynamic> TO <ff_field>.
        IF sy-subrc = 0.
          lv_last_key = |{ <ff_field> }|.
          CONDENSE lv_last_key.
          REPLACE ALL OCCURRENCES OF '''' IN lv_last_key WITH ''''''.
        ENDIF.
      ENDIF.
    ENDIF.

    lv_first_block = abap_false.

    IF iv_max_rows > 0 AND lv_total >= iv_max_rows.
      lv_done = abap_true.
    ENDIF.

    CLEAR <ft_dynamic>.

  ENDWHILE.

  "---------------------------------------------------------------------
  " Close file
  "---------------------------------------------------------------------
  CLOSE DATASET ev_file_name.

  "---------------------------------------------------------------------
  " Set return values
  "---------------------------------------------------------------------
  ev_row_count = lv_total.
  ev_file_size = lv_size.

ENDFUNCTION.
