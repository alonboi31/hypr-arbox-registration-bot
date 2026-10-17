# Deploying the bot to a cloud VM

Goal: the bot runs 24/7 on a small Linux server, books your classes the moment
registration opens, and you manage the rules from a web page that only you can reach.

Time needed: about 30 minutes, plus one rehearsal. Cost: roughly 4-6 USD per month.

> The Docker image could not be built in the environment where this was written
> (no Docker daemon). Everything else is tested. Step 5 is where any Docker
> problem would show up, before anything real happens.

---

## 1. Pick a server

Any small Linux VM works: 1 vCPU, 1 GB RAM, Ubuntu 24.04.

| Provider | Plan | About |
|---|---|---|
| Hetzner Cloud | CX22 | 4 EUR/month |
| DigitalOcean | Basic droplet | 6 USD/month |
| AWS Lightsail | smallest plan | 5 USD/month |
| Oracle Cloud | Always Free | free, but sign-up is picky |

**Region:** choose Europe or Israel (AWS has Tel Aviv, `il-central-1`). Arbox sits
behind Cloudflare, and I could not test whether it blocks some datacenter
locations. Step 5 checks this for you. If login fails with a 403 from one provider,
recreate the server in another region.

When creating the server, add your SSH key (or choose password-less key login).

## 2. Prepare the server

Connect: `ssh root@YOUR_SERVER_IP`

```bash
apt update && apt -y upgrade
timedatectl set-ntp true          # exact clock = exact booking time
timedatectl set-timezone Asia/Jerusalem
timedatectl | grep -E "synchronized|Time zone"   # want: synchronized: yes

# firewall: only SSH is open; the web page is NOT exposed to the internet
ufw allow OpenSSH && ufw --force enable

# Docker
curl -fsSL https://get.docker.com | sh
```

## 3. Get the code onto the server

The repo is private, so give the server read-only access with a deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/deploy_key
cat ~/.ssh/deploy_key.pub
```

On GitHub open the repo, then Settings, Deploy keys, Add deploy key. Paste the
output, leave "Allow write access" **off**. Then:

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:alonboi31/hypr-arbox-registration-bot.git /opt/arbox-bot
cd /opt/arbox-bot
git config core.sshCommand "ssh -i ~/.ssh/deploy_key"   # so later `git pull` works
```

## 4. Configure

```bash
cp .env.example .env
chmod 600 .env          # holds your Arbox password
nano .env
```

Fill in:

| Setting | Value |
|---|---|
| `ARBOX_EMAIL`, `ARBOX_PASSWORD` | your HYPR login (use a **new** password, see Security) |
| `UI_PASSWORD` | a long random password for the web page. Generate one with `openssl rand -base64 18` |
| `NTFY_TOPIC` | a long random name, e.g. `arbox-k3j29xq81fd`. Install the free **ntfy** app on your phone and subscribe to the same name to get booking alerts |
| `DRY_RUN` | **leave at `1`** for now |

Everything else already has the right defaults for HYPR White City.

## 5. Check the connection (books nothing)

```bash
docker compose build
docker compose run --rm bot python -m app.check
```

Good output looks like:

```
OK: logged in, 112 classes in the next 8 days
OK: booking membership id = 20739437
DRY_RUN=on (nothing will be booked)   TZ=Asia/Jerusalem
0 active rule(s)
```

If it fails, see Troubleshooting at the bottom.

## 6. Start it and open the web page

```bash
docker compose up -d
docker compose logs -f --tail 30        # Ctrl+C leaves the logs, the bot keeps running
```

The page listens only on the server itself (`127.0.0.1:8000`). Two safe ways in:

**A. SSH tunnel (simplest, no extra install).** On your own computer run:

```bash
ssh -L 8000:localhost:8000 root@YOUR_SERVER_IP
```

then open http://localhost:8000 while that window stays open. Log in as `admin`
with your `UI_PASSWORD`.

**B. Tailscale (works from your phone).** Free for personal use. On the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale serve --bg 8000
```

Install Tailscale on your phone and laptop, sign in with the same account, and open
the `https://<server-name>.<tailnet>.ts.net` address that `tailscale serve` prints.
Nobody outside your Tailscale account can reach it.

