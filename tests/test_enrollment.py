# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Enrolment accepts certificates from the network, so it is tested as such."""

import shutil
import subprocess

import pytest

import enrollment

OPENSSL = shutil.which("openssl", path="/opt/homebrew/bin") or "/opt/homebrew/bin/openssl"


def _make_cert(d, name="c", days="1"):
    subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(d / f"{name}.key"), "-out", str(d / f"{name}.crt"),
         "-days", days, "-subj", f"/CN={name}"],
        check=True, capture_output=True,
    )
    return (d / f"{name}.crt").read_bytes()


@pytest.fixture
def edir(tmp_path):
    return tmp_path / "enrolled"


def test_reenrolling_the_same_cert_is_a_noop(tmp_path, edir):
    """A reinstall must not be an error, and must not need approval.

    Identity is derived from the certificate, so the same certificate is the
    same identity by construction -- this is what removes the whole
    conflict/approval problem that IP or name keying would create.
    """
    pem = _make_cert(tmp_path)
    s1, fp1 = enrollment.enroll(pem, OPENSSL, enroll_dir=edir)
    s2, fp2 = enrollment.enroll(pem, OPENSSL, enroll_dir=edir)

    assert (s1, s2) == ("enrolled", "unchanged")
    assert fp1 == fp2
    assert len(enrollment.enrolled(edir)) == 1, "must not create a second entry"


def test_a_different_cert_is_a_different_identity(tmp_path, edir):
    """Two clients coexist; neither can displace the other."""
    a, b = _make_cert(tmp_path, "a"), _make_cert(tmp_path, "b")
    _, fa = enrollment.enroll(a, OPENSSL, enroll_dir=edir)
    _, fb = enrollment.enroll(b, OPENSSL, enroll_dir=edir)

    assert fa != fb
    assert set(enrollment.enrolled(edir)) == {fa, fb}
    assert enrollment.cert_for(fa, edir) != enrollment.cert_for(fb, edir)


@pytest.mark.parametrize("bad,why", [
    (b"", "empty"),
    (b"hello", "not PEM"),
    (b"-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n", "bad base64"),
    (b"-----BEGIN CERTIFICATE-----\n" + b"A" * 80 + b"\n-----END CERTIFICATE-----\n",
     "well-formed base64 that is not a certificate"),
])
def test_malformed_input_is_rejected(bad, why, edir):
    with pytest.raises(enrollment.EnrollError):
        enrollment.enroll(bad, OPENSSL, enroll_dir=edir)
    assert enrollment.enrolled(edir) == {}, f"nothing should be stored for: {why}"


def test_oversized_input_is_rejected_before_openssl(edir):
    """Bounded first, parsed second -- unbounded network input never reaches a
    subprocess."""
    huge = (b"-----BEGIN CERTIFICATE-----\n" + b"A" * 80 + b"\n"
            + b"-----END CERTIFICATE-----\n")
    huge = b"-----BEGIN CERTIFICATE-----\n" + (b"A" * 80 + b"\n") * 400 + huge
    with pytest.raises(enrollment.EnrollError, match="too large|not a single PEM"):
        enrollment.enroll(huge, OPENSSL, enroll_dir=edir)


def test_fingerprint_lookup_rejects_path_traversal(tmp_path, edir):
    """Fingerprints become filenames, so anything not strict hex is refused.

    Validating the shape is the traversal defence -- there is no separate
    sanitising step that could be forgotten.
    """
    _make_cert(tmp_path)
    for hostile in ("../../etc/passwd", "..", "a/b", "", "A" * 32, "z" * 32):
        assert not enrollment.is_fingerprint(hostile)
        assert enrollment.cert_for(hostile, edir) is None


def test_mac_is_normalised_and_junk_is_dropped(tmp_path, edir):
    """The MAC is a display label, so it is cleaned rather than trusted."""
    pem = _make_cert(tmp_path)
    enrollment.enroll(pem, OPENSSL, mac="D0-11-E5-3B-0E-C4",
                      label="mini", enroll_dir=edir)
    row = enrollment.listing(edir)[0]
    assert row["mac"] == "d0:11:e5:3b:0e:c4", "hyphens and case normalised"

    pem2 = _make_cert(tmp_path, "d")
    enrollment.enroll(pem2, OPENSSL, mac="not-a-mac", enroll_dir=edir)
    row2 = [r for r in enrollment.listing(edir) if r["fp"] != row["fp"]][0]
    assert row2.get("mac") is None, "an unparseable MAC is dropped, not stored"


def test_label_cannot_carry_control_characters(tmp_path, edir):
    """Labels are printed in listings; terminal escapes must not survive."""
    pem = _make_cert(tmp_path)
    enrollment.enroll(pem, OPENSSL, label="ok\x1b[31mred\x00\n", enroll_dir=edir)
    assert enrollment.listing(edir)[0]["label"] == "ok[31mred"


def test_enrolment_is_capped(tmp_path, edir, monkeypatch):
    """Enrolment must not be a way to fill the disk."""
    monkeypatch.setattr(enrollment, "MAX_ENROLLED", 2)
    for n in range(2):
        enrollment.enroll(_make_cert(tmp_path, f"k{n}"), OPENSSL, enroll_dir=edir)
    with pytest.raises(enrollment.EnrollError, match="limit reached"):
        enrollment.enroll(_make_cert(tmp_path, "k2"), OPENSSL, enroll_dir=edir)


