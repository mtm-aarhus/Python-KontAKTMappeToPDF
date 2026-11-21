"""KontAKT: lav en fil i en mappe om til PDF.

Queue-driven, ét køelement pr. fil. En sagsbehandler har lagt en fil i en mappe
i KontAKT (en sag, KontAKT selv ejer - der er ingen F2 eller GO bag den). Er den
et Word-dokument, et regneark, et billede, en mail eller lignende, sender KontAKT
den hertil (app/mappe_pdf.py), og robotten:

  1. henter den uploadede fil fra KontAKTs filstore (GET .../content),
  2. laver den om til PDF med ``oomtm.convert`` - den samme vej som alle
     KontAKTs PDF-robotter: WeasyPrint til mails, HTML, tekst og CSV,
     LibreOffice til Office, Pillow til billeder (alle sider af en TIFF),
  3. lægger PDF'en på SAMME dokument (POST .../store?kind=pdf), med en note
     til sagsbehandleren, hvis der er noget, PDF'en ikke viser.

Bagefter kan dokumentet screenes og overstreges som alle andre.

ORIGINALEN RØRES IKKE, FØR DER ER EN PDF
    /store er det eneste kald, der skriver i filstoret, og det sker kun med en
    rigtig PDF (oomtm.convert tjekker %PDF-). Kan filen ikke laves om, sender
    robotten ``skipped`` med oomtm's forklaring til /file - og KontAKT lader den
    uploadede fil ligge og udleverer den, som den er
    (api/routes.document_file_status). Intet kan altså gå tabt her: værst
    tænkeligt bliver filen ikke screenet.

EN MAIL MED VEDHÆFTEDE FILER
    Mailen bliver en PDF af selve mailen: hoved, tekst og billederne i teksten
    (logoet i en signatur, et indsat skærmbillede). De rigtige vedhæftede filer
    kommer ikke med - de står i mailens hoved som "Vedhæftet", og noten på
    dokumentet beder sagsbehandleren lægge dem i mappen hver for sig. Før
    blev hele mailen udleveret, som den var, og kunne ikke screenes.

Queue payload (sat af KontAKT):
    {
        "kontakt_case_id": 11,
        "doc_id": 42,                    # case_documents.id
        "source_case_id": "Egne filer",  # mappens navn - kun til loggen
        "dok_id": "UPL-3",
        "akt_id": 3,
        "filnavn": "Referat_fra_moede.docx",
        "title": "Referat fra møde"
    }

OO config:
    Credential KontAKTAPI      username = base URL, password = X-API-Key
"""
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
import tempfile
from pathlib import Path

from robot_framework import reset
from robot_framework.exceptions import CaseDeleted
from oomtm import convert

# KontAKTs file_note er 500 tegn.
NOTE_MAX = 500
# ----- Deleted in KontAKT ----------------------------------------------------


def _check_gone(resp) -> None:
    """Stop cleanly if what this queue element is about was deleted in KontAKT.

    KontAKT answers HTTP 410 with ``{"deleted": "case"|"reference"|"document"}``
    when the caseworker deleted the KontAKT case, the mappe or the file while
    this element waited in the queue. Not an error and not retryable, so the
    queue framework marks the element done and takes the next one.
    """
    if resp is None or resp.status_code != 410:
        return
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if body.get("deleted"):
        raise CaseDeleted(body.get("note") or f"{body['deleted']} deleted in KontAKT")


def process(
    orchestrator_connection: OrchestratorConnection,
    queue_element: QueueElement | None = None,
    client: "reset.Client | None" = None,
) -> None:
    orchestrator_connection.log_trace("Running process.")
    if queue_element is None:
        raise RuntimeError("KontAKTMappeToPDF is queue-driven; no queue_element given.")
    if client is None:  # e.g. a manual run outside the queue framework
        client = reset.open_all(orchestrator_connection)

    payload = json.loads(queue_element.data or "{}")
    case_id = int(payload["kontakt_case_id"])
    doc_id = int(payload["doc_id"])
    filnavn = str(payload.get("filnavn") or "").strip()

    orchestrator_connection.log_info(
        f"MappeToPDF case={case_id} doc={doc_id} mappe={payload.get('source_case_id')!r} "
        f"fil={filnavn!r}")
    _callback(orchestrator_connection, client, case_id, doc_id, {"status": "converting"})

    try:
        status, note = _lav_pdf(orchestrator_connection, client, case_id, doc_id, filnavn)
    except CaseDeleted:
        raise
    except Exception as exc:
        orchestrator_connection.log_info(f"MappeToPDF failed: {exc!r}")
        # KontAKT lader den uploadede fil ligge, saa den stadig udleveres.
        _callback(orchestrator_connection, client, case_id, doc_id,
                  {"status": "error", "note": str(exc)[:500]})
        raise

    # Paa succes har /store allerede sat status og metadata. Kun det, der IKKE
    # blev en PDF, skal meldes - og KontAKT beholder originalen.
    if status != "ready":
        _callback(orchestrator_connection, client, case_id, doc_id,
                  {"status": "skipped", "note": note[:500]})
    orchestrator_connection.log_info(f"MappeToPDF done doc={doc_id}: {status} {note}")


