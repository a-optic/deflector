# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Opt-in, end-to-end encrypted request capture.

WHY THIS SHAPE
--------------
The server encrypts to a PUBLIC certificate and holds no private key, so it is
structurally incapable of reading what it wrote. The private key lives on the
client machine. The reasoning that makes this sound: a client already possesses
everything in its own request and response, so encrypting that data back to
that client's key discloses nothing new to the key holder -- while removing the
server's ability to read it.

That argument only holds if requester == key holder, which is why the recipient
is derived from `request.client.host` and NOT from a header. A header-only
opt-in would let any host on the LAN have its traffic encrypted to somebody
else's key.

VERIFIED CRYPTO FACTS (do not "modernise" without re-testing)
-------------------------------------------------------------
* AES-256-CBC, not GCM. The client decrypts with macOS's bundled LibreSSL
  3.3.6, whose `cms` supports only CBC ciphers -- no GCM, no AuthEnvelopedData.
  GCM would produce blobs the client cannot open.
* Decryption uses `openssl`, not Apple's `security cms -D`. The latter failed
  even on its own `-E` output during testing, with -8147
  (SEC_ERROR_NOT_A_RECIPIENT) -- a key-LOOKUP failure, not a format one: the
  identity sat in a temp keychain outside the decoder's search list. The
  client's private key is Keychain-held either way; only the crypto tool
  differs. See scripts/decrypt-capture.sh.
* `-binary` is mandatory: without it CMS applies MIME canonicalisation (CRLF
  translation) and silently corrupts the payload.
* The openssl binary is resolved by ABSOLUTE PATH. Under a LaunchDaemon's
  minimal PATH, bare `openssl` resolves to /usr/bin/openssl = LibreSSL 3.3.6 --
  the very build that cannot produce what we need -- silently and with no error.

INVARIANT
---------
Plaintext never touches disk. It exists only as a Python bytes object and in
the subprocess's stdin pipe. No code path opens a file before verified
ciphertext is in hand.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib

import enrollment
import subprocess
import time

_MAGIC_DER_SEQUENCE = 0x30

_queue: asyncio.Queue | None = None
_workers: list[asyncio.Task] = []
_recipients: dict[str, list[str]] = {}   # client ip -> [cert paths]
_backups: list[str] = []                 # resolved backup certs, appended to every recipient
_openssl: str | None = None
_cfg: dict = {}
_log = None                              # injected metadata logger


def _note(ev: str, **fields) -> None:
    if _log:
        _log({"ev": ev, **fields})


