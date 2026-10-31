# mailoffload

[![tests](https://github.com/MoriartySwag/mail-offload/actions/workflows/tests.yml/badge.svg)](https://github.com/MoriartySwag/mail-offload/actions/workflows/tests.yml)

Keep a free 15 GB Gmail account working by moving older mail onto your own Mac — with a check that the copy is good before anything leaves Google.

```
$ mailoffload plan
Storage:  12.6 GB of 15.0 GB (84.0%)
Selected: 214 messages (2.4 GB)

  2019-04-12    118.4 MB  Your flight confirmation
  2019-06-02     92.1 MB  Photos from the trip
  ...
```

## Why this exists

Gmail's 15 GB is **space on Google's servers**. Every mail app — Apple Mail, Thunderbird, anything — only keeps a *copy* on your device. Archiving a message, or telling the app to download attachments, changes nothing on the server. The only thing that lowers the number Google reports is deleting the message from Gmail.

So the useful workflow is: **copy an old message to your Mac, prove the copy is intact, then delete it from Gmail.** No mail client does that for you, because "delete on the server" is a scary thing to automate. This tool does it, in small batches, with checks at every step.

Note: Gmail's 15 GB is shared with Drive and Photos. This tool only ever touches mail.

## What it does

1. Asks Gmail how full the account is.
2. If you are past your trigger (80% by default) or you ask it to, it picks messages older than a set age — biggest first, or oldest first.
3. Downloads each one into a dated folder on your Mac.
4. Reads the file back and compares its fingerprint with what came off the server.
5. Only then deletes the original from Gmail, including Gmail's **All Mail**, and finally from **Trash** — the two places people usually forget.
6. Stops at a hard cap (500 messages / 2 GB per run by default).

## What it does not do

- It does not make your phone see the archived mail. Once it leaves Gmail, it lives on the Mac.
- It does not restore anything automatically (the files are plain `.eml`, so you can drag them back into Gmail yourself).
- It does not touch Drive, Photos, or any other provider yet.
- It does not run on its own — there is a scheduler example below.

## Safety rules

- **Rehearsal by default.** `mailoffload run` copies and checks everything but deletes nothing. Deleting requires `--apply`.
- **Copy before delete, always.** Deletion only happens for messages whose file was written *and* re-read successfully in that same run.
- **Capped.** A wrong setting can't empty your account in one go.
- **Age floor.** Nothing newer than `min_age_days` is ever considered.
- **Idempotent.** Running it twice does not duplicate files; identical content maps to the same file.

Still: this is a delete tool. Keep a backup of the archive folder (Time Machine plus one external copy). Once mail leaves Gmail, you are the backup.

## Install

You need Python 3.9 or newer. Nothing else — no packages, no build step.

```sh
git clone https://github.com/MoriartySwag/mail-offload
cd mail-offload
./bin/mailoffload --help
```

Or install it on your PATH:

```sh
pipx install .
```

## 1. Get an app password

Gmail will not accept your normal password over IMAP.

1. Turn on 2-Step Verification: Google Account → Security.
2. Go to Google Account → Security → **App passwords**.
3. Create one called `mailoffload` and copy the 16 letters.

## 2. Configure

```sh
./bin/mailoffload init
```

That writes `~/.config/mailoffload/config.ini` (readable only by you). Put your address and the app password in it:

```ini
[account]
user = you@gmail.com
password = abcdefghijklmnop
host = imap.gmail.com
port = 993

[policy]
trigger_percent = 80      # start moving mail once the account is this full
target_percent = 70       # stop when it is back down to this
min_age_days = 180        # never touch anything newer than this
max_messages_per_run = 500
max_bytes_per_run = 2G
order = largest           # or "oldest"

[storage]
archive_dir = ~/MailArchive
state_dir = ~/.local/state/mailoffload
```

Prefer to keep the password out of the file? Leave `password` blank and export it instead:

```sh
export MAILOFFLOAD_APP_PASSWORD='abcdefghijklmnop'
```

## 3. Look before you leap

```sh
./bin/mailoffload check     # how full is the account, where is the archive
./bin/mailoffload plan      # which messages a run would pick, no downloading
```

## 4. Rehearse, then run

```sh
./bin/mailoffload run              # download + verify, deletes nothing
./bin/mailoffload run --apply      # same, then delete those messages from Gmail
```

The rehearsal is not wasted work: the second command finds the same copies already on disk, checks them again, and only then deletes.

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--apply` | actually delete from Gmail |
| `--limit 50` | at most 50 messages this run |
| `--force` | ignore the trigger threshold |
| `--assume-used 13G` | pretend the account is this full (if Gmail won't report it) |
| `--config PATH` | use a different config file |

## Where the mail goes

```
~/MailArchive/
  2019/05/2019-05-02_481_b1d4f2aa.eml
  manifest.jsonl
```

One plain `.eml` file per message, sorted by the message's own date. `manifest.jsonl` records the fingerprint, size, Message-ID and path of every file.

- `./bin/mailoffload verify` re-checks every file against the manifest.
- `./bin/mailoffload pack` builds `MailArchive-YYYY.mbox` files you can import into Mail.app (`File → Import Mailboxes…`).
- Any `.eml` opens in Mail.app, or you can drag it back into a Gmail mailbox to restore it.

## Run it weekly

`contrib/com.mailoffload.weekly.plist` is a LaunchAgent that runs every Monday at 10:00. Edit the two paths in it, then:

```sh
cp contrib/com.mailoffload.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mailoffload.weekly.plist
```

It reads the password from your config file, so nothing secret lives in the plist. A run under the trigger is a no-op.

## Things worth knowing

- **Gmail has two copies that count.** Removing a message from your inbox leaves it in All Mail, and deleting it there leaves it in Trash. Trash still uses quota. This tool handles all three steps, which is why `Space actually freed` can be lower than `Archived` until the Trash step completes.
- **IMAP is the only interface used.** That is what makes it client-independent — Apple Mail, Thunderbird, and the web UI all see the same result.
- **Subjects can be odd.** Some messages have no Message-ID; those still get archived and are removed from Trash after Gmail's normal 30-day cleanup.
- **Your phone will lose old mail.** That is the trade-off for not paying for storage.
- **Keep the archive out of iCloud Drive** unless you want it counted against your iCloud space too. `~/MailArchive` is outside it by default.
- **Nothing is uploaded anywhere.** The only network traffic is to `imap.gmail.com`.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

## Roadmap

- [ ] menu bar app showing the same numbers
- [ ] Thunderbird add-on that wraps the CLI
- [ ] restore command (`mailoffload restore --since 2019`)
- [ ] other IMAP providers (the Gmail folder names are the only special part)

## License

MIT. See `LICENSE`.
