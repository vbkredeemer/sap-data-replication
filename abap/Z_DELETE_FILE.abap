*---------------------------------------------------------------------*
* Funktionsbaustein: Z_DELETE_FILE
* Zweck: Löscht eine Datei auf dem SAP-Server-Dateisystem.
*        Wird vom Python-Client aufgerufen nach erfolgreichem Import.
*---------------------------------------------------------------------*
* IMPORTING:
*   IV_FILE_PATH   TYPE STRING     - Vollständiger Dateipfad
*
* EXPORTING:
*   EV_ERROR       TYPE STRING     - Fehlermeldung
*---------------------------------------------------------------------*

FUNCTION Z_DELETE_FILE.
*"----------------------------------------------------------------------
*"*"Lokale Schnittstelle:
*"  IMPORTING
*"     VALUE(IV_FILE_PATH) TYPE  STRING
*"  EXPORTING
*"     VALUE(EV_ERROR) TYPE  STRING
*"----------------------------------------------------------------------

  CLEAR ev_error.

  IF iv_file_path IS INITIAL.
    ev_error = 'IV_FILE_PATH is empty'.
    RETURN.
  ENDIF.

  TRY.
      DELETE DATASET iv_file_path.
      IF sy-subrc <> 0.
        ev_error = 'File not found or cannot delete: ' && iv_file_path.
      ENDIF.
    CATCH cx_root INTO DATA(lo_cx).
      ev_error = 'Error deleting file: ' && lo_cx->get_text( ).
  ENDTRY.

ENDFUNCTION.