# ----- Konvertering ----------------------------------------------------------


def _lav_pdf(oc, client, case_id: int, doc_id: int, filnavn: str) -> tuple[str, str]:
    """("ready", note) naar PDF'en er lagt paa dokumentet, ellers ("skipped", hvorfor)."""
    endelse = Path(filnavn).suffix.lower().lstrip(".")
    with tempfile.TemporaryDirectory() as tmp:
        ind = Path(tmp) / "ind"
        ud = Path(tmp) / "ud"
        ind.mkdir()
        ud.mkdir()
        # Et fast, kort navn: sagsbehandlerens filnavn kan vaere langt nok til
        # at ramme Windows' 260 tegn.
        src = ind / f"original.{endelse or 'bin'}"
        _hent(client, case_id, doc_id, src)
        oc.log_info(f"Hentet {src.stat().st_size} byte.")

        r = convert.convert(src, ud, name=filnavn or src.name, log=oc.log_info)
        oc.log_info(f"oomtm.convert: ok={r.ok} motor={r.engine} vedhaeftet={r.attachments} "
                    f"advarsler={r.warnings}")
        if not r.ok:
            return "skipped", (r.note or "Filen kunne ikke laves om til PDF.")

        note = note_til_sagsbehandler(r)
        nyt_navn = (Path(filnavn).stem or "dokument") + ".pdf"
        _gem(client, case_id, doc_id, r.pdf, nyt_navn, note)
        return "ready", note


def note_til_sagsbehandler(r) -> str:
    """Det, PDF'en ikke viser, i hele saetninger: en mails vedhaeftede filer og
    oomtm's advarsler (sporede aendringer, kommentarer, skjulte ark). Tom, hvis
    der ikke er noget."""
    dele = []
    if r.attachments:
        navne = ", ".join(r.attachments[:5])
        if len(r.attachments) > 5:
            navne += f" og {len(r.attachments) - 5} til"
        dele.append(f"Mailen har vedhæftede filer, som ikke er med i PDF'en: {navne}. "
                    "Læg dem i mappen hver for sig, hvis de skal med.")
    dele.extend(r.warnings or [])
    note = " ".join(dele)
    if len(note) > NOTE_MAX:
        note = note[:NOTE_MAX - 1].rstrip() + "…"
    return note


# ----- KontAKT ---------------------------------------------------------------


def _hent(client, case_id: int, doc_id: int, dest: Path) -> None:
    """Den uploadede fil fra KontAKTs filstore, streamet til disk."""
    r = client.session.get(
        f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/content",
        stream=True, timeout=(10, 600),
    )
    _check_gone(r)
    r.raise_for_status()
    with open(dest, "wb") as fh:
        for blok in r.iter_content(chunk_size=1 << 20):
            fh.write(blok)


def _gem(client, case_id: int, doc_id: int, pdf: Path, filnavn: str, note: str = "") -> None:
    """PDF'en paa dokumentet. /store skriver selv navn, stoerrelse, hash,
    status ('ready') og noten (vises ved dokumentet i KontAKT), saa der er
    ingen kvittering bagefter."""
    with open(pdf, "rb") as fh:
        r = client.session.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/store",
            params={"filename": filnavn, "kind": "pdf", "note": note},
            headers={"Content-Type": "application/octet-stream"},
            data=fh, timeout=600,
        )
    _check_gone(r)
    r.raise_for_status()


def _callback(orchestrator_connection, client, case_id: int, doc_id: int, body: dict) -> None:
    try:
        resp = client.session.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/file",
            json=body, timeout=30,
        )
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Callback to KontAKT failed: {exc!r}")
        return
    # Outside the except: a network blip stays harmless, but "deleted in KontAKT"
    # must reach the framework instead of being swallowed as a broad Exception.
    _check_gone(resp)
