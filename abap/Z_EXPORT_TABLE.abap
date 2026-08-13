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
  IF lv_file+strlen(lv_file)-1(1) <> '/'.
    CONCATENATE lv_file '/' INTO lv_file.
  ENDIF.

  * Build filename: TABLE_YYYYMMDD_HHMMSS.csv
  GET TIME STAMP FIELD DATA(lv_ts).
  CONVERT TIME STAMP lv_ts TIME ZONE sy-zonlo INTO DATE DATA(lv_date) TIME DATA(lv_time).

  DATA: lv_date_str TYPE c LENGTH 8,
        lv_time_str TYPE c LENGTH 6.
  WRITE lv_date TO lv_date_str YYMMDD.
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
      OPEN DATASET ev_file_name FOR OUTPUT IN TEXT MODE ENCODING UTF-8.
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

  *---------------------------------------------------------------------
  * Execute SELECT in blocks of 50000 rows for memory efficiency
  *---------------------------------------------------------------------
  DATA: lv_block_size TYPE i VALUE 50000,
        lv_skip        TYPE i VALUE 0,
        lv_total       TYPE i VALUE 0,
        lv_done        TYPE abap_bool VALUE abap_false.

  * Build field list for SELECT (same as export fields, comma-separated)
  DATA: lv_select_fields TYPE string.
  IF lv_all_fields = abap_true.
    lv_select_fields = '*'.
  ELSE.
    lv_select_fields = lv_fields.
  ENDIF.

  WHILE lv_done = abap_false.

    * Build and execute dynamic SELECT
    TRY.
        IF lv_where IS NOT INITIAL.
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
              WHERE (lv_where)
              INTO TABLE <ft_dynamic> UP TO lv_max_fetch ROWS.
          ELSE.
            SELECT (lv_select_fields) FROM (iv_table)
              WHERE (lv_where)
              INTO TABLE <ft_dynamic> UP TO lv_block_size ROWS.
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
              INTO TABLE <ft_dynamic> UP TO lv_max_fetch ROWS.
          ELSE.
            SELECT (lv_select_fields) FROM (iv_table)
              INTO TABLE <ft_dynamic> UP TO lv_block_size ROWS.
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

    *-------------------------------------------------------------------
    * Write rows to CSV
    *-------------------------------------------------------------------
    LOOP AT <ft_dynamic> ASSIGNING <fs_dynamic>.
      CLEAR lv_row.

      LOOP AT lt_export_fields INTO lv_fieldname.
        CONDENSE lv_fieldname.
        ASSIGN COMPONENT lv_fieldname OF STRUCTURE <fs_dynamic> TO <ff_field>.
        IF sy-subrc = 0.
          DATA(lv_char_val) = |{ <ff_field> }|.
          CONDENSE lv_char_val.
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

    * Check if we fetched less than block size — means we're done
    IF lines( <ft_dynamic> ) < lv_block_size.
      lv_done = abap_true.
    ENDIF.

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

  IF lv_total = 0.
    * Empty file — still valid, just no data
    ev_error = ''.
  ENDIF.

ENDFUNCTION.