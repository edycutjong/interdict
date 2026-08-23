#!/usr/bin/env python3
"""Print the Firestore document ids and values the demo video's C2/C3 shots need.

Every re-screen run appends to the ledger and rewrites the run summary, so any id
written down by hand goes stale the next time `make demo-state` is run. Read them from
the live mirror instead, immediately before filming.
"""
import os
import sys

from google.cloud import firestore

PROJECT = os.environ.get("INTERDICT_FIRESTORE_PROJECT")
if not PROJECT:
    sys.exit("set INTERDICT_FIRESTORE_PROJECT (and GOOGLE_APPLICATION_CREDENTIALS)")

db = firestore.Client(project=PROJECT)

newest = next(iter(
    db.collection("interdict_ledger").order_by(
        "seq", direction=firestore.Query.DESCENDING).limit(1).stream()), None)
if newest is None:
    sys.exit("interdict_ledger is empty -- has a run been mirrored?")

print(f"project            {PROJECT}")
print(f"\nC2  interdict_ledger/{newest.id}")
d = newest.to_dict()
print(f"    seq            {d.get('seq')}")
print(f"    event_type     {d.get('event_type')}")
for f in ("payload", "prev_hash", "entry_hash"):
    print(f"    {f:<14} {'present' if d.get(f) is not None else 'MISSING'}")

runs = sorted(db.collection("interdict_runs").stream(), key=lambda r: r.id)
if runs:
    r = runs[-1]
    print(f"\nC3  interdict_runs/{r.id}")
    for k, v in sorted(r.to_dict().items()):
        print(f"    {k:<20} {v}")
