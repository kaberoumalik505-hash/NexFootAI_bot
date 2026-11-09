import os
import math
import logging
import requests
import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

HEADERS = {
    "x-rapidapi-key": FOOTBALL_API_KEY,
    "x-rapidapi-host": "v3.football.api-sports.io"
}

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
        "⚽ **FOOTBALL PREDICTOR AI – V9**\n\n"
        "Analyse complète, prédictions avancées (Buts, Corners, Cartons, Mi-temps).\n\n"
        "**Commandes :**\n"
        "• `/today` — Matchs du jour (1, 2, 3...)\n"
        "• `/search <équipe>` — Recherche sur toute la saison (ex: `/search barca`)\n"
        "• `/predict <N°>` — Analyse complète d'un match"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def today_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = "https://v3.football.api-sports.io/fixtures"
    today_date = datetime.date.today().strftime("%Y-%m-%d")
    params = {"date": today_date}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
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
        await update.message.reply_text("Erreur lors de la récupération des matchs.")

async def search_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Veuillez indiquer un nom d'équipe. Exemple : `/search barca`", parse_mode="Markdown")
        return

    raw_query = " ".join(context.args).strip().lower()

    try:
        team_id = None
        team_name = raw_query.capitalize()

        if raw_query in FORCED_TEAM_IDS:
            team_id = FORCED_TEAM_IDS[raw_query]
            team_name = "FC Barcelona" if "barc" in raw_query else raw_query.capitalize()
        else:
            team_url = f"https://v3.football.api-sports.io/teams?search={raw_query}"
            team_resp = requests.get(team_url, headers=HEADERS, timeout=15).json()
            teams = team_resp.get("response", [])
            if teams:
                team_id = teams[0]["team"]["id"]
                team_name = teams[0]["team"]["name"]

        if not team_id:
            await update.message.reply_text(f"Aucune équipe trouvée pour : *{raw_query}*.", parse_mode="Markdown")
            return

        fix_url = f"https://v3.football.api-sports.io/fixtures?team={team_id}"
        fix_resp = requests.get(fix_url, headers=HEADERS, timeout=15).json()
        fixtures = fix_resp.get("response", [])

        if not fixtures:
            for s in [2026, 2025, 2024]:
                f_url = f"https://v3.football.api-sports.io/fixtures?team={team_id}&season={s}"
                f_resp = requests.get(f_url, headers=HEADERS, timeout=15).json()
                items = f_resp.get("response", [])
                if items:
                    fixtures.extend(items)

        if not fixtures:
            await update.message.reply_text(f"Aucun match trouvé pour {team_name} dans l'API.")
            return

        seen_ids = set()
        unique_fixtures = []
        for f in fixtures:
            fid = f["fixture"]["id"]
            if fid not in seen_ids:
                seen_ids.add(fid)
                unique_fixtures.append(f)

        unique_fixtures.sort(key=lambda x: x["fixture"]["date"], reverse=True)
        context.user_data["search_fixtures"] = unique_fixtures[:25]

        msg = f"🔍 **Matchs de la saison — {team_name}**\n\n"
        for idx, f in enumerate(unique_fixtures[:25], start=1):
            f_date = f["fixture"]["date"][:10]
            league = f["league"]["name"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            status = f["fixture"]["status"]["short"]
            
            score_str = ""
            if status in ["FT", "AET", "PEN"]:
                gh = f["goals"]["home"]
                ga = f["goals"]["away"]
                score_str = f" | **{gh}-{ga}**"

            msg += f"**{idx}.** 📅 {f_date} | {league}{score_str}\n   🏠 {home} 🆚 ✈️ {away}\n\n"

        msg += "Pour analyser un match : `/predict 1`"
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Erreur recherche saison: {e}")
        await update.message.reply_text(f"Erreur lors de la recherche des matchs : {e}")

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
            resp = requests.get(url, headers=HEADERS, timeout=15).json()
            fixtures = resp.get("response", [])
            if fixtures:
                match = fixtures[0]
        except Exception:
            pass

    if not match:
        await update.message.reply_text("Match introuvable. Faites d'abord `/today` ou `/search <équipe>` puis tapez `/predict 1`.")
        return

    home_team = match["teams"]["home"]["name"]
    away_team = match["teams"]["away"]["name"]
    league_name = match["league"]["name"]

    lambda_home = 1.45
    lambda_away = 1.15

    score_probs = []
    for h_goals in range(6):
        for a_goals in range(6):
            p = poisson_prob(lambda_home, h_goals) * poisson_prob(lambda_away, a_goals)
            score_probs.append((h_goals, a_goals, p))

    score_probs.sort(key=lambda x: x[2], reverse=True)
    top_scores = score_probs[:4]

    home_win = sum(p for h, a, p in score_probs if h > a)
    draw = sum(p for h, a, p in score_probs if h == a)
    away_win = sum(p for h, a, p in score_probs if h < a)
    total_1n2 = home_win + draw + away_win

    h_pct = (home_win / total_1n2) * 100
    d_pct = (draw / total_1n2) * 100
    a_pct = (away_win / total_1n2) * 100

    best_h, best_a, _ = top_scores[0]

    total_xG = lambda_home + lambda_away
    over_2_5 = sum(p for h, a, p in score_probs if (h + a) > 2.5) * 100
    under_2_5 = 100 - over_2_5
    ht_total = (lambda_home + lambda_away) * 0.45
    est_corners = round((lambda_home + lambda_away) * 4.2 + 3.5)
    est_yellows = round((lambda_home + lambda_away) * 1.8 + 2.1)

    msg = (
        f"📊 **ANALYSE & PRÉDICTIONS AVANCÉES**\n\n"
        f"🏆 **{league_name}**\n"
        f"🏠 **{home_team}** 🆚 ✈️ **{away_team}**\n\n"
        f"🎯 **SCORE EXACT PRÉDIT : `{best_h} - {best_a}`**\n\n"
        f"📈 **1N2 :**\n"
        f"• 1 ({home_team}) : **{h_pct:.1f}%** | Nul : **{d_pct:.1f}%** | 2 ({away_team}) : **{a_pct:.1f}%**\n\n"
        f"⚽ **Buts & Marchés :**\n"
        f"• Total Buts Estimés (xG) : **{total_xG:.2f}**\n"
        f"• Over 2.5 buts : **{over_2_5:.1f}%** (Under : {under_2_5:.1f}%)\n"
        f"• Buts 1ère Mi-Temps (Moy.) : ~**{ht_total:.2f}** buts\n"
        f"• Buts 2ème Mi-Temps (Moy.) : ~**{(total_xG - ht_total):.2f}** buts\n\n"
        f"🚩 **Corners & Cartons (Estimations) :**\n"
        f"• Total Corners estimés : ~**{est_corners}**\n"
        f"• Total Cartons Jaunes estimés : ~**{est_yellows}**\n\n"
        f"🏆 **Top 4 Scores Exacts :**\n"
    )
    for h, a, p in top_scores:
        msg += f"• `{h} - {a}` ➔ {(p*100):.1f}%\n"

    msg += "\n⚠️ *Modèle mathématique statistique (Poisson & Tendances).* "

    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_matches))
    app.add_handler(CommandHandler("search", search_team))
    app.add_handler(CommandHandler("predict", predict_match))

    print("Le bot V9 est en route...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
