# Python-KontAKTMappeToPDF

Converts a file that a caseworker uploaded into a **mappe** in **KontAKT** (the aktindsigt / FOI request system) to PDF, so it can be screened for personal data and redacted like every document fetched from F2 or GO.

KontAKT queues one element per file, when a Word document, spreadsheet, presentation, image or e-mail is uploaded into a mappe (`app/mappe_pdf.py` in KontAKT decides which file types). PDFs never come here, and neither do videos, archives or unknown file types: those are delivered as they are.

## What it does

For one uploaded file:

1. Downloads it from KontAKT's file store (`GET /api/v1/cases/{id}/documents/{doc_id}/content`).
2. Converts it with the shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`oomtm.convert`), the same way every KontAKT PDF robot does: WeasyPrint for e-mails, HTML, text and CSV, LibreOffice for Office files, Pillow for images (every page of a multi-page TIFF).
3. Uploads the PDF onto the **same** document (`POST .../store?kind=pdf`), which records name, size, SHA-256, status `ready` and a note for the caseworker when the PDF doesn't show everything.

The uploaded original is only replaced once there is a real PDF. If the file can't be converted, the robot reports `skipped` with oomtm's reason to `POST .../file`, and KontAKT keeps the original and delivers it as it is. Nothing can be lost here; at worst the file isn't screened.

**E-mails with attachments** become a PDF of the mail itself: header, text and the images inside the text (signature logos, pasted screenshots). The real attachments aren't in the PDF; they're listed in the mail's header as "Vedhæftet", and the note on the document asks the caseworker to add them to the mappe as separate files. Notes also carry oomtm's warnings about what an Office file has that the PDF doesn't show (tracked changes, comments, hidden sheets).

## Input (one file)

| Field | Meaning |
|-------|---------|
| `kontakt_case_id` | KontAKT case id |
| `doc_id` | KontAKT document id (the store is addressed by this id) |
| `source_case_id` | The mappe's name (logging only) |
| `dok_id` | KontAKT's own document number (`UPL-n`) |
| `akt_id` | Act number in the mappe |
| `filnavn` | The stored file name. Its extension picks the conversion |
| `title` | Document title |

## Required configuration

- Credential `KontAKTAPI`: username = KontAKT's base URL, password = API key (the same credential the other KontAKT robots use)
- OO queue trigger on `KontAKTMappeToPDF`

## Dependencies

`oomtm[pdf]` (pinned to a commit), `requests`, `Pillow` (also needed by `error_screenshot.py`). On the first run, `reset.open_all` makes oomtm install its engines without admin rights, once per machine: LibreOffice (~350 MB) and WeasyPrint's Windows libraries (~30 MB), both from [oomtm_setup](https://github.com/mtm-aarhus/oomtm_setup) first.
