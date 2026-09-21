<!-- This Source Code Form is subject to the terms of the Mozilla Public
     License, v. 2.0. If a copy of the MPL was not distributed with this
     file, You can obtain one at https://mozilla.org/MPL/2.0/. -->

# Backing up your capture key

Losing the client's Keychain item makes every capture encrypted to it permanently unreadable —
silently, because encryption keeps succeeding regardless. This is how to make that survivable.

Related: [client setup](macos-client-capture-setup.md) · [reading captures](reading-captures.md)

---

## Do not back up the primary key

Exporting the Keychain key would recreate exactly the on-disk copy the design exists to remove,
and it would have to be handled, moved and stored — three chances to leak the one secret that
matters.

Use `backup_recipients` instead. You generate a **second, independent key pair**, and CMS encrypts
every new blob to *both* certificates. Either key opens any capture, so recovery never involves
the primary at all. Only public certificates ever leave the client.

Two things to be clear about before starting:

- **It is not retroactive.** Captures already on disk stay encrypted to the primary key only.
  Configure this before captures start mattering.
- **The backup key can read everything.** It is exactly as sensitive as the primary. Offline,
  separate storage is the entire point.

A hardware token (YubiKey PIV) is the strongest option in principle, but CMS decryption through
PKCS#11 does not work with the LibreSSL that macOS ships, and is fragile even with real OpenSSL. A
recovery path used once, under pressure, should be boring. That fragility is a worse risk than the
one it removes.

---

## Generate it on the CLIENT — never on the server

Run this on the machine that reads captures, not the one running Deflector.

The server is the one machine that must never hold a key able to read captures. Generating there
would put one on its disk, even briefly — and "briefly" is doing more work than it can bear:
`rm -P` is best-effort on an SSD, memory can page to swap, and a server compromised during that
window yields the key *and* its passphrase, and with them every future capture. The fact that the
key is worthless until its certificate becomes a recipient makes the window small, not safe.

## It needs real OpenSSL, not Apple's LibreSSL

**Specifically, `genrsa -aes256` on LibreSSL is the problem.** It writes the traditional PEM format
(`Proc-Type: 4,ENCRYPTED` / `DEK-Info:`), whose key derivation is **MD5 at a single iteration** — no
meaningful work factor, so the passphrase falls to offline guessing very fast.

That is a claim about one command, not the whole toolchain: LibreSSL does support
`pkcs8 -topk8 -v2` (real PBKDF2) and `enc -pbkdf2 -iter`. But real OpenSSL lets us pin the
iteration count, so `make-backup-key.sh` requires it and sets **600,000 iterations of
HMAC-SHA256**.

```bash
brew install openssl
```

The script searches `PATH` before the usual prefixes, so a Homebrew installed somewhere
non-standard — a different volume, for instance — is found. If it still cannot see yours, name it:

```bash
DEFLECTOR_OPENSSL=/path/to/bin/openssl scripts/make-backup-key.sh --bitwarden
```

**Recovery does not need any of this.** `decrypt-capture.sh` uses `/usr/bin/openssl`, and LibreSSL
3.3.6 was verified to read a 600,000-iteration PKCS#8 key. So if the Homebrew that generated the
key ever disappears — an unmounted external volume, a wiped machine — you can still restore.

```bash
scripts/make-backup-key.sh              # copies the key to your clipboard
scripts/make-backup-key.sh --print      # prints it instead (avoids Universal Clipboard)
```

It generates the pair, wraps the key, writes `~/.agentstop/keys/backup.crt`, and **verifies a full
encrypt/decrypt round trip before handing you anything**. If the passphrase or key is wrong you
find out immediately, not during a real recovery. The plaintext key never touches disk outside a
`0700` temp directory that is shredded on exit.

---

## Storing it in Bitwarden, automatically

```bash
brew install bitwarden-cli     # once, ON THE CLIENT
bw login                       # once
agentstop backup --bitwarden   # or scripts/make-backup-key.sh --bitwarden
```

That generates the key, writes it straight into your vault as a Secure Note named
`Deflector capture backup key`, and **reads it back to confirm it matches byte for byte** before
telling you it worked. It records the certificate's SHA-256 and the creation date as custom
fields, so you can tell which server config a stored key belongs to.

The key never appears in a process listing. `bw create` and `bw encode` both read from stdin, so
it is piped rather than passed as an argument — anything on a command line is visible to `ps`.
Your master password is typed at Bitwarden's own prompt and the session key stays in the
environment.

