# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Encrypted capture tests.

The two that matter most:
  * test_no_plaintext_on_disk_* -- the absolute invariant, checked on BOTH the
    success and the openssl-failure paths.
  * test_roundtrip_decrypts     -- proves the actual flag choices
    (-binary, -aes-256-cbc, DER) really produce something decryptable, and is
    the regression guard if anyone "modernises" them to GCM, which the client's
    LibreSSL cannot open.
Run: .venv/bin/python -m pytest tests/test_capture.py -q
"""

import asyncio
import json
import os
import shutil
import subprocess

import pytest

import capture

OPENSSL = "/opt/homebrew/bin/openssl"
MARKER = "UNIQUE-PLAINTEXT-MARKER-8f3a1c"

pytestmark = pytest.mark.skipif(
    not shutil.which(OPENSSL), reason="Homebrew OpenSSL not available"
)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """Throwaway identity. rsa:2048 and -days 1 keep generation fast."""
    d = tmp_path_factory.mktemp("keys")
    subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(d / "t.key"), "-out", str(d / "t.crt"),
         "-days", "1", "-subj", "/CN=capture-test"],
        check=True, capture_output=True,
    )
    return d


def _configure(certs, log_dir, client="10.0.0.9", **over):
    events = []
    cfg = {
        "enabled": True, "openssl_bin": OPENSSL,
        "header": "x-deflector-capture",
        "recipients": {client: str(certs / "t.crt")},
        "cms_timeout_s": 10, **over,
    }
    capture.configure(cfg, lambda rec: events.append(rec))
    return events


def _job(log_dir, certs, **over):
    return {
        "trace_id": "t-cap-1", "ts": 0, "client": "10.0.0.9", "model": "m",
        "certs": [str(certs / "t.crt")], "log_dir": log_dir,
        "request_in": json.dumps({"secret": MARKER}),
        "request_upstream": None, "response_out": None, "truncated": {},
        **over,
    }


def test_roundtrip_decrypts(certs, isolate_log_dir):
    """Encrypt as the daemon does, then decrypt with the private key."""
    events = _configure(certs, isolate_log_dir)
    asyncio.run(capture._encrypt_and_write(_job(isolate_log_dir, certs)))

    blobs = list((isolate_log_dir / "capture").rglob("*.cms"))
    assert len(blobs) == 1
    out = subprocess.run(
        [OPENSSL, "cms", "-decrypt", "-inform", "DER", "-in", str(blobs[0]),
         "-recip", str(certs / "t.crt"), "-inkey", str(certs / "t.key")],
        capture_output=True, check=True,
    ).stdout
    payload = json.loads(out)
    assert json.loads(payload["request_in"])["secret"] == MARKER
    assert payload["v"] == 1
    assert any(e["ev"] == "capture_ok" for e in events)


def test_no_plaintext_on_disk_success_path(certs, isolate_log_dir):
    _configure(certs, isolate_log_dir)
    asyncio.run(capture._encrypt_and_write(_job(isolate_log_dir, certs)))
    _assert_marker_absent(isolate_log_dir)


def test_no_plaintext_on_disk_when_openssl_fails(certs, isolate_log_dir):
    """The invariant must hold on the ERROR path too.

    /usr/bin/false exits non-zero and emits nothing -- the code must write no
    file at all rather than falling back to anything unencrypted.
    """
    events = _configure(certs, isolate_log_dir, openssl_bin="/usr/bin/false")
    # configure() rejects a non-OpenSSL binary outright; force the job through
    # to prove the writer itself also refuses.
    capture._openssl = "/usr/bin/false"
    asyncio.run(capture._encrypt_and_write(_job(isolate_log_dir, certs)))

    assert not list((isolate_log_dir / "capture").rglob("*.cms"))
    assert not list((isolate_log_dir / "capture").rglob("*.part"))
    _assert_marker_absent(isolate_log_dir)


def _assert_marker_absent(log_dir):
    for p in log_dir.rglob("*"):
        if not p.is_file():
            continue
        assert MARKER not in p.read_bytes().decode("utf-8", "replace"), \
            f"plaintext marker leaked into {p}"


def test_unknown_client_cannot_capture(certs, isolate_log_dir):
    """The binding that makes the whole security argument true."""
    _configure(certs, isolate_log_dir, client="10.0.0.9")
    headers = {"x-deflector-capture": "1"}
    assert capture.wants_capture(headers, "10.0.0.9") is True
    assert capture.wants_capture(headers, "10.0.0.99") is False   # not in map
    assert capture.recipient_for("10.0.0.99") is None


def test_header_absent_means_no_capture(certs, isolate_log_dir):
    _configure(certs, isolate_log_dir)
    assert capture.wants_capture({}, "10.0.0.9") is False


def test_disabled_config_never_captures(certs, isolate_log_dir):
    _configure(certs, isolate_log_dir, enabled=False)
    assert capture.wants_capture({"x-deflector-capture": "1"}, "10.0.0.9") is False


def test_libressl_is_rejected_as_the_encrypt_binary(isolate_log_dir):
    """The daemon-PATH trap, as a test.

    Under a LaunchDaemon's minimal PATH, bare `openssl` resolves to
    /usr/bin/openssl = LibreSSL, which cannot produce what the client needs --
    silently. configure() must refuse it rather than encrypt with it.
    """
    events = []
    capture.configure({"enabled": True, "openssl_bin": "/usr/bin/openssl",
                       "recipients": {}}, lambda r: events.append(r))
    assert capture._openssl is None
    assert any(e["ev"] == "capture_disabled"
               and e["reason"] == "openssl_unusable" for e in events)


def test_missing_cert_disables_that_recipient(tmp_path, isolate_log_dir):
    events = []
    capture.configure({"enabled": True, "openssl_bin": OPENSSL,
                       "recipients": {"10.0.0.9": str(tmp_path / "nope.crt")}},
                      lambda r: events.append(r))
    assert capture.recipient_for("10.0.0.9") is None
    assert any(e["ev"] == "capture_recipient_skipped" for e in events)


def test_backup_recipient_can_also_decrypt(certs, tmp_path, isolate_log_dir):
    """Key-loss insurance: a second cert must open the same blob."""
    subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(tmp_path / "b.key"), "-out", str(tmp_path / "b.crt"),
         "-days", "1", "-subj", "/CN=backup"],
        check=True, capture_output=True,
    )
    _configure(certs, isolate_log_dir)
    job = _job(isolate_log_dir, certs)
    job["certs"] = [str(certs / "t.crt"), str(tmp_path / "b.crt")]
    asyncio.run(capture._encrypt_and_write(job))

    blob = next((isolate_log_dir / "capture").rglob("*.cms"))
    for name in ("b", ):
        out = subprocess.run(
            [OPENSSL, "cms", "-decrypt", "-inform", "DER", "-in", str(blob),
             "-recip", str(tmp_path / f"{name}.crt"),
             "-inkey", str(tmp_path / f"{name}.key")],
            capture_output=True, check=True,
        ).stdout
        assert MARKER in out.decode()


def test_clip_bounds_and_flags_truncation():
    val, trunc = capture.clip("x" * 100, 10)
    assert len(val) == 10 and trunc is True
    val, trunc = capture.clip("short", 10)
    assert val == "short" and trunc is False
    assert capture.clip(None, 10) == (None, False)


def test_enqueue_never_raises_when_queue_full(certs, isolate_log_dir):
    """Called from a teardown `finally`; must never propagate."""
    _configure(certs, isolate_log_dir)

    async def scenario():
        capture._queue = asyncio.Queue(maxsize=1)
        capture.enqueue({"trace_id": "a"})
        capture.enqueue({"trace_id": "b"})   # over capacity -> dropped, not raised
        assert capture._queue.qsize() == 1

    asyncio.run(scenario())


def test_non_streaming_path_still_captures(certs, isolate_log_dir):
    """Regression: `stream: false` used to accept the header and drop everything.

    The non-streaming branch returns a JSONResponse and never builds a
    _traced_stream, so before _finish_capture was shared it produced neither a
    blob nor a skip reason -- the exact ambiguity the skip logging prevents.
    """
    import main
    import tracing
    _configure(certs, isolate_log_dir)
    tracing.start("t-nonstream")
    ctx = tracing.get()
    ctx["cap_certs"] = [str(certs / "t.crt")]
    ctx["cap_request_in"] = json.dumps({"secret": MARKER})
    ctx["cap_truncated"] = {}
    ctx["cap_client"] = "10.0.0.9"

    async def scenario():
        capture._queue = asyncio.Queue(maxsize=4)
        main._finish_capture("m", '{"resp":"x"}', 12)
        assert capture._queue.qsize() == 1, "non-streaming request was not enqueued"
        job = capture._queue.get_nowait()
        assert job["request_in"] == ctx["cap_request_in"]

    asyncio.run(scenario())


def test_claude_branch_records_upstream(certs, isolate_log_dir):
    """Regression: the Claude branch returned before the upstream snapshot.

    That is the cloud-egress path, so every Claude capture carried a null
    request_upstream -- exactly where proving the privacy pipeline matters.
    """
    import main
    import tracing
    _configure(certs, isolate_log_dir)
    tracing.start("t-claude")
    ctx = tracing.get()
    ctx["cap_certs"] = [str(certs / "t.crt")]
    ctx["cap_truncated"] = {}

    main._capture_upstream({"model": "claude-haiku-4-5", "messages": []})
    assert ctx["cap_request_upstream"] is not None
    assert "claude-haiku-4-5" in ctx["cap_request_upstream"]


# --- client-side decrypt mechanism -------------------------------------------
# The decrypt script had NO test coverage at all. These cover the two things
# that actually proved fragile in testing, without touching the operator's real
# Keychain (which cannot even be written to outside a GUI login session).

def test_decrypt_accepts_key_from_nonseekable_pipe(certs, isolate_log_dir, tmp_path):
    """scripts/decrypt-capture.sh passes the key as `-inkey <(...)`.

    That is a /dev/fd pipe, not a real file. PEM parsing is sequential so it
    works, but `-inkey` normally takes a seekable path -- this is the assumption
    the whole key-never-on-disk design rests on.
    """
    blob = tmp_path / "b.cms"
    subprocess.run(
        [OPENSSL, "cms", "-encrypt", "-binary", "-aes-256-cbc", "-outform", "DER",
         "-out", str(blob), str(certs / "t.crt")],
        input=json.dumps({"marker": MARKER}).encode(), check=True, capture_output=True,
    )
    # bash process substitution, exactly as the script does it
    out = subprocess.run(
        ["/bin/bash", "-c",
         f'{OPENSSL} cms -decrypt -inform DER -in {blob} '
         f'-recip {certs}/t.crt -inkey <(cat {certs}/t.key)'],
        capture_output=True, check=True,
    ).stdout
    assert MARKER in out.decode()


def test_base64_is_required_for_keychain_storage(certs):
    """`security -w` hex-encodes any value that is not plain printable text.

    A PEM contains newlines, so storing it raw round-trips as HEX and silently
    corrupts the key -- observed as 3272 bytes in, 6543 bytes out. Base64 makes
    it one printable line, which round-trips byte-exactly. This test pins the
    property base64 provides, without needing a Keychain.
    """
    import base64 as b64
    pem = (certs / "t.key").read_bytes()
    assert b"\n" in pem, "a PEM is multi-line -- that is what trips the hex path"

    encoded = b64.b64encode(pem).decode()
    assert "\n" not in encoded, "stored value must be a single printable line"
    assert b64.b64decode(encoded) == pem, "base64 round trip must be byte-exact"


def test_decrypt_distinguishes_locked_keychain_from_missing_item(certs, tmp_path):
    """A locked keychain must not be reported as a missing item.

    Both surface as a failed `find-generic-password`, but the fixes are
    opposite: one says "import your key", the other says "unlock". Sending the
    operator hunting for an item that is sitting right there is the expensive
    failure, so stub `security` to emit the locked error and pin the branch --
    a real locked-read cannot be staged without writing to the real Keychain.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "security").write_text(
        '#!/bin/bash\n'
        'echo "security: SecKeychainSearchCopyNext: User interaction is not allowed." >&2\n'
        'exit 1\n'
    )
    (stub / "security").chmod(0o755)

    blob = tmp_path / "b.cms"
    blob.write_bytes(b"\x30\x00")  # never parsed; the script exits first

    res = subprocess.run(
        ["/bin/bash", "scripts/decrypt-capture.sh", str(blob),
         "--cert", str(certs / "t.crt")],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
    )
    assert res.returncode == 1
    assert "locked or unreachable" in res.stderr
    assert "unlock-keychain" in res.stderr
    assert "no Keychain item" not in res.stderr, "misdiagnosed a lock as a missing item"


