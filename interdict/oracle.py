"""The external oracle: OpenSanctions yente.

yente is not part of the product. It is an independent implementation of the same task,
built by people who have never seen this code, and it exists here so that every claim
Interdict makes about screening quality is graded by something the builder does not
control. Agreement numbers in the README and the demo come from this module.

SCOPE PIN (audit F6) -- the endpoint is `/match/us_ofac_sdn` and nothing else. yente's
default scope spans 465 datasets and will happily "match" a grantee against a Wikidata
politician or a SAM exclusion list, neither of which creates OFAC liability. Using the
default scope would inflate every agreement number in this project. The manifest indexes
only us_ofac_sdn precisely so the wrong endpoint cannot be reached by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

YENTE_URL = os.environ.get("INTERDICT_YENTE_URL", "http://localhost:8000")
SCOPE = "us_ofac_sdn"          # never "default" -- see the module docstring

# yente identifies entities by OpenSanctions canonical id (e.g. "Q334775"). The OFAC
# uid we screen against appears in the entity's referents as "ofac-<uid>".
_OFAC_PREFIX = "ofac-"


@dataclass(frozen=True)
class OracleHit:
    sdn_uid: str | None
    score: float
    caption: str
    canonical_id: str


def _sdn_uid(result: dict) -> str | None:
    for ref in result.get("referents") or []:
        if ref.startswith(_OFAC_PREFIX):
            return ref[len(_OFAC_PREFIX):]
    # Fall back to the OFAC detail URL, which carries the same uid.
    for url in (result.get("properties", {}) or {}).get("sourceUrl", []):
        if "Details.aspx?id=" in url:
            return url.rsplit("=", 1)[-1]
    return None


class Oracle:
    """Batched yente client.

    yente's /match endpoint takes many queries per request, which matters: grading a
    400-entry book one HTTP call at a time is slow enough that people stop doing it
    daily, and a grade you stop collecting is not a grade.
    """

    def __init__(self, base_url: str = YENTE_URL, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Oracle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def match(self, queries: dict[str, dict], threshold: float = 0.7,
              limit: int = 5) -> dict[str, list[OracleHit]]:
        """Screen a batch. `queries` maps a caller-chosen key to a query spec."""
        if not queries:
            return {}

        payload = {"queries": {
            key: {
                "schema": q.get("schema", "Person"),
                "properties": {
                    k: v for k, v in (
                        ("name", [q["name"]]),
                        ("birthDate", [q["dob"]] if q.get("dob") else None),
                    ) if v
                },
            }
            for key, q in queries.items()
        }}

        resp = self._client.post(
            f"{self.base_url}/match/{SCOPE}",
            params={"threshold": threshold, "limit": limit},
            json=payload,
        )
        resp.raise_for_status()
        body = resp.json()

        out: dict[str, list[OracleHit]] = {}
        for key, response in body.get("responses", {}).items():
            out[key] = [
                OracleHit(
                    sdn_uid=_sdn_uid(r),
                    score=float(r.get("score", 0.0)),
                    caption=r.get("caption", ""),
                    canonical_id=r.get("id", ""),
                )
                for r in response.get("results", [])
            ]
        return out

    def healthy(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/readyz").status_code == 200
        except httpx.HTTPError:
            return False
