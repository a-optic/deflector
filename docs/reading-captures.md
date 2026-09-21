<!-- This Source Code Form is subject to the terms of the Mozilla Public
     License, v. 2.0. If a copy of the MPL was not distributed with this
     file, You can obtain one at https://mozilla.org/MPL/2.0/. -->

# Reading your captures on the client

Where the files live, how to read one, and how to confirm the server cannot.

Related: [setting up a client](macos-client-capture-setup.md) ·
[auditing the privacy claim](verifying-capture-privacy.md)

---

## Do I need the command line, or can I do this in a GUI?

**You need a terminal — but what actually matters is the *session*, not the tool.**

No macOS GUI can decrypt a CMS file. Keychain Access cannot, Finder cannot, Passwords.app cannot.
`openssl` is the only thing on the system that does it, so a terminal is unavoidable.

The real constraint is *where* you run it:

| Where you run it | Works? | Why |
| --- | --- | --- |
| **Terminal.app at the client's own screen** | **yes** | Aqua session — your login keychain is unlocked |
| Terminal via Screen Sharing *into* the client | yes | still the Aqua session |
| **ssh into the client** | **no** | Background session — keychain reads fail with `User interaction is not allowed` |

So: you must type commands, and you must be sitting at the client to do it.

One-line check before anything else — if this does not say `Aqua`, nothing below will work:

```bash
launchctl managername
```

