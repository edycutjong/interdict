"""Malformed-record, delta-schema and fetch behaviour, on synthetic feeds.

Separate from test_ofac_parse.py on purpose. That module asserts figures about the real
27MB publication and is therefore skipped wholesale when data/SDN.XML is absent -- which
is the normal state of a fresh clone and of CI, because the file is gitignored. The
behaviours pinned here (what the parser does with a record OFAC published badly, and what
fetch() does with Treasury's redirect) must be verified on every run, not only on a
machine that happens to have run `make fetch-sdn`. So they run against small inline
fixtures with no data dependency and no network: the redirect tests talk to a loopback
HTTP server started by the test itself.
"""

import hashlib
import http.server
import threading
from typing import ClassVar

import pytest

from interdict.ofac import WEAK, fetch, parse_delta, parse_sdn

SDN_XMLNS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"
DELTA_XMLNS = "https://www.treasury.gov/ofac/DeltaFile/1.0"


def _write_sdn(tmp_path, body: str, publication: str = "") -> object:
    """A minimal but schema-faithful SDN.XML -- same namespace, same element names."""
    path = tmp_path / "sdn.xml"
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<sdnList xmlns="{SDN_XMLNS}">{publication}{body}</sdnList>',
        encoding="utf-8",
    )
    return path


def _write_delta(tmp_path, body: str) -> object:
    path = tmp_path / "delta.xml"
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<sanctionsData xmlns="{DELTA_XMLNS}"><entities>{body}</entities></sanctionsData>',
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# SDN.XML: records OFAC published badly
# ---------------------------------------------------------------------------


def test_a_publication_without_the_header_element_parses_to_empty_metadata(tmp_path):
    """No publshInformation must mean empty strings, not an exception.

    The header is the one element whose absence would otherwise take the whole parse
    down, and a truncated download is exactly how it goes missing. Entries must still
    come back so the caller can see the payload is short and refuse to promote it.
    """
    path = _write_sdn(tmp_path, "<sdnEntry><uid>1</uid><lastName>SMITH</lastName></sdnEntry>")

    entries, pub = parse_sdn(path)

    assert pub == {"publish_date": "", "record_count": ""}
    assert [e.uid for e in entries] == ["1"]


def test_an_entry_with_no_uid_is_dropped_rather_than_stored_with_an_empty_key(tmp_path):
    """uid is the ledger's identity for a party. A record without one cannot be kept.

    Two uid-less records would collide on the empty key and silently overwrite each
    other, so the entry is discarded and its names must not surface in the list at all.
    """
    path = _write_sdn(
        tmp_path,
        "<sdnEntry><lastName>NO UID</lastName></sdnEntry>"
        "<sdnEntry><uid>  </uid><lastName>BLANK UID</lastName></sdnEntry>"
        "<sdnEntry><uid>7</uid><lastName>KEPT</lastName></sdnEntry>",
    )

    entries, _ = parse_sdn(path)

    assert [e.uid for e in entries] == ["7"]
    assert all(e.primary_name != "NO UID" for e in entries)
    assert all(e.primary_name != "BLANK UID" for e in entries)


def test_an_alias_with_no_name_text_is_dropped(tmp_path):
    """An empty alias would normalise to the empty string and then match everything.

    OFAC does publish aka blocks carrying only a category and no name parts; they must
    never reach the matcher.
    """
    path = _write_sdn(
        tmp_path,
        "<sdnEntry><uid>9</uid><lastName>REAL</lastName><akaList>"
        '<aka><category>weak</category><type>a.k.a.</type></aka>'
        '<aka><firstName>  </firstName><lastName>  </lastName></aka>'
        "<aka><lastName>SECOND</lastName></aka>"
        "</akaList></sdnEntry>",
    )

    (entry,), _ = parse_sdn(path)

    assert [n.text for n in entry.names] == ["REAL", "SECOND"]
    assert all(n.text for n in entry.names)


def test_an_entry_whose_only_names_are_empty_akas_is_kept_with_no_names(tmp_path):
    """The uid still exists on the list, so it is not invented away -- but it carries
    nothing matchable, which is what lets a caller spot the defect instead of screening
    against a blank name."""
    path = _write_sdn(
        tmp_path,
        "<sdnEntry><uid>11</uid><akaList><aka><category>weak</category></aka></akaList></sdnEntry>",
    )

    (entry,), _ = parse_sdn(path)

    assert entry.uid == "11"
    assert entry.names == ()
    assert entry.primary_name == ""


def test_an_alias_without_a_category_defaults_to_strong_not_weak(tmp_path):
    """The matcher downweights weak aliases. Defaulting an unlabelled alias to weak
    would quietly suppress real hits, so the default is strong and only OFAC's own
    `weak` flag downweights."""
    path = _write_sdn(
        tmp_path,
        "<sdnEntry><uid>3</uid><lastName>PRIMARY</lastName><akaList>"
        "<aka><lastName>UNLABELLED</lastName></aka>"
        "<aka><category>weak</category><type>f.k.a.</type><lastName>FLAGGED</lastName></aka>"
        "</akaList></sdnEntry>",
    )

    (entry,), _ = parse_sdn(path)
    by_text = {n.text: n for n in entry.names}

    assert by_text["UNLABELLED"].category == "strong"
    assert by_text["UNLABELLED"].kind == "a.k.a."
    assert by_text["FLAGGED"].category == WEAK
    assert by_text["FLAGGED"].kind == "f.k.a."
    assert by_text["PRIMARY"].category == "primary"


# ---------------------------------------------------------------------------
# /changes/latest: the delta schema
# ---------------------------------------------------------------------------


