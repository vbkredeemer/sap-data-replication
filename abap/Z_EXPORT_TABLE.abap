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
*   IV_TABLE       TYPE TABNAME    - SAP-Quelltabelle (z.B. 'MARA')
*   IV_DATE_FIELD  TYPE STRING     - Datumsfeld für Filter (z.B. 'AEDAT'), optional
*   IV_DATE_FROM   TYPE DATUM      - Von-Datum (YYYYMMDD), optional
*   IV_DATE_TO     TYPE DATUM      - Bis-Datum (YYYYMMDD), optional
*   IV_FIELDS      TYPE STRING     - Feldliste komma-separiert (z.B. 'MATNR,MTART'), '*' = alle
*   IV_MAX_ROWS    TYPE I          - Max. Zeilen (0 = alle)
*   IV_FILE_PATH   TYPE STRING     - Verzeichnis für CSV (z.B. '/usr/sap/trans/data/')
*                                    Wenn leer: Default-Verzeichnis wird verwendet
*
* EXPORTING:
*   EV_FILE_NAME   TYPE STRING     - Vollständiger Dateipfad der erzeugten CSV
*   EV_ROW_COUNT   TYPE I          - Anzahl geschriebener Zeilen
*   EV_FILE_SIZE   TYPE I          - Dateigröße in Bytes (approx.)
*   EV_ERROR       TYPE STRING     - Fehlermeldung
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
*"     VALUE(IV_DATE_FIELD) TYPE  STRING OPTIONAL
*"     VALUE(IV_DATE_FROM) TYPE  DATUM OPTIONAL
*"     VALUE(IV_DATE_TO) TYPE  DATUM OPTIONAL
*"     VALUE(IV_FIELDS) TYPE  STRING OPTIONAL
*"     VALUE(IV_MAX_ROWS) TYPE  I DEFAULT 0
*"     VALUE(IV_FILE_PATH) TYPE  STRING OPTIONAL
*"  EXPORTING
*"     VALUE(EV_FILE_NAME) TYPE  STRING
*"     VALUE(EV_ROW_COUNT) TYPE  I
*"     VALUE(EV_FILE_SIZE) TYPE  I
*"     VALUE(EV_ERROR) TYPE  STRING
*"----------------------------------------------------------------------

  DATA: lv_select       TYPE string,
        lv_where        TYPE string,
        lv_fields       TYPE string,
        lv_file         TYPE string,
        lv_filename     TYPE string,
        lv_timestamp    TYPE string,
        lv_header       TYPE string,
        lv_row          TYPE string,
        lv_char_val     TYPE string,
        lv_count        TYPE i,
        lv_size         TYPE i.

  CLEAR: ev_error, ev_file_name, ev_row_count, ev_file_size.

  *---------------------------------------------------------------------
  * Validate inputs
  *---------------------------------------------------------------------
  IF iv_table IS INITIAL.
    ev_error = 'IV_TABLE is empty'.
    RETURN.
  ENDIF.

  *---------------------------------------------------------------------
  * Build field list
  *---------------------------------------------------------------------
  IF iv_fields IS INITIAL OR iv_fields = '*' OR iv_fields = ''.
    lv_fields = '*'.
  ELSE.
    lv_fields = iv_fields.
  ENDIF.

  *---------------------------------------------------------------------
  * Validate date field name if provided
  IF iv_date_field IS NOT INITIAL.
    DATA: lv_field_found TYPE i.
    FIND REGEX '[^A-Za-z0-9_]' IN iv_date_field MATCH COUNT lv_field_found.
    IF lv_field_found > 0.
      ev_error = 'Invalid date field name: ' && iv_date_field.
      RETURN.
    ENDIF.
  ENDIF.

  * Build WHERE clause for date filter
  *---------------------------------------------------------------------
  CLEAR lv_where.

  IF iv_date_field IS NOT INITIAL.
    IF iv_date_from IS NOT INITIAL AND iv_date_to IS NOT INITIAL.
      CONCATENATE iv_date_field ' >= ''' iv_date_from ''''
                  ' AND ' iv_date_field ' <= ''' iv_date_to ''''
                  INTO lv_where.
    ELSEIF iv_date_from IS NOT INITIAL.
      CONCATENATE iv_date_field ' >= ''' iv_date_from ''''
                  INTO lv_where.
    ELSEIF iv_date_to IS NOT INITIAL.
      CONCATENATE iv_date_field ' <= ''' iv_date_to ''''
                  INTO lv_where.
    ENDIF.
  ENDIF.

  *---------------------------------------------------------------------
  * Build file path
  *---------------------------------------------------------------------
  IF iv_file_path IS INITIAL.
    * Default: SAP temp directory
    lv_file = '/usr/sap/tmp/'.
  ELSE.
    lv_file = iv_file_path.
  ENDIF.

  * Ensure trailing slash
  DATA: lv_file_len TYPE i.
  lv_file_len = strlen( lv_file ).
  IF lv_file_len > 0 AND lv_file+lv_file_len-1(1) <> '/'.
    CONCATENATE lv_file '/' INTO lv_file.
  ELSEIF lv_file_len = 0.
    lv_file = '/usr/sap/tmp/'.
  ENDIF.

  * Build filename: TABLE_YYYYMMDD_HHMMSS.csv
  GET TIME STAMP FIELD DATA(lv_ts).
  CONVERT TIME STAMP lv_ts TIME ZONE sy-zonlo INTO DATE DATA(lv_date) TIME DATA(lv_time).

  DATA: lv_date_str TYPE c LENGTH 8,
        lv_time_str TYPE c LENGTH 6.
  WRITE lv_date TO lv_date_str YYYYMMDD.
  WRITE lv_time TO lv_time_str HHMMSS.

  CONCATENATE iv_table '_' lv_date_str '_' lv_time_str '.csv'
              INTO lv_filename.
  CONCATENATE lv_file lv_filename INTO ev_file_name.

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
  * Determine which fields to export
  *---------------------------------------------------------------------
  DATA: lt_export_fields TYPE TABLE OF string,
        lv_all_fields    TYPE abap_bool,
        lv_fieldname     TYPE string.

  IF lv_fields = '*'.
    lv_all_fields = abap_true.
    LOOP AT lt_components INTO ls_component.
      APPEND ls_component-name TO lt_export_fields.
    ENDLOOP.
  ELSE.
    lv_all_fields = abap_false.
    SPLIT lv_fields AT ',' INTO TABLE lt_export_fields.
  ENDIF.

  *---------------------------------------------------------------------
  * Build CSV header line
  *---------------------------------------------------------------------
  CLEAR lv_header.
  LOOP AT lt_export_fields INTO lv_fieldname.
    CONDENSE lv_fieldname.
    IF lv_header IS INITIAL.
      lv_header = lv_fieldname.
    ELSE.
      CONCATENATE lv_header lv_fieldname INTO lv_header SEPARATED BY '|'.
    ENDIF.
  ENDLOOP.

  *---------------------------------------------------------------------
  * Open file for writing
  *---------------------------------------------------------------------
  TRY.
      OPEN DATASET ev_file_name FOR OUTPUT IN TEXT MODE ENCODING DEFAULT.
    CATCH cx_root INTO DATA(lo_cx_open).
      ev_error = 'Cannot open file: ' && lo_cx_open->get_text( ).
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------
  * Write header line
  *---------------------------------------------------------------------
  TRY.
      TRANSFER lv_header TO ev_file_name.
      lv_size = strlen( lv_header ) + 2.
    CATCH cx_root INTO lo_cx_open.
      ev_error = 'Cannot write header: ' && lo_cx_open->get_text( ).
      CLOSE DATASET ev_file_name.
      RETURN.
  ENDTRY.

  *---------------------------------------------------------------------
  * Build dynamic SELECT and fetch data in blocks
  *---------------------------------------------------------------------
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

  *---------------------------------------------------------------------*
  * Execute SELECT in blocks of 50000 rows for memory efficiency
  * Uses keyset paging: after each block, remember the last key value
  * and fetch the next block with WHERE key > last_key
  *---------------------------------------------------------------------*
  DATA: lv_block_size TYPE i VALUE 50000,
        lv_total       TYPE i VALUE 0,
        lv_done        TYPE abap_bool VALUE abap_false.

  * Get primary key fields for keyset paging
  DATA: lt_pk_fields TYPE TABLE OF dfies,
        ls_pk_field  TYPE dfies,
        lv_pk_where  TYPE string,
        lv_last_key  TYPE string,
        lv_first_block TYPE abap_bool VALUE abap_true.

  CALL FUNCTION 'DDIF_NAMETAB_GET'
    EXPORTING
      tabname   = iv_table
    TABLES
      dfies_tab = lt_pk_fields
    EXCEPTIONS
      OTHERS    = 1.

  DELETE lt_pk_fields WHERE keyflag <> 'X'.

  * Build field list for SELECT (same as export fields, comma-separated)
  DATA: lv_select_fields TYPE string.
  IF lv_all_fields = abap_true.
    lv_select_fields = '*'.
  ELSE.
    lv_select_fields = lv_fields.
  ENDIF.

  * Build ORDER BY clause for keyset paging (first non-MANDT PK field)
  DATA: lv_orderby TYPE string.
  IF lines( lt_pk_fields ) > 0.
    * Skip MANDT — use first non-client PK field for keyset paging
    LOOP AT lt_pk_fields INTO ls_pk_field.
      IF ls_pk_field-fieldname <> 'MANDT'.
        lv_orderby = ls_pk_field-fieldname.
        EXIT.
      ENDIF.
    ENDLOOP.
  ENDIF.

  * If no PK found (or only MANDT), use first field from components as fallback
  IF lv_orderby IS INITIAL.
    READ TABLE lt_components INTO ls_component INDEX 1.
    IF sy-subrc = 0.
      lv_orderby = ls_component-name.
    ENDIF.
  ENDIF.

  WHILE lv_done = abap_false.

    * Build keyset WHERE clause: use first non-MANDT PK field for keyset paging
    CLEAR lv_pk_where.
    IF lv_first_block = abap_false AND lv_last_key IS NOT INITIAL AND lines( lt_pk_fields ) > 0.
      * Find first non-MANDT PK field for keyset paging
      DATA: lv_keyset_field TYPE dfies.
      LOOP AT lt_pk_fields INTO ls_pk_field.
        IF ls_pk_field-fieldname <> 'MANDT'.
          lv_keyset_field = ls_pk_field.
          EXIT.
        ENDIF.
      ENDLOOP.
      IF lv_keyset_field-fieldname IS NOT INITIAL.
        CONCATENATE lv_keyset_field-fieldname ' > ''' lv_last_key ''''
          INTO lv_pk_where.
        IF lv_where IS NOT INITIAL.
          CONCATENATE lv_where ' AND ' lv_pk_where
            INTO lv_pk_where SEPARATED BY space.
        ENDIF.
      ELSE.
        lv_pk_where = lv_where.
      ENDIF.
    ELSE.
      lv_pk_where = lv_where.
    ENDIF.

    * Build and execute dynamic SELECT
    TRY.
        IF lv_pk_where IS NOT INITIAL.
          IF iv_max_rows > 0.
            DATA: lv_max_fetch TYPE i.
            lv_max_fetch = iv_max_rows - lv_total.
            IF lv_max_fetch <= 0.
              lv_done = abap_true.
              EXIT.
            ENDIF.
            IF lv_max_fetch > lv_block_size.
              lv_max_fetch = lv_block_size.
            ENDIF.

            SELECT (lv_select_fields) FROM (iv_table)
              WHERE (lv_pk_where)
              INTO TABLE <ft_dynamic> UP TO lv_max_fetch ROWS
              ORDER BY (lv_orderby).
          ELSE.
            SELECT (lv_select_fields) FROM (iv_table)
              WHERE (lv_pk_where)
              INTO TABLE <ft_dynamic> UP TO lv_block_size ROWS
              ORDER BY (lv_orderby).
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

            SELECT (lv_select_fields) FROM (iv_table)
              INTO TABLE <ft_dynamic> UP TO lv_max_fetch ROWS
              ORDER BY (lv_orderby).
          ELSE.
            SELECT (lv_select_fields) FROM (iv_table)
              INTO TABLE <ft_dynamic> UP TO lv_block_size ROWS
              ORDER BY (lv_orderby).
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

    * Check if we got any data
    IF lines( <ft_dynamic> ) = 0.
      lv_done = abap_true.
      EXIT.
    ENDIF.

    *-------------------------------------------------------------------*
    * Write rows to CSV with type-aware conversion
    *-------------------------------------------------------------------*
    * Build field type map for conversion
    DATA: lt_type_map TYPE TABLE OF i,  " stores type_kind per field
          lv_type_kind   TYPE abap_typekind,
          lv_type_idx TYPE i.

    LOOP AT lt_export_fields INTO lv_fieldname.
      CONDENSE lv_fieldname.
      READ TABLE lt_components INTO ls_component
        WITH KEY name = lv_fieldname.
      IF sy-subrc = 0.
        APPEND ls_component-type_kind TO lt_type_map.
      ELSE.
        APPEND cl_abap_structdescr=>typekind_char TO lt_type_map.
      ENDIF.
    ENDLOOP.

    LOOP AT <ft_dynamic> ASSIGNING <fs_dynamic>.
      CLEAR lv_row.
      lv_type_idx = 0.

      LOOP AT lt_export_fields INTO lv_fieldname.
        CONDENSE lv_fieldname.
        lv_type_idx = lv_type_idx + 1.
        ASSIGN COMPONENT lv_fieldname OF STRUCTURE <fs_dynamic> TO <ff_field>.
        IF sy-subrc = 0.
          * Get type kind for this field
          READ TABLE lt_type_map INTO lv_type_kind INDEX lv_type_idx.
          IF sy-subrc <> 0.
            lv_type_kind = cl_abap_structdescr=>typekind_char.
          ENDIF.

          * Type-aware conversion to MSSQL-compatible format
          CLEAR lv_char_val.

          CASE lv_type_kind.
            WHEN cl_abap_structdescr=>typekind_date.
              * SAP DATE: YYYYMMDD → MSSQL: YYYY-MM-DD
              IF <ff_field> IS NOT INITIAL.
                lv_date_str = |{ <ff_field> }|.
                IF strlen( lv_date_str ) = 8.
                  CONCATENATE lv_date_str(4) '-' lv_date_str+4(2) '-' lv_date_str+6(2)
                    INTO lv_char_val.
                ELSE.
                  lv_char_val = lv_date_str.
                ENDIF.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_time.
              * SAP TIME: HHMMSS → MSSQL: HH:MM:SS
              IF <ff_field> IS NOT INITIAL.
                lv_time_str = |{ <ff_field> }|.
                IF strlen( lv_time_str ) = 6.
                  CONCATENATE lv_time_str(2) ':' lv_time_str+2(2) ':' lv_time_str+4(2)
                    INTO lv_char_val.
                ELSE.
                  lv_char_val = lv_time_str.
                ENDIF.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_packed.
              * SAP PACKED: convert to decimal with dot, no thousand separators
              IF <ff_field> IS NOT INITIAL.
                * Write with WRITE and EDIT MASK to get decimal notation
                WRITE <ff_field> TO lv_char_val NO-GROUPING.
                * Replace comma with dot (if German locale)
                REPLACE ALL OCCURRENCES OF ',' IN lv_char_val WITH '.'.
                CONDENSE lv_char_val.
                * Remove leading spaces
                SHIFT lv_char_val LEFT DELETING LEADING SPACE.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_int
              OR cl_abap_structdescr=>typekind_int2
              OR cl_abap_structdescr=>typekind_int1.
              * Integer — plain number
              lv_char_val = |{ <ff_field> }|.
              CONDENSE lv_char_val.

            WHEN cl_abap_structdescr=>typekind_float.
              * FLOAT — use scientific or decimal notation with dot
              IF <ff_field> IS NOT INITIAL.
                WRITE <ff_field> TO lv_char_val NO-GROUPING.
                REPLACE ALL OCCURRENCES OF ',' IN lv_char_val WITH '.'.
                CONDENSE lv_char_val.
                SHIFT lv_char_val LEFT DELETING LEADING SPACE.
              ENDIF.

            WHEN cl_abap_structdescr=>typekind_hex.
              * RAW — convert to hex string with 0x prefix for MSSQL VARBINARY
              DATA(lv_hex_str) = |{ <ff_field> }|.
              CONCATENATE '0x' lv_hex_str INTO lv_char_val.

            WHEN cl_abap_structdescr=>typekind_char
              OR cl_abap_structdescr=>typekind_string.
              * CHAR/STRING — as-is, but remove trailing spaces
              lv_char_val = |{ <ff_field> }|.
              * Don't CONDENSE — preserves internal spaces, only removes trailing
              SHIFT lv_char_val RIGHT DELETING TRAILING space.
              SHIFT lv_char_val LEFT DELETING LEADING space.

            WHEN OTHERS.
              * Fallback: plain string conversion
              lv_char_val = |{ <ff_field> }|.
              CONDENSE lv_char_val.
          ENDCASE.

          IF lv_row IS INITIAL.
            lv_row = lv_char_val.
          ELSE.
            CONCATENATE lv_row lv_char_val INTO lv_row SEPARATED BY '|'.
          ENDIF.
        ELSE.
          * Field not found — empty value
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

    * Check if we fetched less than block size — means we're done
    IF lines( <ft_dynamic> ) < lv_block_size.
      lv_done = abap_true.
    ENDIF.

    * Remember last key for keyset paging (first non-MANDT PK field)
    IF lv_done = abap_false AND lines( lt_pk_fields ) > 0.
      DATA: lv_keyset_track TYPE dfies.
      LOOP AT lt_pk_fields INTO ls_pk_field.
        IF ls_pk_field-fieldname <> 'MANDT'.
          lv_keyset_track = ls_pk_field.
          EXIT.
        ENDIF.
      ENDLOOP.
      IF lv_keyset_track-fieldname IS NOT INITIAL.
        DESCRIBE TABLE <ft_dynamic> LINES DATA(lv_line_count).
        READ TABLE <ft_dynamic> ASSIGNING <fs_dynamic> INDEX lv_line_count.
        IF sy-subrc = 0.
          ASSIGN COMPONENT lv_keyset_track-fieldname OF STRUCTURE <fs_dynamic> TO <ff_field>.
          IF sy-subrc = 0.
            lv_last_key = |{ <ff_field> }|.
            CONDENSE lv_last_key.
            * Escape single quotes for next iteration's WHERE clause
            REPLACE ALL OCCURRENCES OF '''' IN lv_last_key WITH ''''''.
          ENDIF.
        ENDIF.
      ENDIF.
    ENDIF.

    lv_first_block = abap_false.

    * Check max rows
    IF iv_max_rows > 0 AND lv_total >= iv_max_rows.
      lv_done = abap_true.
    ENDIF.

    * Clear table for next block
    CLEAR <ft_dynamic>.

  ENDWHILE.

  *---------------------------------------------------------------------
  * Close file
  *---------------------------------------------------------------------
  CLOSE DATASET ev_file_name.

  *---------------------------------------------------------------------
  * Set return values
  *---------------------------------------------------------------------
  ev_row_count = lv_total.
  ev_file_size = lv_size.

ENDFUNCTION.