"""Command line: `hash-pin` (for users.json), `verify` (audit journal chain)."""

import getpass
import json
import sys

from app import auth, db, journal

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "hash-pin":
        pin = getpass.getpass("PIN (6 chiffres) : ")
        if not (len(pin) == auth.PIN_LENGTH and pin.isdigit()):
            sys.exit("Le PIN doit faire 6 chiffres.")
        h, s = auth.hash_pin(pin)
        print(json.dumps({"pin_hash": h, "pin_salt": s}, indent=2))
    elif cmd == "verify":
        print(json.dumps(journal.verify_chain(db.connect()), indent=2))
    else:
        sys.exit("usage: python medirappel.py hash-pin | verify")
