"""This module handles resetting the state of the computer so the robot can work with a clean slate.

For this robot the "state" is the cached KontAKT credentials and one HTTP
session to KontAKT. ``open_all`` opens them and returns a :class:`Client`;
``reset`` re-opens them, so the queue framework can reconnect on a retry instead
of reconnecting for every single document.
"""

import requests
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from oomtm import convert, engines


class Client:
    """KontAKT's address and key + one HTTP session, reused across every queue
    element (opened once per run by ``open_all``)."""

    def __init__(self, orchestrator_connection: OrchestratorConnection):
        kontakt = orchestrator_connection.get_credential("KontAKTAPI")
        self.kontakt_base = (kontakt.username or "").rstrip("/")
        self.kontakt_key = kontakt.password
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = self.kontakt_key

    def close(self) -> None:
        self.session.close()


def reset(orchestrator_connection: OrchestratorConnection) -> Client:
    """Clean up, close/kill all programs, then (re)open the connections.

    Returns the freshly-opened :class:`Client` so the queue framework can reuse
    it across queue elements (and reconnect by calling ``reset`` again)."""
    orchestrator_connection.log_trace("Resetting.")
    clean_up(orchestrator_connection)
    close_all(orchestrator_connection)
    kill_all(orchestrator_connection)
    return open_all(orchestrator_connection)


def clean_up(orchestrator_connection: OrchestratorConnection) -> None:
    """Do any cleanup needed to leave a blank slate."""
    orchestrator_connection.log_trace("Doing cleanup.")


def close_all(orchestrator_connection: OrchestratorConnection) -> None:
    """Gracefully close all applications used by the robot: oomtm's WeasyPrint
    process (it also closes by itself when the robot exits)."""
    orchestrator_connection.log_trace("Closing all applications.")
    engines.close_workers()


def kill_all(orchestrator_connection: OrchestratorConnection) -> None:
    """Forcefully close all applications used by the robot.

    LibreOffice runs as a child process per conversion, in a profile only it
    uses, with a timeout that kills its whole process tree (oomtm.engines), so
    there is nothing left running between documents to kill here."""
    orchestrator_connection.log_trace("Killing all applications.")


def open_all(orchestrator_connection: OrchestratorConnection) -> Client:
    """Open all connections used by the robot and return them as a :class:`Client`.

    Also makes sure oomtm's engines (WeasyPrint and LibreOffice) are on the
    machine, installing them without admin if not - once per machine, so a
    missing engine shows up here and not halfway through a case."""
    orchestrator_connection.log_trace("Opening KontAKT connection.")
    motorer = convert.ensure_engines(log=orchestrator_connection.log_info)
    orchestrator_connection.log_info(f"PDF-motorer klar: {motorer}")
    return Client(orchestrator_connection)