Do **not** open port 8000 to the internet.

## 7. Add your rules

In the page, press **Auto-book** next to a class in "Upcoming classes". That creates a
weekly rule for that weekday, time and class name. Or use the form at the top.
You can switch rules off and on any time.

Remember:

- A rule applies to every future week, not one date.
- If a class matching a new rule is **already open**, the bot books it on its next
  check (within 30 seconds). That is the point of the tool, but it means adding a rule
  can book something immediately.
- Your plan allows one group class per day. Two rules on the same day will make the
  second one fail with `categoryFrequencyRestricts`. Keep one per day.

## 8. Rehearse (still DRY_RUN=1)

1. Run `docker compose run --rm bot python -m app.check`. It lists every upcoming class
   your rules match and when each one opens. Compare those times with what the app
   shows. **Check a Monday class in particular**: Arbox reports 72 hours before, which
   is Friday, not Thursday.
2. Wait for the first real opening. The log line you want is a message like
   `DRY RUN - would book: W.O.D Hall A Wed 21/10 18:00`, and your phone gets the same
   alert. In the page the status shows `dry-run`.
3. It should happen at the opening minute, not earlier or later. If you see
   `registerScheduleDisabled` in the log for a few seconds before it, that is normal:
   the bot starts knocking slightly early and retries.

## 9. Go live

```bash
nano .env                 # set DRY_RUN=0
docker compose up -d --force-recreate   # restart so it reads the new setting
docker compose run --rm bot python -m app.check    # should now say "DRY_RUN=OFF"
```

Rehearsed classes are forgotten when you go live, so any matching class that is
already open gets booked for real within about 30 seconds.

For the first real booking, be ready with the app open to confirm it shows up.
You can cancel it from the app if needed.

## 10. Day to day

| Task | Command (run in `/opt/arbox-bot`) |
|---|---|
| See what it is doing | `docker compose logs -f --tail 50` |
| Is it running? | `docker compose ps` |
| Stop it (e.g. holiday) | `docker compose stop` |
| Start it again | `docker compose up -d` |
| Update to the latest code | `git pull && docker compose up -d --build` |
| Backup the rules | copy `data/bot.db` somewhere safe |

The container restarts by itself after crashes or server reboots
(`restart: unless-stopped`). To pause just one class, switch its rule off in the page.

## Security

- **Change your Arbox password** before first use if the traffic capture file you
  shared earlier still exists anywhere. It contained the old password and login tokens.
- `.env` stays on the server with `chmod 600`. It is git-ignored and never leaves it.
- The web page has a password but no HTTPS of its own; that is why it is only reachable
  through the SSH tunnel or Tailscale.
- Keep the server patched: `apt update && apt -y upgrade` every month or so.
- Automatic booking may be against Arbox's or your studio's rules. The bot uses your own
  account and only a handful of requests, but it is your call.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `check` fails with 403 or a Cloudflare page | The provider's IP is blocked. Recreate the server in another region or provider. |
| `check` fails with 400 or 401 at login | Wrong `ARBOX_EMAIL` or `ARBOX_PASSWORD` in `.env`. The password is not quoted; special characters are fine. |
| `ZoneInfoNotFoundError` | The image was built without `tzdata`. Run `git pull` and rebuild with `docker compose build --no-cache`. |
| Bot books a few seconds late | Server clock is off. Re-check `timedatectl` shows `synchronized: yes`. |
| Page shows no classes | Run `check` to see the error. The login may have expired on Arbox's side; the bot logs in again by itself on the next cycle. |
| `FAILED ... categoryFrequencyRestricts` | Your plan's one-class-per-day limit. Remove one of the two rules on that day. |
| `FAILED ... registerScheduleDisabled` after 2 minutes | The class did not open when the bot expected. Compare the time shown in the page with the app, and tell me, so I can fix the rule for that weekday. |
| No phone alerts | `NTFY_TOPIC` in `.env` must match the topic in the ntfy app exactly. Restart with `docker compose up -d` after editing (`docker compose up -d --force-recreate`). |
| Bot did nothing for a class | In the page, check the rule is on, the weekday, time, and name text match the class. The `check` command prints all matches. |
