"""KontAKT: lav en fil i en mappe om til PDF.

Queue-driven, ét køelement pr. fil. En sagsbehandler har lagt en fil i en mappe
i KontAKT (en sag, KontAKT selv ejer - der er ingen F2 eller GO bag den). Er den
et Word-dokument, et regneark, et billede, en mail eller lignende, sender KontAKT
den hertil (app/mappe_pdf.py), og robotten:

  1. henter den uploadede fil fra KontAKTs filstore (GET .../content),
  2. laver den om til PDF med ``oomtm.pdf.convert_to_pdf`` - LibreOffice til
     Office og tekst, Pillow til billeder, mails via HTML. Den samme motor, som
     KontAKTGOToPDF bruger, når GO selv afviser en fil,
  3. lægger PDF'en på SAMME dokument (POST .../store?kind=pdf).

Bagefter kan dokumentet screenes og overstreges som alle andre.

ORIGINALEN RØRES IKKE, FØR DER ER EN PDF
    /store er det eneste kald, der skriver i filstoret, og det sker kun med en
    fil, der begynder med %PDF-. Kan filen ikke laves om, sender robotten
    ``skipped`` med en forklaring til /file - og KontAKT lader den uploadede fil
    ligge og udleverer den, som den er (api/routes.document_file_status). Intet
    kan altså gå tabt her: værst tænkeligt bliver filen ikke screenet.

TO STEDER, HVOR EN PDF VILLE MISTE NOGET - DER LAVES DEN IKKE
    * En mail med vedhæftede filer. oomtm.pdf skriver mailens tekst og LISTER
      de vedhæftede filers navne - selve filerne kommer ikke med. Ansøgeren
      ville få mindre, end sagsbehandleren lagde i mappen. Så bliver mailen
      udleveret, som den er, og noten siger, hvorfor.
    * Et billede med flere sider (en scannet TIFF). oomtm.pdf.image_to_pdf
      gemmer kun den første side. Her laves alle siderne om, én pr. side.

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
from oomtm import pdf as oomtm_pdf

# Filtyper med flere sider i én billedfil. Andre billeder har én side (en
# animeret GIF er ikke et dokument - dens første billede er nok).
FLERSIDEDE_BILLEDER = {"tif", "tiff"}


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
    """("ready", "") naar PDF'en er lagt paa dokumentet, ellers ("skipped", hvorfor)."""
    endelse = Path(filnavn).suffix.lower().lstrip(".")
    with tempfile.TemporaryDirectory() as tmp:
        ind = Path(tmp) / "ind"
        ud = Path(tmp) / "ud"
        ind.mkdir()
        ud.mkdir()
        # Et fast, kort navn: LibreOffice kalder resultatet <stem>.pdf, og
        # sagsbehandlerens filnavn kan vaere langt nok til at ramme MAX_PATH.
        src = ind / f"original.{endelse or 'bin'}"
        _hent(client, case_id, doc_id, src)
        oc.log_info(f"Hentet {src.stat().st_size} byte.")

        if endelse == "pdf":
            # KontAKT sender ikke PDF'er hertil - men en PDF er allerede det, vi
            # skal have. Den laegges tilbage, som den er.
            pdf = src
        else:
            pdf, hvorfor = _konverter(oc, src, endelse, ud)
            if pdf is None:
                return "skipped", hvorfor

        with open(pdf, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                return "skipped", "Konverteringen gav ikke en PDF."

        nyt_navn = (Path(filnavn).stem or "dokument") + ".pdf"
        _gem(client, case_id, doc_id, pdf, nyt_navn)
        return "ready", ""


def _konverter(oc, src: Path, endelse: str, ud: Path):
    """(pdf-sti, "") eller (None, hvorfor)."""
    if endelse in oomtm_pdf.EMAIL_EXTS:
        try:
            vedhaeftede = _rigtige_vedhaeftninger(src, endelse)
        except Exception as exc:  # pylint: disable=broad-except
            oc.log_info(f"Kunne ikke læse mailens vedhæftninger: {exc!r}")
            return None, ("Mailen kunne ikke læses, så det er ikke sikkert, at en PDF "
                          "ville få det hele med.")
        if vedhaeftede:
            navne = ", ".join(vedhaeftede[:5]) + (" …" if len(vedhaeftede) > 5 else "")
            return None, (f"Mailen har vedhæftede filer ({navne}), som ikke kommer med i "
                          "en PDF. Læg dem i mappen som egne filer, hvis de skal screenes.")

    if endelse in FLERSIDEDE_BILLEDER:
        sider = _flersidet_billede_til_pdf(src, ud / f"{src.stem}.pdf")
        if sider:
            oc.log_info(f"Billede med {sider} sider lavet om til PDF.")
            return ud / f"{src.stem}.pdf", ""
        # Én side, eller Pillow kunne ikke - saa tager oomtm den almindelige vej.

    pdf, status, note = oomtm_pdf.convert_to_pdf(
        src, endelse, ud, auto_install=True, log=oc.log_info,
    )
    if status == "ready" and pdf is not None:
        return Path(pdf), ""
    return None, (note or "Filen kunne ikke laves om til PDF.")


def _flersidet_billede_til_pdf(src: Path, out: Path) -> int:
    """Alle sider i en TIFF som én PDF. Antal sider, eller 0 hvis billedet kun
    har én side (eller ikke kunne aabnes) - saa klarer oomtm.pdf den."""
    from PIL import Image, ImageFile  # Pillow er en kerneafhaengighed (error_screenshot)
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(src) as im:
            antal = getattr(im, "n_frames", 1) or 1
            if antal <= 1:
                return 0
            sider = []
            for i in range(antal):
                im.seek(i)
                sider.append(_rgb(im.copy()))
    except Exception:  # pylint: disable=broad-except
        return 0
    sider[0].save(out, "PDF", resolution=150.0, save_all=True, append_images=sider[1:])
    return antal if out.exists() and out.stat().st_size > 0 else 0


def _rgb(im):
    """PDF kan ikke gemme gennemsigtighed - laeg billedet paa hvid baggrund."""
    from PIL import Image
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bund = Image.new("RGB", im.size, (255, 255, 255))
        bund.paste(im, mask=im.split()[-1])
        return bund
    return im.convert("RGB") if im.mode != "RGB" else im


def _rigtige_vedhaeftninger(src: Path, endelse: str) -> list[str]:
    """Navnene paa mailens vedhaeftede filer - dem, en PDF af mailen ville tabe.

    Ikke billeder, der sidder INDE i teksten (logoet i en signatur, et indsat
    skaermbillede): de staar i mailens HTML og kommer med i PDF'en, saa de ville
    ellers stoppe naesten alle mails fra Outlook.
    """
    if endelse == "msg":
        import extract_msg  # kommer med oomtm[pdf]
        msg = extract_msg.openMsg(str(src))
        try:
            html = getattr(msg, "htmlBody", None) or b""
            if isinstance(html, bytes):
                html = html.decode("utf-8", errors="replace")
            navne = []
            for att in msg.attachments or []:
                cid = (getattr(att, "cid", None) or getattr(att, "contentId", None) or "").strip("<>")
                if getattr(att, "hidden", False) or (cid and f"cid:{cid}" in html):
                    continue
                navn = (getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
                        or getattr(att, "name", None) or "uden navn")
                navne.append(str(navn).replace("\x00", "").strip() or "uden navn")
            return navne
        finally:
            msg.close()

    from email import policy
    from email.parser import BytesParser
    with open(src, "rb") as fh:
        m = BytesParser(policy=policy.default).parse(fh)
    html_del = m.get_body(preferencelist=("html",))
    html = html_del.get_content() if html_del is not None else ""
    navne = []
    for part in m.walk():
        if part.is_multipart() or part is html_del:
            continue
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if cid and f"cid:{cid}" in html:
            continue        # et billede i teksten - det kommer med i PDF'en
        filnavn = part.get_filename()
        if part.get_content_disposition() == "attachment" or filnavn:
            navne.append(filnavn or "uden navn")
    return navne


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


def _gem(client, case_id: int, doc_id: int, pdf: Path, filnavn: str) -> None:
    """PDF'en paa dokumentet. /store skriver selv navn, stoerrelse, hash og
    status ('ready'), saa der er ingen kvittering bagefter."""
    with open(pdf, "rb") as fh:
        r = client.session.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/store",
            params={"filename": filnavn, "kind": "pdf", "note": ""},
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