There are real GUI steps for *confirming* the encryption
([below](#what-you-can-check-without-a-terminal)). They just cannot undo it.

---

## Where everything lives

### On the SERVER (the machine running Deflector)

| Path | Encrypted? | Retention |
| --- | --- | --- |
| `~/.agentstop/logs/capture/<date>/<trace-id>.cms` | **yes** — 0600, dir 0700 | 7 days |
| `~/.agentstop/logs/requests-<date>.jsonl` | no — metadata only | 14 days |
| `~/.agentstop/logs/routing-<date>.jsonl` | no — metadata only | 14 days |
| `~/.agentstop/logs/kills-<date>.jsonl` | no — metadata only | 14 days |
| `~/.agentstop/logs/lifeos-escalations-<date>.jsonl` | no — metadata only | 14 days |
| `~/.agentstop/logs/stdout.log`, `stderr.log` | no — uvicorn output | held open by launchd |
| `~/.agentstop/keys/<client>-capture.crt` | public certificate | — |

**Only the `.cms` files are encrypted.** Everything else in that directory is plain text.

Note the permissions difference: `capture/` is `drwx------`, but `logs/` itself is `drwxr-xr-x` —
so any other local account on the server can read the metadata, though not the captures.

### On the CLIENT (the machine that reads them)

| Path | What it is |
| --- | --- |
| login Keychain item `deflector-capture-key` | **the private key** — the only copy |
| `~/.agentstop/keys/pi.crt` | public certificate |
| `~/.agentstop/bin/decrypt-capture.sh` | the reader |
| `~/.agentstop/bin/capture-selftest.sh` | automated end-to-end check |
| `~/.agentstop/bin/agentstop` | the client CLI (`enroll`, `status`, `read`, `backup`) |
| `~/.agentstop/client.json` | server address, ssh target and this Mac's capture id |
| `~/.agentstop/capture-selftest.conf` | local server address; not in git |

There is deliberately **no `.key` file**. If you find one, the setup is not finished.

---

## Two different things called "logs"

| You want | Command | Needs the Keychain? |
| --- | --- | --- |
| **Captures** — the actual request and response text, encrypted | `agentstop read` | yes, so at the Mac's screen |
| **Trace logs** — timings, outcomes, routing, stalls | `agentstop logs` | no, works over ssh too |

For *"why was that slow"*, *"did it stall"*, *"which model did it really route to"*, you want
`agentstop logs`. For *"what exactly was sent"*, you want `agentstop read`.

```bash
agentstop logs                  # last 20 requests, one line each
agentstop logs --summary        # outcome / model / kill rollup
agentstop logs --stalls         # requests that never completed
agentstop logs --id <trace>     # everything known about one request
agentstop logs --follow         # tail live
```

Every flag is passed straight through to the server's `deflector-logs.py`, so its full interface
works. It runs over ssh, so `client.json` needs an `ssh` target — set once with
`agentstop config --ssh <user>@<host>`. If the server's checkout is not at
`~/ai-stack/agentstop-mw`, point at it with `agentstop config --server-repo <path>`.

## The short way

```bash
agentstop read            # pick from a menu
agentstop read --latest   # just the most recent
```

Fetches a capture, decrypts it with your Keychain key and prints it — showing what you sent beside
what actually left the box. Only the encrypted file is written locally and it is removed
immediately; the plaintext never touches disk.

Must run in Terminal at this Mac's own screen, for the reason above. The manual equivalent follows.

## Reading a capture, step by step

### 1. Sit at the client and open Terminal

Applications ▸ Utilities ▸ Terminal. Confirm the session:

```bash
launchctl managername          # must print: Aqua
```

### 2. Produce a capture, if you do not already have one

Captures only happen when a request asks for one. The header value is your **capture id** — the
fingerprint `agentstop enroll` printed, also stored in `~/.agentstop/client.json`. (A literal `1`
still works, but only if the server pins your IP under `recipients`, which breaks the moment DHCP
moves you.)

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
  -X POST http://<server>:11500/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Deflector-Capture: <your-capture-id>' \
  -d '{"model":"lfm2.5:latest","stream":false,"max_tokens":16,
       "messages":[{"role":"user","content":"Say OK."}]}'
```

### 3. List what is on the server

```bash
ssh <user>@<server> 'ls -lt ~/.agentstop/logs/capture/*/*.cms | head'
```

Filenames are `<epoch-ms>-<random>.cms` and match the `trace_id` inside, so the newest is last.

### 4. Copy one down

The file is ciphertext, so moving it is safe:

```bash
ssh <user>@<server> 'ls -t ~/.agentstop/logs/capture/*/*.cms | head -1'   # pick one
scp <user>@<server>:<that-path> /tmp/capture.cms
```

### 5. Decrypt it to the screen

```bash
~/.agentstop/bin/decrypt-capture.sh /tmp/capture.cms | python3 -m json.tool
```

The key is read from your Keychain into memory and piped to `openssl`; it never returns to disk,
and the plaintext is only ever on your screen.

You should **not** see an access prompt. The item was created with an ACL for `/usr/bin/security`,
so it is read without asking. (A key imported by hand through Keychain Access has no such entry
and *will* prompt the first time — click **Always Allow**.)

### 6. Delete the local copy

```bash
rm /tmp/capture.cms
```

---

## What you get back

```json
{
  "v": 1, "trace_id": "...", "ts": 1787253592, "client": "...", "model": "lfm2.5:latest",
  "request_in":       "...",
  "request_upstream": "...",
  "response_out":     "...",
  "truncated": {}
}
```

| Field | Meaning |
| --- | --- |
| `request_in` | exactly what you sent, before any redaction |
| `request_upstream` | what actually left the box, after redaction and routing |
| `response_out` | what was sent back to you |

**The difference between the first two is the point.** It is the audit trail for the privacy
pipeline — it shows what was stripped, and what model the request was really routed to. Diff them:

```bash
~/.agentstop/bin/decrypt-capture.sh /tmp/capture.cms \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["request_in"]); print("---"); print(d["request_upstream"])'
```

---

## Confirming the server cannot read it

Three checks, run **on the server**. The full set is in
[verifying-capture-privacy.md](verifying-capture-privacy.md).

```bash
BLOB=$(ls -t ~/.agentstop/logs/capture/*/*.cms | head -1)

# 1. the server holds no private key at all
grep -rl -- '^-----BEGIN [A-Z ]*PRIVATE KEY-----$' ~/.agentstop | wc -l      # expect: 0

# 2. its own decrypt attempt fails
/opt/homebrew/bin/openssl cms -decrypt -inform DER -in "$BLOB"
#    -> No recipient certificate or key specified

# 3. a valid but DIFFERENT key is rejected
openssl req -x509 -newkey rsa:4096 -nodes -keyout /tmp/evil.key -out /tmp/evil.crt \
  -days 1 -subj "/CN=not-your-key" 2>/dev/null
/opt/homebrew/bin/openssl cms -decrypt -inform DER -in "$BLOB" -recip /tmp/evil.crt -inkey /tmp/evil.key
#    -> Error decrypting CMS using private key
rm -f /tmp/evil.key /tmp/evil.crt
```

Check 3 is the one that carries weight. Check 2 could fail simply because no key was supplied;
check 3 supplies a perfectly good one and is still refused.

---

## What you can check without a terminal

These confirm the encryption is real. None of them can decrypt anything.

**The key is in your Keychain.** Open Keychain Access — on current macOS it is *not* in Utilities
and Spotlight will not find it; it lives in `/System/Library/CoreServices/Applications/`. Select
the **login** keychain, then the **Passwords** category, and look for `deflector-capture-key`.
See [If you cannot see the key](#if-you-cannot-see-the-key-in-keychain-access) if it is missing.

**The capture really is unreadable.** After step 4, select `/tmp/capture.cms` in Finder and press
Space for Quick Look, or open it in TextEdit. You get binary garbage — no prompt text, no model
name. That is the encrypted file exactly as it sits on the server.

---

## If it does not work

| Symptom | Cause | Fix |
| --- | --- | --- |
| `User interaction is not allowed` | You are over ssh, in the Background session | Walk to the client and use Terminal there |
| `no Keychain item 'deflector-capture-key'` | Item name or account name does not match | Account must be your short username (`whoami`) |
| No `.cms` files listed | Request lacked the header, or your IP is not in `recipients` | Check both; capture is opt-in *and* IP-bound |
| `Error decrypting CMS using private key` | The blob was encrypted to a different key | The server has a stale `.crt` — resend yours |
| Decrypt works but the content is not yours | You picked the wrong blob | Match on `trace_id`, not on "newest" |

### If you cannot see the key in Keychain Access

The item is a **generic password**, not a certificate or a key in the cryptographic sense. That
trips people up:

- **Passwords.app will never show it.** It only lists website and app logins. This is not one.
- In Keychain Access it appears under **Passwords**, *not* under **Keys** or **My Certificates** —
  even though it holds a private key, macOS classifies it by storage type, not by content.
- Make sure the **login** keychain is selected, not iCloud, System, or "All Items".
- Kind reads as **application password**.

Confirm from a terminal, which is definitive:

```bash
security find-generic-password -a "$(whoami)" -s deflector-capture-key
```

Attributes print even from ssh; only reading the secret itself needs the GUI session.

---

## Limits

This protects **stored captures at rest** — someone reading the log files later, from a backup, a
stolen disk, or another account on the server. It does not encrypt the network hop (plain HTTP on
your LAN), and it does not hide anything from Deflector itself, which necessarily handles your
plaintext while proxying. Metadata stays unencrypted for 14 days.

The full list is in
[verifying-capture-privacy.md](verifying-capture-privacy.md#what-this-does-not-protect).