def _verify_openssl(path: str) -> str | None:
    """Return the version string iff `path` is a real OpenSSL (not LibreSSL)."""
    try:
        out = subprocess.run([path, "version"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    return out if out.startswith("OpenSSL") else None


def configure(cfg: dict, log_fn) -> None:
    """Validate configuration ONCE at startup rather than per request.

    Fails closed and loudly: any problem here disables capture entirely rather
    than risking a per-request surprise.
    """
    global _recipients, _openssl, _cfg, _log, _backups
    _cfg, _log, _recipients, _openssl, _backups = cfg or {}, log_fn, {}, None, []

    if not _cfg.get("enabled", False):
        return

    binary = _cfg.get("openssl_bin", "/opt/homebrew/bin/openssl")
    version = _verify_openssl(binary)
    if not version:
        _note("capture_disabled", reason="openssl_unusable", bin=binary)
        return
    _openssl = binary

    # Resolve backups ONCE, ahead of the recipient loop -- they are identical
    # for every client, and a missing one has to be reported rather than
    # skipped in silence. An unusable backup is indistinguishable from no
    # backup at the moment you actually need it.
    backups = _backups
    for extra in (_cfg.get("backup_recipients") or []):
        e = pathlib.Path(os.path.expanduser(extra))
        if e.is_file():
            backups.append(str(e))
        else:
            _note("capture_backup_missing", path=str(e), reason="cert_missing")

    for ip, cert in (_cfg.get("recipients") or {}).items():
        p = pathlib.Path(os.path.expanduser(cert))
        if not p.is_file():
            _note("capture_recipient_skipped", client=ip, reason="cert_missing")
            continue
        _recipients[ip] = [str(p), *backups]

    # `backups` is the count LOADED, never the count configured. Reporting the
    # configured length meant a typo'd path still read as `backups: 1`, hiding
    # the one failure this field exists to surface -- and it is the field the
    # docs tell operators to check.
    _note("capture_ready", version=version,
          recipients=sorted(_recipients), backups=len(backups))
    if _recipients and not backups:
        _note("capture_no_backup", severity="warning",
              detail="every capture is encrypted to a single key; if that key is "
                     "lost these files become permanently unreadable")


def backup_certs() -> list[str]:
    """Backup certs that actually resolved. Counted for status output."""
    return list(_backups)


def recipient_for(client_ip: str | None, fp: str | None = None) -> list[str] | None:
    """Certs to encrypt to for this caller, or None if it may not capture.

    A fingerprint wins over the IP map. Enrolled certs are resolved on every
    lookup rather than cached at startup, so a client that enrols is usable
    immediately -- requiring a restart would make "install the client and it
    works" false.
    """
    if not _openssl:
        return None
    if fp:
        cert = enrollment.cert_for(fp)
        if not cert:
            return None
        # This client's own backup first, then any operator-configured global
        # backups. Both are appended to the primary, so either key opens it.
        own_backup = enrollment.backup_for(fp)
        return [cert] + ([own_backup] if own_backup else []) + _backups
    if not client_ip:
        return None
    return _recipients.get(client_ip)


def capture_fp(headers) -> str | None:
    """The enrolled fingerprint this request asks for, if any."""
    header = _cfg.get("header", "x-deflector-capture")
    v = (headers.get(header) or "").strip().lower()
    return v if enrollment.is_fingerprint(v) else None


def wants_capture(headers, client_ip: str | None) -> bool:
    """Capture only when the caller asks AND we hold a cert it can name.

    Two ways to ask: a fingerprint (enrolled, DHCP-proof) or the legacy
    truthy value, which still resolves through the IP map so existing
    configurations keep working untouched.
    """
    if not _cfg.get("enabled", False):
        return False
    header = _cfg.get("header", "x-deflector-capture")
    v = (headers.get(header) or "").strip().lower()
    if enrollment.is_fingerprint(v):
        return recipient_for(None, fp=v) is not None
    if v not in ("1", "true", "yes", "on"):
        return False
    return recipient_for(client_ip) is not None


async def start_workers() -> None:
    global _queue
    if not _openssl:
        return
    _queue = asyncio.Queue(maxsize=int(_cfg.get("queue_maxsize", 32)))
    for _ in range(int(_cfg.get("workers", 2))):
        _workers.append(asyncio.create_task(_worker()))


async def stop_workers() -> None:
    for t in _workers:
        t.cancel()
    _workers.clear()


def enqueue(job: dict) -> None:
    """Hand a finished request to the encrypt pool. Never blocks, never raises.

    `put_nowait`, not `await put()`: this is called from a generator's `finally`
    during teardown, where an await can be re-cancelled before completing --
    the same hazard the shielded cleanup in main._dedup_stream exists for.
    """
    if _queue is None:
        return
    try:
        _queue.put_nowait(job)
    except asyncio.QueueFull:
        # Bounded on purpose: dropping a debug artifact is strictly better than
        # growing unbounded memory holding ~157KB buffers under a retry storm.
        _note("capture_drop", id=job.get("trace_id"), reason="queue_full")


async def _worker() -> None:
    while True:
        job = await _queue.get()
        try:
            await _encrypt_and_write(job)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            _note("capture_fail", id=job.get("trace_id"), reason=type(exc).__name__)
        finally:
            _queue.task_done()


async def _encrypt_and_write(job: dict) -> None:
    plaintext = json.dumps({
        "v": 1,
        "trace_id": job["trace_id"],
        "ts": job["ts"],
        "client": job.get("client"),
        "model": job.get("model"),
        # request_in is the PRISTINE body; request_upstream is what actually
        # left the box after routing/redaction. Both together are what let you
        # prove the privacy pipeline behaved.
        "request_in": job.get("request_in"),
        "request_upstream": job.get("request_upstream"),
        "response_out": job.get("response_out"),
        "truncated": job.get("truncated", {}),
    }, ensure_ascii=False).encode()

    proc = await asyncio.create_subprocess_exec(
        _openssl, "cms", "-encrypt", "-binary", "-aes-256-cbc",
        "-outform", "DER", *job["certs"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # communicate() pumps both pipes concurrently. Writing stdin then
        # reading stdout deadlocks past the ~64KB pipe buffer, and real bodies
        # here reach 157KB.
        ct, _err = await asyncio.wait_for(
            proc.communicate(plaintext),
            timeout=float(_cfg.get("cms_timeout_s", 10)),
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()                      # reap, or it lingers as a zombie
        _note("capture_fail", id=job["trace_id"], reason="timeout")
        return
    finally:
        plaintext = b""                        # drop the buffer promptly

    if proc.returncode != 0 or not ct or ct[0] != _MAGIC_DER_SEQUENCE:
        _note("capture_fail", id=job["trace_id"],
              reason=f"openssl_rc{proc.returncode}")
        return

    await asyncio.to_thread(_atomic_write, job["log_dir"], job["trace_id"], ct)
    _note("capture_ok", id=job["trace_id"], bytes=len(ct),
          sha256=hashlib.sha256(ct).hexdigest())


def _atomic_write(log_dir: pathlib.Path, trace_id: str, ciphertext: bytes) -> None:
    """Write ciphertext only, atomically.

    `.part` then `os.replace` means a crash mid-write leaves a partial file
    with a name nothing reads, never a truncated `.cms` that looks complete.
    Explicit 0600/0700 rather than inheriting the daemon's umask.
    """
    day = time.strftime("%Y-%m-%d", time.gmtime())
    d = log_dir / "capture" / day
    d.mkdir(parents=True, exist_ok=True)
    # Both levels: the parent would otherwise inherit the ambient umask (0755).
    os.chmod(d.parent, 0o700)
    os.chmod(d, 0o700)
    final = d / f"{trace_id}.cms"
    tmp = d / f"{trace_id}.cms.part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ciphertext)
    finally:
        os.close(fd)
    os.replace(tmp, final)


def clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Bound a captured field. Returns (value, was_truncated)."""
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True
