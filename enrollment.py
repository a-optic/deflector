# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Zero-touch capture enrolment: a client registers its own certificate.

IDENTITY IS THE CERTIFICATE'S FINGERPRINT, NOT AN IP
----------------------------------------------------
IP-keyed recipients break under DHCP -- the lease moves, the address falls out
of the map, and captures stop with no error at all. A fingerprint survives
DHCP, hostname changes, user renames and reinstalls.

It also deletes a whole class of problem rather than managing it. A different
certificate IS a different fingerprint, so "replace the cert registered for
this identity" -- the one operation that could redirect somebody else's
captures -- cannot even be expressed. Enrolment is idempotent by construction:
re-sending the same certificate is a no-op, so a reinstall needs no approval.

WHY THIS NEEDS NO TOKEN AND NO OPERATOR ACTION
----------------------------------------------
Naming a fingerprint whose private key you do not hold gains you nothing: the
only request affected is your own, which you would be handing to someone else
while losing the ability to read it. It cannot expose or redirect a third
party's data. The proxy is unauthenticated on the LAN anyway, so anyone who
could enrol could already send the traffic themselves.

THE MAC IS A LABEL, NEVER A CREDENTIAL
--------------------------------------
A MAC address is not in an HTTP request and ARP only reaches the same L2
segment, so the server cannot verify one. It is stored purely so a human can
recognise a machine in a listing -- better than a hostname, which a rename
changes. The cryptographic binding to the machine is possession of the private
key, which is a stronger claim than a MAC could ever make.

Certificates are public. Storing them world-readable is intentional.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time

ENROLL_DIR = pathlib.Path(os.path.expanduser("~/.agentstop/keys/enrolled"))

MAX_CERT_BYTES = 16384
MAX_ENROLLED = 256          # enrolment must not be a way to fill the disk
FP_LEN = 32

# A PEM certificate and nothing else -- anchored and bounded. This arrives
# from the network, so it is checked before any of it reaches openssl.
_PEM_RE = re.compile(
    rb"\A-----BEGIN CERTIFICATE-----\n"
    rb"(?:[A-Za-z0-9+/=]{1,80}\n){1,400}"
    rb"-----END CERTIFICATE-----\n?\Z"
)
_MAC_RE = re.compile(r"\A[0-9a-f]{2}(:[0-9a-f]{2}){5}\Z")
_FP_RE = re.compile(rf"\A[0-9a-f]{{{FP_LEN}}}\Z")


class EnrollError(Exception):
    """Rejected. The message is safe to hand back to the caller."""


def fingerprint(cert_pem: bytes) -> str:
    """Stable identity for a certificate.

    Hashes the normalised PEM so trailing-whitespace differences cannot make
    one certificate look like two.
    """
    return hashlib.sha256(cert_pem.strip() + b"\n").hexdigest()[:FP_LEN]


def is_fingerprint(value: str) -> bool:
    """True for something shaped like one of our fingerprints.

    Filenames are built from this, so it is validated as strict lowercase hex.
    A fingerprint can therefore never contain a path separator or `..`, which
    is why no separate traversal defence is needed.
    """
    return bool(_FP_RE.match(value or ""))


def _clean_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    m = mac.strip().lower().replace("-", ":")
    return m if _MAC_RE.match(m) else None


def _clean_label(label: str | None) -> str:
    if not label:
        return ""
    # Printable ASCII only, bounded. This is displayed in listings, so it must
    # not be able to carry control characters or terminal escapes.
    out = "".join(c for c in label if 32 <= ord(c) < 127)
    return out[:64].strip()


def _validate_cert(cert_pem: bytes, openssl_bin: str) -> None:
    if len(cert_pem) > MAX_CERT_BYTES:
        raise EnrollError("certificate too large")
    if not _PEM_RE.match(cert_pem):
        raise EnrollError("not a single PEM certificate")

    # Parse it for real. Well-formed base64 is not necessarily a certificate,
    # and one we cannot parse is one openssl would choke on later -- at encrypt
    # time, mid-request, where the failure is far more expensive to diagnose.
    with tempfile.NamedTemporaryFile(suffix=".crt") as fh:
        fh.write(cert_pem)
        fh.flush()
        parsed = subprocess.run(
            [openssl_bin, "x509", "-in", fh.name, "-noout"],
            capture_output=True, timeout=10,
        )
        if parsed.returncode != 0:
            raise EnrollError("unparseable certificate")
        fresh = subprocess.run(
            [openssl_bin, "x509", "-in", fh.name, "-noout", "-checkend", "0"],
            capture_output=True, timeout=10,
        )
        if fresh.returncode != 0:
            raise EnrollError("certificate has expired")


