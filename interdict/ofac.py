"""OFAC feed: fetch and parse.

Two feeds, both consumed as published -- nothing here synthesises a signal.

  SDN.XML          the full list. 302-redirects to a presigned S3 URL (us-gov-west-1,
                   3600s expiry), so the client MUST follow redirects and must always
                   retry from the source URL rather than persisting the redirect target.
  /changes/latest  the delta. Its own namespace; every <entity> carries an explicit
                   action attribute (add / remove / modify).

The element `publshInformation` is misspelled in OFAC's own schema. We match OFAC's
spelling, and `tests/test_ofac_parse.py` pins it -- if Treasury ever fixes the typo we
want a failing test, not a silently empty publication date.
"""

from __future__ import annotations

import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
DELTA_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/changes/latest"

SDN_NS = {"s": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"}
DELTA_NS = {"d": "https://www.treasury.gov/ofac/DeltaFile/1.0"}

# OFAC's alias categories. 'weak' aliases are explicitly flagged by OFAC as low-quality
# identifiers -- matching on one alone is the classic false-positive generator, so the
# matcher downweights them rather than treating every aka as equal.
WEAK = "weak"


@dataclass(frozen=True)
class Name:
    text: str
    category: str      # 'primary' | 'strong' | 'weak'
    kind: str          # 'primary' | 'a.k.a.' | 'f.k.a.' | 'n.k.a.'


@dataclass(frozen=True)
class SdnEntry:
    uid: str
    sdn_type: str                       # Individual | Entity | Vessel | Aircraft
    primary_name: str
    names: tuple[Name, ...]
    programs: tuple[str, ...]
    dobs: tuple[str, ...] = field(default=())
    nationalities: tuple[str, ...] = field(default=())


def _text(el: ET.Element | None, path: str, ns: dict) -> str:
    if el is None:
        return ""
    return (el.findtext(path, default="", namespaces=ns) or "").strip()


def _join_name(first: str, last: str) -> str:
    return " ".join(p for p in (first.strip(), last.strip()) if p)


def parse_sdn(path: Path) -> tuple[list[SdnEntry], dict]:
    """Parse a full SDN.XML publication. Returns (entries, publication_info)."""
    root = ET.parse(path).getroot()

    # NOTE: OFAC misspells this element. Do not "fix" it.
    pub = root.find("s:publshInformation", SDN_NS)
    publication = {
        "publish_date": _text(pub, "s:Publish_Date", SDN_NS),
        "record_count": _text(pub, "s:Record_Count", SDN_NS),
    }

    entries: list[SdnEntry] = []
    for e in root.findall("s:sdnEntry", SDN_NS):
        uid = _text(e, "s:uid", SDN_NS)
        if not uid:
            continue

        primary = _join_name(_text(e, "s:firstName", SDN_NS), _text(e, "s:lastName", SDN_NS))
        names = [Name(primary, "primary", "primary")] if primary else []

        for aka in e.findall(".//s:aka", SDN_NS):
            text = _join_name(_text(aka, "s:firstName", SDN_NS), _text(aka, "s:lastName", SDN_NS))
            if not text:
                continue
            names.append(Name(
                text=text,
                category=_text(aka, "s:category", SDN_NS) or "strong",
                kind=_text(aka, "s:type", SDN_NS) or "a.k.a.",
            ))

        entries.append(SdnEntry(
            uid=uid,
            sdn_type=_text(e, "s:sdnType", SDN_NS),
            primary_name=primary,
            names=tuple(names),
            programs=tuple(sorted({
                (p.text or "").strip() for p in e.findall(".//s:program", SDN_NS) if p.text
            })),
            dobs=tuple(
                (d.text or "").strip()
                for d in e.findall(".//s:dateOfBirth", SDN_NS) if d.text
            ),
            nationalities=tuple(sorted({
                (c.text or "").strip()
                for c in e.findall(".//s:nationality/s:country", SDN_NS) if c.text
            })),
        ))

    return entries, publication


@dataclass(frozen=True)
class DeltaAction:
    uid: str
    action: str                  # add | remove | modify
    name: str                    # primary formatted full name
    entity_type: str = ""        # Individual | Entity | Vessel | Aircraft
    programs: tuple[str, ...] = field(default=())
    names: tuple[str, ...] = field(default=())


def parse_delta(path: Path) -> list[DeltaAction]:
    """Parse a /changes/latest delta publication.

    The delta is NOT the SDN.XML schema. It uses Treasury's richer `sanctionsData`
    format -- its own namespace, an explicit `action` attribute per entity, and names
    carried as translations with `formattedFullName` rather than first/last pairs. A
    generic "first element whose tag contains 'name'" walk pulls `formattedFirstName`
    and silently yields half a name ("Mario German" for SATIZABAL RENGIFO, Mario
    German), which then fails to match anything. So the real paths are pinned here and
    the test asserts on full names.
    """
    root = ET.parse(path).getroot()
    actions: list[DeltaAction] = []

    for ent in root.iter():
        if ent.tag.rsplit("}", 1)[-1] != "entity":
            continue
        action = (ent.get("action") or "").strip().lower()
        if not action:
            continue

        names: list[str] = []
        primary = ""
        for translation in ent.iter():
            if translation.tag.rsplit("}", 1)[-1] != "translation":
                continue
            full = _text(translation, "d:formattedFullName", DELTA_NS)
            if not full:
                first = _text(translation, "d:formattedFirstName", DELTA_NS)
                last = _text(translation, "d:formattedLastName", DELTA_NS)
                full = _join_name(first, last)
            if not full:
                continue
            names.append(full)
            if not primary and _text(translation, "d:isPrimary", DELTA_NS) == "true":
                primary = full

        actions.append(DeltaAction(
            uid=(ent.get("id") or "").strip(),
            action=action,
            name=primary or (names[0] if names else ""),
            entity_type=_text(ent, "d:generalInfo/d:entityType", DELTA_NS),
            programs=tuple(sorted({
                (p.text or "").strip()
                for p in ent.findall(".//d:sanctionsProgram", DELTA_NS) if p.text
            })),
            names=tuple(names),
        ))

    return actions


def fetch(url: str, dest: Path) -> str:
    """Fetch a feed to disk, following redirects. Returns the sha256 of the payload.

    The hash is the provenance anchor: it is what data/archive/index.json records and
    what lets a judge confirm the archived publication is byte-identical to Treasury's.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "interdict/1.0"})
    digest = hashlib.sha256()
    # urlopen follows 3xx for http(s) by default, which is what the presigned-S3
    # redirect requires.
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        while chunk := resp.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()
