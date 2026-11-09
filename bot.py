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

# Récupération des clés depuis les variables d'environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

HEADERS = {
    "x-rapidapi-key": FOOTBALL_API_KEY,
    "x-rapidapi-host": "v3.football.api-sports.io"
}

TEAM_ALIASES = {
    "barca": "Barcelona",
    "barça": "Barcelona",
    "real": "Real Madrid",
    "man utd": "Manchester United",
    "man city": "Manchester City",
    "psg": "Paris Saint Germain",
    "inter": "Inter",
    "juve": "Juventus",
    "bayern": "Bayern Munich"
}

def poisson_prob(lmbda, k):
    if lmbda < 0:
        return 0.0
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "⚽ **FOOTBALL PREDICTOR AI – V5**\n\n"
        "Je t'aide à analyser les matchs et calculer les scores exacts.\n\n"
        "**Commandes disponibles :**\n"
        "• `/today` — Liste les matchs du jour (1, 2, 3...)\n"
        "• `/search <équipe>` — Recherche un club (ex: `/search FC Barcelona`)\n"
        "• `/predict <N°>` — Analyse complète d'un match (ex: `/predict 1`)\n"
        "• `/help` — Aide"
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

        msg += "Pour analyser un match : `/predict 1`"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur API: {e}")
        await update.message.reply_text("Erreur lors de la récupération des matchs.")

async def search_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Veuillez indiquer un nom d'équipe. Exemple : `/search FC Barcelona`", parse_mode="Markdown")
        return

    # CORRECTION : On rejoint tous les arguments pour garder les espaces (ex: "FC Barcelona")
    raw_query = " ".join(context.args).strip().lower()
    search_query = TEAM_ALIASES.get(raw_query, raw_query)
    
    current_year = datetime.date.today().year
    
    try:
        team_url = f"https://v3.football.api-sports.io/teams?search={search_query}"
        team_resp = requests.get(team_url, headers=HEADERS, timeout=10).json()
        teams = team_resp.get("response", [])

        team_id = None
        team_name = search_query.capitalize()

        if teams:
            team_info = teams[0]["team"]
            team_id = team_info["id"]
            team_name = team_info["name"]

        fixtures = []
        if team_id:
            for season in [current_year, current_year - 1]:
                fix_url = f"https://v3.football.api-sports.io/fixtures?team={team_id}&season={season}"
                fix_resp = requests.get(fix_url, headers=HEADERS, timeout=10).json()
                fixtures = fix_resp.get("response", [])
                if fixtures:
                    break

        if not fixtures:
            await update.message.reply_text(f"Aucun match trouvé pour : *{raw_query}*.", parse_mode="Markdown")
            return

        fixtures.sort(key=lambda x: x["fixture"]["date"])
        context.user_data["search_fixtures"] = fixtures[:25]

        msg = f"🔍 **Matchs de la saison — {team_name}**\n\n"
        for idx, f in enumerate(fixtures[:25], start=1):
            f_date = f["fixture"]["date"][:10]
            league = f["league"]["name"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            status = f["fixture"]["status"]["short"]
            
            score_str = ""
            if status in ["FT", "AET", "PEN"]:
                gh = f["goals"]["home"]
                ga = f["goals"]["away"]
                score_str = f" | Score : **{gh} - {ga}**"

            msg += f"**{idx}.** 📅 {f_date} | {league}{score_str}\n   🏠 {home} 🆚 ✈️ {away}\n\n"

        msg += f"Pour analyser un match : `/predict 1`"
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Erreur recherche saison: {e}")
        await update.message.reply_text("Erreur lors de la recherche des matchs de l'équipe.")

async def predict_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : `/predict N°` (ex: `/predict 1`).", parse_mode="Markdown")
        return

    arg = context.args[0]
    match = None

    if arg.isdigit():
        idx = int(arg) - 1
        if "search_fixtures" in context.user_data and 0 <= idx < len(context.user_data["search_fixtures"]):
            match = context.user_data["search_fixtures"][idx]
        elif "today_fixtures" in context.user_data and 0 <= idx < len(context.user_data["today_fixtures"]):
            match = context.user_data["today_fixtures"][idx]

    if not match:
        url = f"https://v3.football.api-sports.io/fixtures?id={arg}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10).json()
            fixtures = resp.get("response", [])
            if fixtures:
                match = fixtures[0]
        except Exception:
            pass

    if not match:
        await update.message.reply_text("Match introuvable. Effectuez d'abord `/today` ou `/search <équipe>` puis tapez `/predict 1`.")
        return

    home_team = match["teams"]["home"]["name"]
    away_team = match["teams"]["away"]["name"]
    league_name = match["league"]["name"]

    lambda_home = 1.45
    lambda_away = 1.15

    score_probs = []
    for h_goals in range(5):
        for a_goals in range(5):
            p = poisson_prob(lambda_home, h_goals) * poisson_prob(lambda_away, a_goals)
            score_probs.append((h_goals, a_goals, p))

    score_probs.sort(key=lambda x: x[2], reverse=True)
    top_scores = score_probs[:4]

    home_win = sum(p for h, a, p in score_probs if h > a)
    draw = sum(p for h, a, p in score_probs if h == a)
    away_win = sum(p for h, a, p in score_probs if h < a)
    total = home_win + draw + away_win

    h_pct = (home_win / total) * 100
    d_pct = (draw / total) * 100
    a_pct = (away_win / total) * 100

    best_h, best_a, _ = top_scores[0]

    msg = (
        f"📊 **ANALYSE ET PRÉDICTION DU MATCH**\n\n"
        f"🏆 **{league_name}**\n"
        f"🏠 **{home_team}** 🆚 ✈️ **{away_team}**\n\n"
        f"🎯 **SCORE EXACT PRÉDIT : `{best_h} - {best_a}`**\n\n"
        f"📈 **Probabilités 1N2 :**\n"
        f"• Victoire {home_team} : **{h_pct:.1f}%**\n"
        f"• Match nul : **{d_pct:.1f}%**\n"
        f"• Victoire {away_team} : **{a_pct:.1f}%**\n\n"
        f"⚽ **Top 4 Scores Exacts Probables :**\n"
    )
    for h, a, p in top_scores:
        msg += f"• Score `{h} - {a}` ➔ **{(p*100):.1f}%** de chance\n"

    msg += "\n⚠️ *Calcul statistique basé sur la distribution de Poisson.*"

    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_matches))
    app.add_handler(CommandHandler("search", search_team))
    app.add_handler(CommandHandler("predict", predict_match))

    print("Le bot V5 est en route...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
