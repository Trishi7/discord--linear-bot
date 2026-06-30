# Deploying the Discord → Linear Triage Bot (Linux VPS + systemd)

The bot is a single long-running Python process that holds a Discord gateway
connection. It needs to run 24/7 and keep a small SQLite file (`bot_state.db`)
for dedup state. We run it as a **systemd service** so it auto-restarts on
crash and comes back after a reboot.

> This folder is not its own git repo, so we copy the source up with `scp`
> instead of `git clone`. The code is tiny (~400 KB).

---

## 1. Copy the bot to the server (run on your Windows machine)

From a terminal in this folder (`Discord Bot Tracker`). Replace `USER` and
`SERVER_IP`:

```bash
# Create the staging dir first (scp -r with many sources needs it to exist)
ssh USER@SERVER_IP "mkdir -p /tmp/discord-triage-bot"

scp -r ./bot.py ./classifier.py ./config.py ./db.py ./linear_client.py \
       ./main.py ./requirements.txt ./.env ./deploy ./DEPLOY.md \
       USER@SERVER_IP:/tmp/discord-triage-bot/
```

> `.env` carries your secrets — copying it over SSH is encrypted, that's fine.
> We deliberately **do not** copy `bot_state.db`; the server starts with a
> fresh one. The bot only reacts to *new* Discord messages (no history
> backfill), so no old tickets get re-created.

## 2. Install and start it (run on the server, over SSH)

```bash
ssh USER@SERVER_IP
cd /tmp/discord-triage-bot
sudo bash deploy/setup.sh
```

`setup.sh` is idempotent. It will:

1. install `python3` + venv + pip,
2. create a locked-down `botuser` system account,
3. sync the code to `/opt/discord-triage-bot`,
4. build a virtualenv and `pip install -r requirements.txt`,
5. install + enable + start the `discord-triage-bot` systemd service.

When it finishes you'll see the service status and a hint for live logs.

## 3. Verify it's running

```bash
systemctl status discord-triage-bot
journalctl -u discord-triage-bot -f
```

A healthy start logs `Connecting to Discord gateway...` followed by discord.py
shard-ready lines. In Discord, post a test report in a monitored channel and
watch the approval channel for the ✅/❌ embed.

---

## Updating after a code change

Re-copy the changed files (step 1) into `/tmp/discord-triage-bot`, then:

```bash
ssh USER@SERVER_IP "cd /tmp/discord-triage-bot && sudo bash deploy/setup.sh"
```

The setup script re-syncs code, reinstalls deps, and restarts the service.
Your `.env` and `bot_state.db` in `/opt/discord-triage-bot` are preserved.

## Common operations

| Action            | Command                                             |
|-------------------|-----------------------------------------------------|
| Live logs         | `journalctl -u discord-triage-bot -f`               |
| Restart           | `sudo systemctl restart discord-triage-bot`         |
| Stop              | `sudo systemctl stop discord-triage-bot`            |
| Disable on boot   | `sudo systemctl disable discord-triage-bot`         |
| Edit config       | `sudo nano /opt/discord-triage-bot/.env` then restart |

## Notes

- **Config** is read from `/opt/discord-triage-bot/.env` via `python-dotenv`.
  Edit it there and `restart` to apply.
- **Python**: needs 3.10+ (the code uses `list[int]` / `set[int]` syntax). The
  bot was developed on 3.12.
- **Outbound network only** — no inbound ports, so no firewall/Nginx needed.
- **Logs** go to the systemd journal. Tune verbosity with `LOG_LEVEL` in `.env`
  (`DEBUG` | `INFO` | `WARNING` | `ERROR`).
