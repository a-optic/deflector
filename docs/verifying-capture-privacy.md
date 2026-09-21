<!-- This Source Code Form is subject to the terms of the Mozilla Public
     License, v. 2.0. If a copy of the MPL was not distributed with this
     file, You can obtain one at https://mozilla.org/MPL/2.0/. -->

# Verifying that captures are actually private

Twelve tests you can run yourself, by hand, to confirm the encryption does what it claims —
without trusting `capture-selftest.sh` or anything else that was written for you.

Each test lists the exact command and the output that means **PASS**. Every output below was
observed on a live setup, not written from expectation.

> **Read [What this does *not* protect](#what-this-does-not-protect) before you rely on any of
> it.** The tests prove a specific, narrow property: *captures on disk are readable only by the
> client that holds the key*. Several things people assume are covered are not.

Throughout: **CLIENT** = the machine holding the private key (the one running your agent).
**SERVER** = the machine running Deflector.

---

## Setup

Pick a marker so you can tell your own request apart from anything else:

```bash
MARK="AUDIT-$(date +%s)"; echo "$MARK"
```

Send one request **from the CLIENT** asking for a capture. `<your-capture-id>` is the fingerprint from `agentstop enroll`, stored in `~/.agentstop/client.json`:

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
  -X POST http://<server>:11500/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Deflector-Capture: <your-capture-id>' \
  -d "{\"model\":\"lfm2.5:latest\",\"stream\":false,\"max_tokens\":16,
       \"messages\":[{\"role\":\"user\",\"content\":\"Say OK. Marker $MARK\"}]}"
```

Then, **on the SERVER**, get the newest blob:

```bash
BLOB=$(ls -t ~/.agentstop/logs/capture/*/*.cms | head -1); echo "$BLOB"
```

---

## Test 1 — The server holds no private key

**On the SERVER:**

```bash
grep -rl -- '^-----BEGIN [A-Z ]*PRIVATE KEY-----$' ~/.agentstop 2>/dev/null | wc -l
```

**PASS:** `0`

Anchor the pattern to a full header line. A loose `-----BEGIN .*PRIVATE KEY` also matches scripts
that *mention* it, which produced a false positive during development.

## Test 2 — The key is not on disk on the client either

**On the CLIENT:**

```bash
ls -l ~/.agentstop/keys/
grep -rl -- '^-----BEGIN [A-Z ]*PRIVATE KEY-----$' ~ 2>/dev/null | head
```

**PASS:** only `pi.crt` is listed, and the grep returns nothing. The key exists solely as a
Keychain item — confirm with **Keychain Access ▸ login ▸ Passwords ▸ `deflector-capture-key`**.

## Test 3 — The blob is opaque

**On the SERVER:**

```bash
file -b "$BLOB"
xxd -l 32 "$BLOB"
grep -c -a -E 'lfm2\.5|messages|assistant' "$BLOB"
grep -c -a "$MARK" "$BLOB"
```

**PASS:**

```
data
00000000: 3082 06a1 0609 2a86 4886 f70d 0107 03a0  0.....*.H.......
0
0
```

`30 82` is the DER SEQUENCE header, and `2a 86 48 86 f7 0d 01 07 03` is the OID for CMS
enveloped-data. Model names, role names and your marker are all absent.

## Test 4 — The server cannot decrypt it

**On the SERVER**, three attempts:

```bash
/opt/homebrew/bin/openssl cms -decrypt -inform DER -in "$BLOB"
/opt/homebrew/bin/openssl cms -decrypt -inform DER -in "$BLOB" -recip ~/.agentstop/keys/*.crt
```

**PASS:**

```
No recipient certificate or key specified
Could not find private key of signing key from …/mini-capture.crt
```

The server has the certificate — the public half — and that is provably not enough.

## Test 5 — A different key cannot open it *(the attacker test)*

This is the one that matters most: Test 4 could fail merely because no key was supplied. Here a
**valid, well-formed key** is supplied and still rejected.

**On the SERVER:**

```bash
openssl req -x509 -newkey rsa:4096 -nodes -keyout /tmp/evil.key -out /tmp/evil.crt \
  -days 1 -subj "/CN=not-your-key" 2>/dev/null
/opt/homebrew/bin/openssl cms -decrypt -inform DER -in "$BLOB" \
  -recip /tmp/evil.crt -inkey /tmp/evil.key
rm -f /tmp/evil.key /tmp/evil.crt
```

**PASS:** `Error decrypting CMS using private key`

## Test 6 — Your certificate is the only recipient

**On the SERVER:**

```bash
/opt/homebrew/bin/openssl cms -cmsout -noout -print -inform DER -in "$BLOB" \
  | grep -A4 recipientInfos
```

**PASS:** exactly one `d.issuerAndSerialNumber`, whose `serialNumber` matches your certificate.
Compare on the **CLIENT** with:

```bash
python3 -c "print(int('$(openssl x509 -in ~/.agentstop/keys/pi.crt -noout -serial | cut -d= -f2)',16))"
```

`-inform DER` is required. Without it, openssl assumes S/MIME, finds nothing, prints nothing, and
**exits 0** — a check that silently passes while proving nothing.

## Test 7 — You can decrypt it

Copy the blob to the CLIENT, then in **Terminal at the client's own screen** (the Keychain read
needs a GUI session):

```bash
~/.agentstop/bin/decrypt-capture.sh blob.cms | python3 -m json.tool | head -20
```

**PASS:** readable JSON with `client`, `model`, `trace_id`, `request_in`, `request_upstream`,
`response_out`.

## Test 8 — The plaintext is really yours

```bash
~/.agentstop/bin/decrypt-capture.sh blob.cms | grep -c "$MARK"
```

**PASS:** `1` or more. Without this, Test 7 only proves you decrypted *something*.

Also compare `request_in` (what you sent) against `request_upstream` (what actually left the box
after redaction). That difference is the privacy pipeline's audit trail.

## Test 9 — No header, no capture

**From the CLIENT**, repeat the setup request with the `X-Deflector-Capture` header removed, then
**on the SERVER:**

```bash
ls ~/.agentstop/logs/capture/*/*.cms | wc -l
```

**PASS:** the count is unchanged. Capture is opt-in per request, never ambient.

## Test 10 — An unlisted IP cannot capture

Send a request **with** the header from any machine *not* in `recipients` — the server itself
works (`127.0.0.1`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:11500/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Deflector-Capture: <your-capture-id>' \
  -d '{"model":"lfm2.5:latest","stream":false,"max_tokens":8,
       "messages":[{"role":"user","content":"UNAUTHORISED"}]}'
ls ~/.agentstop/logs/capture/*/*.cms | wc -l
```

**PASS:** `HTTP 200`, count unchanged. This is why recipients are keyed by **client IP** rather
than by the header: otherwise any host on the LAN could have its traffic encrypted to *your* key,
or have yours encrypted to a key it controls.

## Test 11 — Prompt text does not leak into the metadata logs

The `.jsonl` logs beside the captures are **not** encrypted. Confirm they carry no content:

```bash
grep -rl "$MARK" ~/.agentstop/logs/*.jsonl
```

**PASS:** no output. A metadata record looks like this — counts and timings only:

```json
{"ts": 1787253914.7, "ev": "body_read", "id": "1787253914714-a017776ce5eb2ef7",
 "dur": 0.0, "model": "lfm2.5:latest", "bytes": 125, "messages": 1, "tools": 0, "slots": 1}
```

## Test 12 — Permissions and retention

**On the SERVER:**

```bash
ls -ld ~/.agentstop/logs/capture ~/.agentstop/logs/capture/*/
ls -l "$BLOB"
grep retention_ config.yaml
```

**PASS:** directories `drwx------` (0700), blobs `-rw-------` (0600), and:

```
retention_metadata_days: 14
retention_capture_days: 7
```

Encrypted captures are swept after 7 days, metadata after 14. The sweep runs at startup and
hourly.

---

## The automated version

```bash
~/.agentstop/bin/capture-selftest.sh
```

Runs the core of the above — blob exists, server cannot read it, you can, marker matches,
recipient serial is yours — and prints `RESULT: PASS` only if all hold. Use the manual tests when
you want to see it for yourself rather than take a script's word.

---

## What this does *not* protect

Worth being blunt, because several of these are commonly assumed:

- **The network is plain HTTP.** Requests travel to Deflector unencrypted over your LAN. Anyone
  who can sniff that segment reads your prompts in full, regardless of capture. Encryption here
  protects logs *at rest*, not in transit.
- **The server sees everything while it runs.** Deflector is a proxy — it holds your plaintext in
  memory and forwards it upstream. The threat this addresses is someone reading *stored captures*
  later (a backup, a stolen disk, another account on the box), not the server during the request.
- **Upstream providers see what they are sent.** Cloud-routed requests go to Ollama Cloud or
  OpenRouter with only the redaction pipeline in front of them.
- **Metadata is not encrypted.** Model names, byte counts, timings, client IPs and trace ids sit
  in plain `.jsonl` for 14 days. That is a real privacy surface even with no prompt content.
- **The Keychain key is extractable.** It is read out to be used, so anything running as you on
  the client with keychain access can obtain it. The `-T /usr/bin/security` ACL narrows this; it
  does not eliminate it.
- **One key, no backup.** With `backup_recipients` empty, losing the Keychain item makes every
  existing capture permanently unreadable — silently, because writes keep succeeding. Configure a
  backup *before* captures start mattering; it cannot be applied retroactively.
