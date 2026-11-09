import os
import math
import logging
import requests
import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# Configuration des logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

HEADERS = {
    "x-rapidapi-key": FOOTBALL_API_KEY,
    "x-rapidapi-host": "v3.football.api-sports.io"
}

# Dictionnaire d'association directe pour garantir l'accès instantané aux grands clubs
FORCED_TEAM_IDS = {
    "barca": 529,
    "barça": 529,
    "fc barcelona": 529,
    "barcelona": 529,
    "real madrid": 541,
    "real": 541,
    "psg": 85,
    "paris": 85
}

def poisson_prob(lmbda, k):
    if lmbda < 0:
        return 0.0
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "⚽ **FOOTBALL PREDICTOR AI – V8**\n\n"
        "Recherche universelle corrigée et statistiques avancées.\n\n"
        "**Commandes :**\n"
        "• `/today` — Matchs du jour (1, 2, 3...)\n"
        "• `/search <équipe>` — Recherche sur toute la saison\n"
        "• `/predict <N°>` — Analyse complète (Scores, Buts, Corners, Cartons, Mi-temps)"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def today_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = "https://v3.football.api-sports.io/fixtures"
    today_date = datetime.date.today().strftime("%Y-%m-%d")
    params = {"date": today_date}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        data = response.json()
        fixtures = data.get("response", [])

        if not fixtures:
            await update.message.reply_text(f"Aucun match trouvé pour aujourd'hui ({today_date}).")
            return

        context.user_data["today_fixtures"] = fixtures[:30]
        
        msg = f"⚽ **Matchs du {today_date} — {len(fixtures)} rencontre(s)**\n\n"
        for idx, f in enumerate(fixtures[:30], start=1):
            time_str = f["fixture"]["date"][11:16]
            league = f["league"]["name"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            msg += f"**{idx}.** 🕒 {time_str} | {league}\n   🏠 {home} 🆚 ✈️ {away}\n\n"

        msg += "Pour analyser : `/predict 1`"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur API: {e}")
        await update.message.reply_text("Erreur lors de la récupération des
        