def test_verify_refuses_when_stored_value_does_not_match(certs, tmp_path):
    """`--verify` is the only thing standing between a bad import and `rm -P`.

    The GUI path has no write step to check, so a truncated paste is entirely
    plausible -- and deleting the key file against a corrupt Keychain copy
    destroys every capture encrypted to it, permanently and silently. Stub
    `security` to return a truncated value and assert the script refuses.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "security").write_text(
        '#!/bin/bash\necho "dHJ1bmNhdGVk"\n'  # valid base64, wrong bytes
    )
    (stub / "security").chmod(0o755)

    res = subprocess.run(
        ["/bin/bash", "scripts/import-capture-key.sh", "--verify", str(certs / "t.key")],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
    )
    assert res.returncode == 1
    assert "MISMATCH" in res.stderr
    assert "Do NOT delete the key file" in res.stderr


# --- backup recipients must be reported honestly ------------------------------

def test_missing_backup_cert_is_reported_not_silently_dropped(certs, isolate_log_dir):
    """A backup path that does not resolve must not read as a working backup.

    `backups` previously counted the CONFIGURED list, so a typo'd path still
    logged `backups: 1` while encrypting to the primary key alone -- hiding the
    exact failure the field exists to surface, in the number the docs tell you
    to check.
    """
    events = _configure(certs, isolate_log_dir,
                        backup_recipients=[str(certs / "does-not-exist.crt")])
    evs = {e["ev"]: e for e in events}

    assert "capture_backup_missing" in evs, "a missing backup cert must be announced"
    assert evs["capture_ready"]["backups"] == 0, "must count LOADED backups, not configured"
    assert "capture_no_backup" in evs, "zero effective backups is a warning condition"
    assert len(capture.recipient_for("10.0.0.9")) == 1, "only the primary cert is usable"


def test_working_backup_is_counted_and_used(certs, isolate_log_dir):
    """The positive case: a resolvable backup is loaded and encrypted to."""
    events = _configure(certs, isolate_log_dir,
                        backup_recipients=[str(certs / "t.crt")])
    evs = {e["ev"]: e for e in events}

    assert evs["capture_ready"]["backups"] == 1
    assert "capture_no_backup" not in evs, "do not warn when a backup is present"
    assert len(capture.recipient_for("10.0.0.9")) == 2, "primary + backup"