def test_a_delta_entity_without_an_action_is_skipped(tmp_path):
    """Every real delta entity carries add/remove/modify. One without an action cannot
    be applied to the ledger in either direction, so it must not become a silent no-op
    row -- it must not appear at all."""
    path = _write_delta(
        tmp_path,
        '<entity id="100"><generalInfo><entityType>Entity</entityType></generalInfo></entity>'
        '<entity id="101" action="   "></entity>'
        '<entity id="102" action="remove"></entity>',
    )

    actions = parse_delta(path)

    assert [a.uid for a in actions] == ["102"]


def test_a_delta_action_is_normalised_to_lower_case(tmp_path):
    path = _write_delta(tmp_path, '<entity id="103" action=" ADD "></entity>')

    assert [a.action for a in parse_delta(path)] == ["add"]


def test_a_translation_without_a_full_name_falls_back_to_first_and_last(tmp_path):
    """Regression guard, same defect as test_delta_names_are_full_names_not_first_names:
    when formattedFullName is absent the parser must join both parts, not take whichever
    part it found first."""
    path = _write_delta(
        tmp_path,
        '<entity id="104" action="add"><names><name><translations>'
        "<translation>"
        "<formattedFirstName>Mario German</formattedFirstName>"
        "<formattedLastName>SATIZABAL RENGIFO</formattedLastName>"
        "</translation>"
        "</translations></name></names></entity>",
    )

    (action,) = parse_delta(path)

    assert action.name == "Mario German SATIZABAL RENGIFO"


def test_the_primary_translation_wins_over_document_order(tmp_path):
    """Transliterations are published alongside the primary name and often come first.
    The name written to the ledger is the one OFAC flags isPrimary; the rest are kept as
    searchable aliases."""
    path = _write_delta(
        tmp_path,
        '<entity id="105" action="add"><names><name><translations>'
        "<translation><formattedFullName>ALIAS, Transliterated</formattedFullName>"
        "<isPrimary>false</isPrimary></translation>"
        "<translation><formattedFullName>REAL, Name</formattedFullName>"
        "<isPrimary>true</isPrimary></translation>"
        "<translation><formattedFullName></formattedFullName></translation>"
        "</translations></name></names></entity>",
    )

    (action,) = parse_delta(path)

    assert action.name == "REAL, Name"
    assert action.names == ("ALIAS, Transliterated", "REAL, Name")


def test_a_nameless_delta_entity_yields_an_empty_name_rather_than_failing(tmp_path):
    """A removal only needs its uid to be applicable, so a missing name must not abort
    the whole delta."""
    path = _write_delta(tmp_path, '<entity id="106" action="remove"></entity>')

    (action,) = parse_delta(path)

    assert action.uid == "106"
    assert action.name == ""
    assert action.names == ()


# ---------------------------------------------------------------------------
# fetch(): the redirect, the hash, the identification
# ---------------------------------------------------------------------------

PRESIGNED_PATH = "/s3/SDN.XML?X-Amz-Expires=3600"


class _TreasuryHandler(http.server.BaseHTTPRequestHandler):
    """Mimics Treasury's handoff: the published URL 302s to a presigned S3 URL."""

    payload = b""
    seen: ClassVar[list] = []       # replaced per server by the fixture's subclass

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("User-Agent", "")))
        if self.path == "/SDN.XML":
            self.send_response(302)
            self.send_header("Location", PRESIGNED_PATH)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def treasury():
    """Start a loopback server serving `payload`. Returns (base_url, request_log)."""
    servers = []

    def _start(payload: bytes):
        handler = type("_Handler", (_TreasuryHandler,), {"payload": payload, "seen": []})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", handler.seen

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


def test_fetch_follows_the_presigned_redirect_to_the_real_payload(tmp_path, treasury):
    """SDN.XML 302s to a presigned S3 URL. A client that does not follow the redirect
    archives the 302 body and hashes it, which is how a provenance anchor ends up
    pointing at nothing."""
    payload = b"<sdnList>real</sdnList>"
    base, seen = treasury(payload)
    dest = tmp_path / "SDN.XML"

    digest = fetch(f"{base}/SDN.XML", dest)

    assert [path for path, _ in seen] == ["/SDN.XML", PRESIGNED_PATH]
    assert dest.read_bytes() == payload
    assert digest == hashlib.sha256(payload).hexdigest()


def test_fetch_hashes_and_writes_the_whole_stream_not_just_the_first_chunk(tmp_path, treasury):
    """The body is read in 1MiB chunks and the real SDN.XML is 27MB. If the digest or
    the write were taken from the first chunk the hash would still look like a hash --
    and would still be wrong for every publication."""
    payload = b"a" * (1 << 20) + b"TAIL-BEYOND-THE-FIRST-CHUNK"
    base, _ = treasury(payload)
    dest = tmp_path / "SDN.XML"

    digest = fetch(f"{base}/SDN.XML", dest)

    assert dest.stat().st_size == len(payload)
    assert dest.read_bytes().endswith(b"TAIL-BEYOND-THE-FIRST-CHUNK")
    assert digest == hashlib.sha256(payload).hexdigest()
    assert digest != hashlib.sha256(payload[: 1 << 20]).hexdigest()


def test_fetch_creates_the_archive_directory_and_identifies_the_client(tmp_path, treasury):
    """Archive paths are dated directories that do not exist yet, and Treasury is
    entitled to block an unidentified default urllib agent -- both are fetch's job."""
    base, seen = treasury(b"payload")
    dest = tmp_path / "archive" / "2026-08-27" / "SDN.XML"
    assert not dest.parent.exists()

    fetch(f"{base}/SDN.XML", dest)

    assert dest.read_bytes() == b"payload"
    assert {agent for _, agent in seen} == {"interdict/1.0"}