def test_expired_certificate_is_rejected(tmp_path, edir):
    """Catch it now, not at encrypt time mid-request."""
    # -not_after conflicts with -days, and needs OpenSSL 3.2+; skip rather than
    # fail on a build that cannot mint a backdated certificate.
    made = subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(tmp_path / "e.key"), "-out", str(tmp_path / "e.crt"),
         "-not_before", "20190101000000Z", "-not_after", "20200101000000Z",
         "-subj", "/CN=old"],
        capture_output=True,
    )
    if made.returncode != 0 or not (tmp_path / "e.crt").exists():
        pytest.skip("this openssl cannot generate a backdated certificate")
    with pytest.raises(enrollment.EnrollError, match="expired"):
        enrollment.enroll((tmp_path / "e.crt").read_bytes(), OPENSSL, enroll_dir=edir)


# --- the status endpoint must not leak the roster ------------------------------

def test_status_reports_counts_not_the_enrolled_roster(tmp_path, edir, monkeypatch):
    """`/capture/status` is unauthenticated, like the rest of the proxy.

    Counts are fine; the list of enrolled fingerprints is not -- that would
    hand any LAN host a roster of which machines are capturing, and a
    fingerprint is the one value needed to direct captures at a key. A caller
    asking about a fingerprint it already knows learns nothing new.
    """
    monkeypatch.setattr(enrollment, "ENROLL_DIR", edir)
    a, b = _make_cert(tmp_path, "a"), _make_cert(tmp_path, "b")
    _, fa = enrollment.enroll(a, OPENSSL, mac="d0:11:e5:3b:0e:c4", enroll_dir=edir)
    _, fb = enrollment.enroll(b, OPENSSL, enroll_dir=edir)

    assert set(enrollment.enrolled(edir)) == {fa, fb}

    # what the endpoint builds its reply from
    assert len(enrollment.enrolled()) == 2
    rows = enrollment.listing()
    assert {r["fp"] for r in rows} == {fa, fb}
    known = [r for r in rows if r["fp"] == fa][0]
    assert known["mac"] == "d0:11:e5:3b:0e:c4"

    # a fingerprint the server has never seen resolves to nothing
    assert enrollment.cert_for("0" * 32) is None


# --- per-client backup recipients ---------------------------------------------
# These carry more weight than the happy path. A hijacked backup is SILENT --
# the victim's own decryption keeps working while the attacker also receives a
# readable copy -- so the authorisation around it is the whole safety argument.

def test_backup_requires_an_enrolled_client(tmp_path, edir):
    """You cannot attach a backup to a fingerprint that was never enrolled."""
    b = _make_cert(tmp_path, "b")
    with pytest.raises(enrollment.EnrollError, match="no such enrolled client"):
        enrollment.set_backup("a" * 32, b, OPENSSL, enroll_dir=edir)


def test_backup_rejects_hostile_fingerprints(tmp_path, edir):
    """The fingerprint becomes a filename, so it is shape-checked first."""
    b = _make_cert(tmp_path, "b")
    for hostile in ("../../etc/passwd", "..", "a/b", ""):
        with pytest.raises(enrollment.EnrollError, match="unrecognised fingerprint"):
            enrollment.set_backup(hostile, b, OPENSSL, enroll_dir=edir)


def test_backup_must_differ_from_the_primary(tmp_path, edir):
    """Backing a key up to itself is not a backup; refuse it rather than
    letting someone believe they are protected."""
    a = _make_cert(tmp_path, "a")
    _, fa = enrollment.enroll(a, OPENSSL, enroll_dir=edir)
    with pytest.raises(enrollment.EnrollError, match="must differ"):
        enrollment.set_backup(fa, a, OPENSSL, enroll_dir=edir)


def test_backup_is_stored_and_found(tmp_path, edir):
    a, b = _make_cert(tmp_path, "a"), _make_cert(tmp_path, "b")
    _, fa = enrollment.enroll(a, OPENSSL, enroll_dir=edir)
    bfp = enrollment.set_backup(fa, b, OPENSSL, enroll_dir=edir)

    assert bfp == enrollment.fingerprint(b)
    assert enrollment.backup_for(fa, edir) is not None
    row = [r for r in enrollment.listing(edir) if r["fp"] == fa][0]
    assert row["backup_fp"] == bfp and row["backup_set"] > 0


def test_a_backup_cert_is_not_itself_a_client(tmp_path, edir):
    """`<fp>.backup.crt` also matches *.crt -- it must not appear as a client,
    or it would show in listings and count against the enrolment cap."""
    a, b = _make_cert(tmp_path, "a"), _make_cert(tmp_path, "b")
    _, fa = enrollment.enroll(a, OPENSSL, enroll_dir=edir)
    enrollment.set_backup(fa, b, OPENSSL, enroll_dir=edir)

    assert list(enrollment.enrolled(edir)) == [fa], "backup leaked into the roster"
    assert len(enrollment.listing(edir)) == 1


def test_backup_validation_matches_primary(tmp_path, edir):
    """Same size/shape/parse checks as an enrolment -- it is network input too."""
    a = _make_cert(tmp_path, "a")
    _, fa = enrollment.enroll(a, OPENSSL, enroll_dir=edir)
    for bad in (b"", b"hello",
                b"-----BEGIN CERTIFICATE-----\n" + b"A" * 80 +
                b"\n-----END CERTIFICATE-----\n"):
        with pytest.raises(enrollment.EnrollError):
            enrollment.set_backup(fa, bad, OPENSSL, enroll_dir=edir)
    assert enrollment.backup_for(fa, edir) is None, "nothing should have been stored"