It refuses rather than guessing if: the CLI is missing, you are not logged in, or an item with
that name already exists. That last one matters — two items with one name is how you end up
restoring the wrong key. Use `DEFLECTOR_BW_ITEM` to choose a different name.

If anything fails, nothing is saved and the certificate is left in place so you can retry.

### Doing it by hand instead

The encrypted key is ~3,450 characters — comfortably inside Bitwarden's 10,000-character secure
note limit, so no paid attachment is needed.

1. Run `scripts/make-backup-key.sh` (clipboard) or `--print` (terminal).
2. In Bitwarden: **New item ▸ Secure Note**, name it `Deflector capture backup key`, paste into
   **Notes**.
3. Clear the clipboard: `pbcopy </dev/null`
4. Record the **passphrase somewhere else** — see below.

> **Universal Clipboard.** With Handoff enabled, your clipboard syncs to your other Apple devices,
> so the key briefly leaves this machine. Either turn Handoff off first
> (System Settings ▸ General ▸ AirDrop & Handoff), or use `--print` and copy from the terminal —
> then clear the scrollback (⌘K). `--bitwarden` avoids the clipboard entirely.

### Where the passphrase goes

**Not in that same note, and ideally not in the same vault.** If one compromise yields both the
key and its passphrase, the passphrase has added nothing — you are relying on Bitwarden alone.

Keeping them separate gives genuine two-factor recovery: an attacker needs vault access *and* the
second secret. Reasonable choices are paper in a safe, a different password manager, or memorised
with a sealed written copy as insurance.

Be honest with yourself about the trade: separation improves security but adds a way to lose
access. If you will realistically lose a piece of paper, storing the passphrase in the same vault
is still far better than having no backup — just understand it collapses to Bitwarden's security.

### Bitwarden as a single point of failure

One vault is one dependency: lose access, lose the backup. If these captures ever matter, add a
second copy on different media — an APFS-encrypted USB drive in a drawer covers the failure modes
a cloud vault does not, and vice versa.

---

## Order of operations

Do these in order. It is not arbitrary:

1. Generate the key.
2. Store it in Bitwarden and record the passphrase separately.
3. Let the script exit — the local copy is shredded.
4. **Only then** add the certificate to the server's `config.yaml` and restart.

Until the certificate is a configured recipient, the key decrypts nothing — that is the one moment
it is worthless, and the moment it spends time on disk. Reversing steps 3 and 4 leaves a live
decryption key sitting in a temp directory.

```yaml
capture:
  backup_recipients:
    - ~/.agentstop/keys/backup.crt
```

Confirm it took effect — the startup log records the count:

```bash
grep capture_ready ~/.agentstop/logs/requests-*.jsonl | tail -1
# {"ev": "capture_ready", ..., "recipients": ["..."], "backups": 1}
```

`"backups": 0` means it is not active, and captures written meanwhile are unprotected by it.
A backup path that does not resolve is reported separately as `capture_backup_missing`, and
enabling capture with no working backup logs a `capture_no_backup` warning at every startup.

---

## The recovery drill

```bash
agentstop backup --verify
```

Pulls the key back out of Bitwarden, makes a fresh capture, and decrypts it with **the backup key
alone** — not your Keychain. It prompts for the backup passphrase, writes the key only to a 0600
temp file and shreds it afterwards. Re-run it whenever the keys change, and occasionally anyway.

Everything before this step proves the key was *stored*, not that it *opens* anything. Only this
proves the backup is real.

### By hand

**A backup you have never restored from is not a backup.** Do this once now, and after any change
to the keys.

1. Make a fresh capture (`-H 'X-Deflector-Capture: <your-capture-id>'`).
2. Retrieve the key from Bitwarden into a temp file on the client:
   `/tmp/restore.key`, `chmod 600`.
3. Decrypt using **only the backup key** — not the Keychain:

   ```bash
   ~/.agentstop/bin/decrypt-capture.sh <blob>.cms \
     --key-file /tmp/restore.key --cert ~/.agentstop/keys/backup.crt
   ```

   It prompts for the passphrase. That is deliberate: it is never passed on the command line,
   where it would land in your shell history and in `ps` output.
4. Confirm you get readable JSON, then `rm -P /tmp/restore.key`.

If step 3 fails, your backup does not work — fix it now, while the primary key still exists.