def enroll(cert_pem: bytes, openssl_bin: str, mac: str | None = None,
           label: str | None = None,
           enroll_dir: pathlib.Path | None = None) -> tuple[str, str]:
    """Register a client certificate. Returns (status, fingerprint).

    status is "enrolled" for a new certificate or "unchanged" when the same one
    is sent again, so reinstalling a client is a no-op rather than an error.
    """
    d = enroll_dir or ENROLL_DIR
    _validate_cert(cert_pem, openssl_bin)

    fp = fingerprint(cert_pem)
    cert_path = d / f"{fp}.crt"
    meta_path = d / f"{fp}.json"
    now = int(time.time())
    status = "unchanged" if cert_path.exists() else "enrolled"

    if status == "enrolled":
        try:
            # Count clients, not certificates: backups also end in .crt and
            # would otherwise halve the effective limit.
            existing = len(enrolled(d))
        except OSError:
            existing = 0
        if existing >= MAX_ENROLLED:
            raise EnrollError("enrolment limit reached; remove unused certificates")

    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o755)

    if status == "enrolled":
        # Atomic: a half-written certificate must never become live.
        tmp = d / f".{fp}.part"
        tmp.write_bytes(cert_pem)
        os.chmod(tmp, 0o644)
        os.replace(tmp, cert_path)

    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
    meta.update({
        "fp": fp,
        "mac": _clean_mac(mac) or meta.get("mac"),
        "label": _clean_label(label) or meta.get("label", ""),
        "first_seen": meta.get("first_seen", now),
        "last_seen": now,
    })
    tmpm = d / f".{fp}.json.part"
    tmpm.write_text(json.dumps(meta, sort_keys=True))
    os.chmod(tmpm, 0o644)
    os.replace(tmpm, meta_path)

    return status, fp


def set_backup(fp: str, cert_pem: bytes, openssl_bin: str,
               enroll_dir: pathlib.Path | None = None) -> str:
    """Attach a backup recipient certificate to an enrolled client.

    CALLERS MUST HAVE PROVEN POSSESSION of this fingerprint's private key
    first. A backup is not like the primary certificate: it is added to the
    client's captures *in addition to* its own key, so a hijacked backup leaves
    the victim's own decryption working perfectly while the attacker quietly
    receives a readable copy. There is no symptom, which is exactly why this
    cannot be trust-on-first-use the way primary enrolment is.

    Scoped to one client on purpose. A global backup would let whoever set it
    read every other client's captures.
    """
    d = enroll_dir or ENROLL_DIR
    if not is_fingerprint(fp):
        raise EnrollError("unrecognised fingerprint")
    if not (d / f"{fp}.crt").is_file():
        raise EnrollError("no such enrolled client")

    _validate_cert(cert_pem, openssl_bin)
    bfp = fingerprint(cert_pem)
    if bfp == fp:
        raise EnrollError("backup certificate must differ from the primary")

    dest = d / f"{fp}.backup.crt"
    tmp = d / f".{fp}.backup.part"
    tmp.write_bytes(cert_pem)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)

    meta_path = d / f"{fp}.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
    meta.update({"backup_fp": bfp, "backup_set": int(time.time())})
    tmpm = d / f".{fp}.json.part"
    tmpm.write_text(json.dumps(meta, sort_keys=True))
    os.chmod(tmpm, 0o644)
    os.replace(tmpm, meta_path)
    return bfp


def backup_for(fp: str, enroll_dir: pathlib.Path | None = None) -> str | None:
    """Path to a client's backup certificate, or None. Hostile-input safe."""
    if not is_fingerprint(fp):
        return None
    p = (enroll_dir or ENROLL_DIR) / f"{fp}.backup.crt"
    return str(p) if p.is_file() else None


def cert_for(fp: str, enroll_dir: pathlib.Path | None = None) -> str | None:
    """Path to an enrolled certificate, or None. Safe against hostile input."""
    if not is_fingerprint(fp):
        return None
    p = (enroll_dir or ENROLL_DIR) / f"{fp}.crt"
    return str(p) if p.is_file() else None


def enrolled(enroll_dir: pathlib.Path | None = None) -> dict[str, str]:
    """Every enrolled fingerprint -> certificate path. Never raises."""
    d = enroll_dir or ENROLL_DIR
    out: dict[str, str] = {}
    try:
        for p in sorted(d.glob("*.crt")):
            # `<fp>.backup.crt` also ends in .crt but is not a client; its stem
            # is "<fp>.backup", which is_fingerprint rejects. Relying on that
            # implicitly would be fragile, so exclude it by name as well.
            if p.name.endswith(".backup.crt"):
                continue
            if is_fingerprint(p.stem):
                out[p.stem] = str(p)
    except OSError:
        pass
    return out


def listing(enroll_dir: pathlib.Path | None = None) -> list[dict]:
    """Enrolled clients with their labels, for human-facing status output."""
    d = enroll_dir or ENROLL_DIR
    rows = []
    for fp in enrolled(d):
        meta = {"fp": fp}
        try:
            meta.update(json.loads((d / f"{fp}.json").read_text()))
        except (OSError, ValueError):
            pass
        rows.append(meta)
    return sorted(rows, key=lambda r: r.get("last_seen", 0), reverse=True)
