<!-- This Source Code Form is subject to the terms of the Mozilla Public
     License, v. 2.0. If a copy of the MPL was not distributed with this
     file, You can obtain one at https://mozilla.org/MPL/2.0/. -->

# Setting up encrypted capture on a macOS client

Point-and-click walkthrough for installing a Deflector capture key on a macOS client.
For the scripted version, see the *Encrypted capture* section of the [README](../README.md).

## The short way

```bash
scripts/install-client.sh --server <deflector-host>   # from a checkout on the CLIENT
agentstop enroll --server <deflector-host>
agentstop status
```

The installer copies the client tools into `~/.agentstop/bin` and puts them on your PATH. It is
idempotent, so re-run it to upgrade after pulling changes.

It generates the key, stores it in the login Keychain, deletes the key file, registers the
certificate with the server and prints your capture id. **There is no server-side step and nothing
to edit** — the client is identified by its certificate's fingerprint, which survives DHCP,
hostname changes and reinstalls.

Run it in Terminal at this Mac's own screen; writing to the login Keychain needs a GUI session,
and the command tells you so rather than failing obscurely.

The rest of this page is the same thing by hand — useful when something breaks, or to see exactly
what is happening.

---

## Before you start

**Do this on the client** — the Mac that will *read* the captures (the one running your agent),
not the Mac running Deflector. The server never gets the private key; that is the whole design.
It only receives the `.crt`, which is public.

Two of the eight steps use Terminal, because macOS ships no GUI for generating a key pair. Every
step that touches the Keychain is point-and-click.

You will need:

- a login account on the client Mac, signed in **at the screen** (directly or via Screen Sharing)
- the ability to hand one small file (`pi.crt`) to whoever administers the Deflector server

> **Why signed in at the screen?** Writing to the login keychain requires it to be *unlocked*,
> which only a GUI (Aqua) session provides. An ssh session, a launchd job, or a tool that shells
> out on your behalf runs in the Background session and fails with
> `User interaction is not allowed`. If you only have ssh, skip this guide and use path (b) in the
> README, which unlocks the keychain first.

---

## Step 1 — Generate the key pair *(Terminal)*

Open **Terminal** on the client Mac and run:

```bash
mkdir -p ~/.agentstop/keys && chmod 700 ~/.agentstop/keys
openssl req -x509 -newkey rsa:4096 -nodes -keyout ~/.agentstop/keys/pi.key \
  -out ~/.agentstop/keys/pi.crt -days 3650 -subj "/CN=deflector-capture"
```

This produces two files:

| File | What it is | Where it ends up |
| --- | --- | --- |
| `pi.key` | the **private** key | your Keychain — then deleted from disk |
| `pi.crt` | the **public** certificate | copied to the Deflector server |

## Step 2 — Put the key on your clipboard *(Terminal)*

```bash
cd /path/to/agentstop-mw
scripts/import-capture-key.sh --gui ~/.agentstop/keys/pi.key
```

This **writes nothing**. It base64-encodes the key, places it on the clipboard, verifies the
clipboard actually took it, and prints the three values you are about to type. Leave this window
open — you will come back to it in step 6.

> The key is stored base64-encoded on purpose. `security` returns *hex* for any value that is not
> plain printable text, and a PEM is multi-line — so storing it raw silently corrupts it
> (3272 bytes in, 6543 bytes of hex out). Base64 is one printable line, and round-trips exactly.

## Step 3 — Open Keychain Access

On current macOS, Keychain Access is **not** in Applications ▸ Utilities and **Spotlight will not
find it**. It lives in `/System/Library/CoreServices/Applications/`.

Either run `open -b com.apple.keychainaccess`, or in **Finder** choose **Go ▸ Go to Folder…**
(⇧⌘G), enter that path, and double-click **Keychain Access**.

## Step 4 — Select the `login` keychain

In the sidebar, under **Default Keychains**, click **login**.

This matters: the *New Password Item* dialog has no keychain picker — it uses whatever is selected.
Choose the wrong one and the item lands somewhere the decrypt script will not look.

If the sidebar is hidden, use **View ▸ Show Keychains**. If **login** shows a padlock, double-click
it and enter your login password to unlock it first.

## Step 5 — Create the item

Choose **File ▸ New Password Item…** (or click **+** in the toolbar). Fill in the three fields
**exactly** — they are matched literally, and a trailing space will break the lookup:

| Field | Value |
| --- | --- |
| **Keychain Item Name:** | `deflector-capture-key` |
| **Account Name:** | your short username — run `whoami` if unsure |
| **Password:** | press **⌘V** (it is already on your clipboard from step 2) |

Tick **Show Password** if you want to confirm something pasted — you should see one long
unbroken line of letters and digits ending in `=` or `==`. It will not look like a key, and
should contain **no** `-----BEGIN` line and **no** line breaks. If it does, the paste came from
the wrong source; redo step 2.

