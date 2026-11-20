# Python-KontAKTMappeToPDF

Converts a file that a caseworker uploaded into a **mappe** in **KontAKT** (the aktindsigt / FOI request system) to PDF, so it can be screened for personal data and redacted like every document fetched from F2 or GO.

KontAKT queues one element per file, when a Word document, spreadsheet, presentation, image or e-mail is uploaded into a mappe (`app/mappe_pdf.py` in KontAKT decides which file types). PDFs never come here, and neither do videos, archives or unknown file types: those are delivered as they are.

## What it does

For one uploaded file:

1. Downloads it from KontAKT's file store (`GET /api/v1/cases/{id}/documents/{doc_id}/content`).
2. Converts it with the shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`oomtm.pdf.convert_to_pdf`): LibreOffice for Office and text, Pillow for images, HTML rendering for e-mails. The same engine KontAKTGOToPDF falls back to.
3. Uploads the PDF onto the **same** document (`POST .../store?kind=pdf`), which records name, size, SHA-256 and status `ready`.

The uploaded original is only replaced once there is a real PDF (`%PDF-`). If the file can't be converted, the robot reports `skipped` with a reason to `POST .../file`, and KontAKT keeps the original and delivers it as it is. Nothing can be lost here; at worst the file isn't screened.

Two cases are deliberately **not** converted, because the PDF would contain less than the original:

- **E-mails with attachments.** `oomtm.pdf` only lists the attachment names. Inline images (signature logos, pasted screenshots) don't count, since they are part of the mail's HTML.
- **Multi-page TIFF scans** are converted page by page here, because `oomtm.pdf.image_to_pdf` only keeps the first page.

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

`oomtm[pdf]` (pinned to a commit), `requests`, `Pillow` (also needed by `error_screenshot.py`). LibreOffice is unpacked on the worker without admin rights on the first run (~350 MB, once per machine).
