# CROUS housing bot 🏠

Checks **trouverunlogement.lescrous.fr** every ~5 minutes and sends you a
**Telegram** message as soon as a CROUS accommodation becomes available in your cities.
You choose the cities **by chatting with the bot**. It runs free on GitHub Actions.

## Setup (≈10 min)

1. **Create the Telegram bot:** in Telegram open **@BotFather** → `/newbot` → choose a name →
   copy the **token** (looks like `123456:ABC-...`).
2. **Put the code on GitHub:** create a **public** repository (public = unlimited free
   Actions minutes; your token stays secret in Secrets) and upload all files of this
   folder, including the `.github` folder.
3. **Add the secret:** repo → **Settings → Secrets and variables → Actions → New repository
   secret** → name `TELEGRAM_BOT_TOKEN`, value = your token.
4. **Start it:** repo → **Actions** tab → enable workflows.
5. **Open your bot in Telegram and press Start.** Within ~5–10 minutes it replies with a
   list of cities to choose from. The first person who writes to the bot becomes its owner,
   so do this yourself, first.

## Talking to the bot

| Command | What it does |
|---|---|
| `/add Lyon` | watch Lyon (10 km around the centre) |
| `/add Lyon 20` | watch Lyon with a 20 km radius |
| `/add` | shows buttons with popular student cities |
| `/remove Lyon` | stop watching Lyon |
| `/cities` | what you're watching |
| `/max 450` / `/max off` | max rent per month |
| `/now` | everything available right now |

When you add a city, the bot first sends what's already available there, then only **new** listings.

⏱ The bot answers at its next check, usually 5–10 minutes later (GitHub runs it on a schedule,
it isn't online 24/7). Your settings are saved in `config.json` in the repo.

## Notes
- You get notified again if a listing disappears and comes back (someone cancelled).
- Optional: limit room type by editing `config.json` → `"occupation_modes": ["alone"]`
  (values: `alone`, `house_sharing`, `couple`).
- GitHub pauses scheduled workflows in repos with no activity for 60 days. If that
  happens, open the Actions tab and click "Enable workflow".