Click **Add**.

## Step 6 — Clear the clipboard and verify *(Terminal)*

Back in Terminal:

```bash
pbcopy </dev/null                                          # stop the key lingering on the clipboard
scripts/import-capture-key.sh --verify ~/.agentstop/keys/pi.key
```

You want exactly this:

```
OK: Keychain item 'deflector-capture-key' matches …/pi.key byte-for-byte.
```

**Do not continue until you see it.** See [Troubleshooting](#troubleshooting) if you do not.

## Step 7 — Delete the private key file

Only now, and only if step 6 said `OK`:

```bash
rm -P ~/.agentstop/keys/pi.key
```

The private key now exists **only in your Keychain**. The decrypt script reads it into memory and
pipes it to `openssl`; it never returns to disk.

> **Make a backup first if these captures will matter.** A Keychain item is a single point of
> failure — lose it and every capture encrypted to that key is permanently unreadable, silently,
> because encryption keeps succeeding. Generate a second pair, keep it offline (a USB key, a
> password manager), and give the server operator its `.crt` for `backup_recipients`. This cannot
> be done retroactively.

## Step 8 — Hand `pi.crt` to the server

Give `~/.agentstop/keys/pi.crt` to whoever runs Deflector. They add it to `config.yaml` keyed by
**your client's IP as the server sees it**:

```yaml
capture:
  enabled: true
  recipients:
    "10.0.0.42": ~/.agentstop/keys/pi.crt
```

An IP absent from that map cannot capture — and the failure is silent, not an error. If captures
never appear, this mapping is the first thing to check.

---

## Using it

Ask for a capture per request with a header:

```bash
curl -XPOST http://<deflector>:11500/v1/chat/completions \
  -H 'X-Deflector-Capture: <your-capture-id>' ...
```

Then decrypt on the client:

```bash
scripts/decrypt-capture.sh ~/.agentstop/logs/capture/<date>/<trace-id>.cms | python3 -m json.tool
```

### Confirming it actually works

```bash
~/.agentstop/bin/capture-selftest.sh
```

Run it in Terminal **at the Mac's own screen** — it needs the unlocked login keychain. It sends a
marked request and then checks, in order, that the blob exists, that the server *cannot* read it
and holds no private key, that you can, and that the plaintext contains your marker. It prints
`RESULT: PASS` only if all of those hold.

### The prompt you will see the first time

The first decrypt shows a dialog asking whether `security` may use the item — roughly *"security
wants to use your confidential information stored in "deflector-capture-key" in your keychain"* —
with **Deny** / **Allow** / **Always Allow**. Wording varies by macOS version.

This is expected and specific to the GUI path: an item created in Keychain Access carries no
access-control entry for `/usr/bin/security`, whereas one written by the script does (`-T`).
Choose **Always Allow** unless you want to approve every decrypt.

---

## Reading captures day to day

Setup is one-time. For where the files live, whether you need a terminal, and how to decrypt
one, see [reading-captures.md](reading-captures.md).

## Troubleshooting

| What you see | What it means | Fix |
| --- | --- | --- |
| `User interaction is not allowed` | Not a GUI session, or the keychain is locked | Run from Terminal at the Mac; or `security unlock-keychain ~/Library/Keychains/login.keychain-db` |
| `could not put the key on the clipboard` | `pbcopy` wrote to a different session's pasteboard (typical over ssh) | Run step 2 at the Mac's screen, or use the non-GUI import |
| `no Keychain item 'deflector-capture-key'` | Name or account does not match, or it went into the wrong keychain | Check both fields for typos and trailing spaces; confirm the item is under **login** |
| `MISMATCH: item exists but does not match` | Paste was truncated, or the raw PEM was stored instead of base64 | Delete the item, redo steps 2–5. **Do not** delete the key file |
| Decrypt says `no recipient matches` | Blob was encrypted to a *different* key than the one you hold | The server's `.crt` is stale — resend `pi.crt` |
| Captures never appear | Client IP not in `recipients`, or capture disabled | Check the server's `config.yaml`; this fails silently by design |

To start over, delete the item in Keychain Access (right-click ▸ **Delete**) and repeat from
step 2. Re-running the script with `--gui` re-copies the value.

## Notes

- **Nothing is stored until step 5.** Steps 1–2 only produce files and a clipboard value, so it is
  safe to stop and restart.
- **The item is a generic password, not a certificate identity.** It appears under the **Passwords**
  category in Keychain Access, not **My Certificates** — that is correct.
- **`pi.crt` is public.** It is safe to email, commit, or copy over the network. `pi.key` is not.
- A fully GUI key-generation path via **Certificate Assistant** would avoid Terminal in step 1, but
  it stores the key as a Keychain *identity* rather than a generic password, which the decrypt
  script does not currently read. Not supported today.
