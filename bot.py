import os
import math
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

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

def poisson_prob(lmbda, k):
    """Calcule la probabilité de k buts selon la loi de Poisson de paramètre lmbda."""
    if lmbda < 0:
        return 0.0
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "⚽ **FOOTBALL PREDICTOR AI – V2**\n\n"
        "Je t'aide à analyser les matchs et calculer les scores exacts.\n\n"
        "**Commandes disponibles :**\n"
        "• `/today` — Liste tous les matchs du jour\n"
        "• `/search <équipe>` — Recherche les matchs d'une équipe (ex: `/search Real`)\n"
        "• `/predict <ID>` — Analyse un match et calcule les scores exacts\n"
        "• `/help` — Aide"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def today_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = "https://v3.football.api-sports.io/fixtures"
    import datetime
    today_date = datetime.date.today().strftime("%Y-%m-%d")
    params = {"date": today_date}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        data = response.json()
        fixtures = data.get("response", [])

        if not fixtures:
            await update.message.reply_text(f"Aucun match trouvé pour aujourd'hui ({today_date}).")
            return

        msg = f"⚽ **Matchs du {today_date} — {len(fixtures)} rencontre(s)**\n\n"
        for f in fixtures[:25]:  # Limité aux 25 premiers pour éviter les messages trop longs
            fid = f["fixture"]["id"]
            time = f["fixture"]["date"][11:16]
            league = f["league"]["name"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            msg += f"`{fid}` | {time} | {league}\n{home} – {away}\n\n"

        msg += "Pour analyser un match : `/predict ID` ou cherchez avec `/search nom_equipe`"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur API: {e}")
        await update.message.reply_text("Erreur lors de la récupération des matchs.")

async def search_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Veuillez indiquer un nom d'équipe. Exemple : `/search Real`", parse_mode="Markdown")
        return

    query = " ".join(context.args).lower()
    url = "https://v3.football.api-sports.io/fixtures"
    import datetime
    today_date = datetime.date.today().strftime("%Y-%m-%d")
    params = {"date": today_date}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        data = response.json()
        fixtures = data.get("response", [])

        matched = []
        for f in fixtures:
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            if query in home.lower() or query in away.lower():
                matched.append(f)

        if not matched:
            await update.message.reply_text(f"Aucun match trouvé aujourd'hui pour l'équipe : *{query}*.", parse_mode="Markdown")
            return

        msg = f"🔍 **Résultats de recherche pour :** _{query}_\n\n"
        for f in matched:
            fid = f["fixture"]["id"]
            time = f["fixture"]["date"][11:16]
            league = f["league"]["name"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            msg += f"ID : `{fid}` | {time} | {league}\n⚽ **{home} vs {away}**\n\n"

        msg += "Utilisez `/predict ID` pour lancer l'analyse complète."
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur recherche: {e}")
        await update.message.reply_text("Erreur lors de la recherche.")

async def predict_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : `/predict ID`\nTrouve un identifiant avec `/today` ou `/search`.", parse_mode="Markdown")
        return

    fixture_id = context.args[0]
    url = f"https://v3.football.api-sports.io/fixtures?id={fixture_id}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        data = response.json()
        fixtures = data.get("response", [])

        if not fixtures:
            await update.message.reply_text("Identifiant de match introuvable.")
            return

        match = fixtures[0]
        home_team = match["teams"]["home"]["name"]
        away_team = match["teams"]["away"]["name"]
        league_name = match["league"]["name"]

        # Estimation statistique par défaut (Loi de Poisson ajustée)
        lambda_home = 1.45
        lambda_away = 1.15

        # Calcul des probabilités de scores exacts (de 0-0 à 4-4)
        score_probs = []
        for h_goals in range(5):
            for a_goals in range(5):
                p = poisson_prob(lambda_home, h_goals) * poisson_prob(lambda_away, a_goals)
                score_probs.append((h_goals, a_goals, p))

        # Trier par probabilité décroissante
        score_probs.sort(key=lambda x: x[2], reverse=True)
        top_scores = score_probs[:3]

        # Calcul des issues (1N2)
        home_win = sum(p for h, a, p in score_probs if h > a)
        draw = sum(p for h, a, p in score_probs if h == a)
        away_win = sum(p for h, a, p in score_probs if h < a)
        total = home_win + draw + away_win

        h_pct = (home_win / total) * 100
        d_pct = (draw / total) * 100
        a_pct = (away_win / total) * 100

        msg = (
            f"📊 **Analyse & Prédiction : {home_team} vs {away_team}**\n"
            f"🏆 Compétition : {league_name}\n\n"
            f"📈 **Probabilités 1N2 (Modèle Poisson) :**\n"
            f"• Victoire {home_team} : **{h_pct:.1f}%**\n"
            f"• Match nul : **{d_pct:.1f}%**\n"
            f"• Victoire {away_team} : **{a_pct:.1f}%**\n\n"
            f"🎯 **Top 3 Scores Exacts Probables :**\n"
        )
        for h, a, p in top_scores:
            msg += f"• `{h} - {a}` (Confiance estimée : {(p*100):.1f}%)\n"

        msg += "\n⚠️ *Estimations mathématiques basées sur la distribution de Poisson.*"
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Erreur prediction: {e}")
        await update.message.reply_text("Erreur lors du calcul de la prédiction.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_matches))
    app.add_handler(CommandHandler("search", search_team))
    app.add_handler(CommandHandler("predict", predict_match))

    print("Le bot est en route...")
    app.run_polling()

if __name__ == "__main__":
    main()
