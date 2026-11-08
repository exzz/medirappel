"""Read-only CardDAV address books, one per managed practitioner (CLAUDE.md §8).

The SMSGate phone syncs them with DAVx⁵: /carddav/<trophy practitioner id>/ holds every
patient of that practitioner (referent in Trophy) with a French mobile. Only what DAVx⁵
needs: PROPFIND (depth 0/1), REPORT addressbook-multiget, GET. Writes are refused.
Basic auth with settings.json `contacts.username` / `contacts.password`; no block, no route.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response

from . import auth, config, db, planner, trophy

log = logging.getLogger(__name__)
router = APIRouter()

DAV_METHODS = ["OPTIONS", "PROPFIND", "REPORT", "GET", "HEAD", "PUT", "DELETE", "PROPPATCH", "MKCOL", "MOVE", "COPY"]
NS = 'xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" xmlns:cs="http://calendarserver.org/ns/"'


def _authorized(request: Request) -> Response | None:
    """None if the phone may read; otherwise the response to send."""
    cfg = config.settings().get("contacts")
    if not cfg:
        return Response(status_code=404)
    ip = request.client.host if request.client else ""
    if not auth.take_ip_try(ip):
        return Response(status_code=429)
    try:
        user, _, pwd = base64.b64decode(request.headers.get("authorization", "")[6:]).decode().partition(":")
    except ValueError:
        user = pwd = ""
    if secrets.compare_digest(user.encode(), cfg["username"].encode()) & \
            secrets.compare_digest(pwd.encode(), cfg["password"].encode()):
        auth.give_back_ip_try(ip)
        return None
    if user:
        log.warning("carddav: wrong credentials from %s", ip)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="MediRappel"'})


def _vcard(p: dict, phone: str) -> str:
    nom, prenom = (p["nom"] or "").upper().strip(), (p["prenom"] or "").strip()
    esc = lambda v: v.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")  # noqa: E731
    return "\r\n".join(["BEGIN:VCARD", "VERSION:3.0", f"UID:medirappel-patient-{p['id']}",
                        f"FN:{esc(' '.join(x for x in (nom, prenom) if x))}", f"N:{esc(nom)};{esc(prenom)};;;",
                        f"TEL;TYPE=CELL:{phone}", "END:VCARD", ""])


def _book(pid: int) -> dict[str, str] | None:
    """{'<patient id>.vcf': vcard} for this practitioner; None when the cache is empty
    (answering an empty book would make DAVx⁵ delete every contact on the phone)."""
    conn = db.connect()
    try:
        patients = trophy.cached_patients(conn)
    finally:
        conn.close()
    if not patients:
        return None
    book = {}
    for p in patients:
        phone = planner.french_mobile(p["mobile"]) or planner.french_mobile(p["tel"])
        if p.get("praticien_id") == pid and phone:
            book[f"{p['id']}.vcf"] = _vcard(p, phone)
    return book


def _etag(text: str) -> str:
    return '"' + hashlib.sha256(text.encode()).hexdigest()[:32] + '"'


def _multistatus(parts: list[str]) -> Response:
    body = f'<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus {NS}>' + "".join(parts) + "</d:multistatus>"
    return Response(body, status_code=207, media_type="application/xml; charset=utf-8")


def _card_response(href: str, card: str, with_data: bool) -> str:
    data = f"<card:address-data>{escape(card)}</card:address-data>" if with_data else ""
    return (f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>"
            f"<d:resourcetype/><d:getcontenttype>text/vcard; charset=utf-8</d:getcontenttype>"
            f"<d:getetag>{escape(_etag(card))}</d:getetag>{data}</d:prop>"
            f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


@router.api_route("/carddav/{pid}/", methods=DAV_METHODS)
@router.api_route("/carddav/{pid}/{name}", methods=DAV_METHODS)
async def carddav(request: Request, pid: int, name: str | None = None):
    if (denied := _authorized(request)) is not None:
        return denied
    conn = db.connect()
    try:
        practitioner = next((p for p in planner.managed_practitioners(conn) if p["practitioner_id"] == pid), None)
    finally:
        conn.close()
    if practitioner is None:  # assistants and unknown ids have no book
        return Response(status_code=404)
    if request.method == "OPTIONS":
        return Response(headers={"DAV": "1, 3, addressbook", "Allow": "OPTIONS, PROPFIND, REPORT, GET, HEAD"})
    if request.method not in ("PROPFIND", "REPORT", "GET", "HEAD"):
        return Response(status_code=403)  # read-only: Trophy is the only source
    trophy.refresh_patients_soon()  # new patients reach the next sync
    book = _book(pid)
    if book is None:
        return Response(status_code=503, headers={"Retry-After": "300"})
    base = f"/carddav/{pid}/"

    if name is not None:
        card = book.get(name)
        if card is None or request.method == "REPORT":
            return Response(status_code=404)
        if request.method in ("GET", "HEAD"):
            return Response(card if request.method == "GET" else b"", media_type="text/vcard; charset=utf-8",
                            headers={"ETag": _etag(card)})
        return _multistatus([_card_response(base + name, card, False)])

    if request.method == "REPORT":
        try:
            root = ET.fromstring(await request.body())
        except ET.ParseError:
            return Response(status_code=400)
        if root.tag != "{urn:ietf:params:xml:ns:carddav}addressbook-multiget":
            return Response(status_code=403)
        parts = []
        for h in root.iter("{DAV:}href"):
            href = (h.text or "").strip()
            card = book.get(href.rsplit("/", 1)[-1])
            parts.append(_card_response(href, card, True) if card else
                         f"<d:response><d:href>{escape(href)}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>")
        log.info("carddav: %d contacts sent for practitioner %s", len(parts), pid)
        return _multistatus(parts)
    if request.method in ("GET", "HEAD"):
        return Response(status_code=405)

    ctag = _etag("".join(sorted(_etag(c) for c in book.values())))
    parts = [f"<d:response><d:href>{base}</d:href><d:propstat><d:prop>"
             f"<d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>"
             f"<d:displayname>{escape(practitioner['name'])}</d:displayname>"
             f"<d:current-user-principal><d:href>{base}</d:href></d:current-user-principal>"
             f"<card:addressbook-home-set><d:href>{base}</d:href></card:addressbook-home-set>"
             f"<cs:getctag>{escape(ctag)}</cs:getctag>"
             f"<d:supported-report-set><d:supported-report><d:report><card:addressbook-multiget/></d:report>"
             f"</d:supported-report></d:supported-report-set>"
             f"<d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>"
             f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"]
    if request.headers.get("depth", "0") != "0":
        parts += [_card_response(base + n, c, False) for n, c in book.items()]
    return _multistatus(parts)
