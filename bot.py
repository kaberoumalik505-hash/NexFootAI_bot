import os
import math
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("football_predictor_ai")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()
API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = os.getenv("BOT_TIMEZONE", "Africa/Ouagadougou")


def api_get(endpoint: str, params: dict):
    """Call API-Football and return its response rows."""
    if not FOOTBALL_API_KEY:
        raise RuntimeError("La clé API Football n'est pas configurée.")
    response = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"x-apisports-key": FOOTBALL_API_KEY},
        params=params,
        timeout=20
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload.get("response", [])


def poisson(k: int, lam: float) -> float:
    if lam < 0:
        return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def estimate_markets(home_xg: float, away_xg: float):
    """Independent Poisson estimates; model estimates, not guarantees."""
    max_goals = 10
    home_probs = [poisson(i, home_xg) for i in range(max_goals + 1)]
    away_probs = [poisson(i, away_xg) for i in range(max_goals + 1)]

    home_win = draw = away_win = 0.0
    btts = over_25 = 0.0
    scores = []

    for h, ph in enumerate(home_probs):
        for a, pa in enumerate(away_probs):
            p = ph * pa
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p
            if h > 0 and a > 0:
                btts += p
            if h + a >= 3:
                over_25 += p
            scores.append((p, h, a))

    total = home_win + draw + away_win
    if total > 0:
        home_win, draw, away_win = [x / total for x in (home_win, draw, away_win)]
    scores.sort(reverse=True)
    return {
        "home": home_win,
        "draw": draw,
        "away": away_win,
        "btts": btts,
        "over25": over_25,
        "under25": 1 - over_25,
        "scores": scores[:3]
    }


def pct(value):
    return f"{value * 100:.1f} %"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚽ *FOOTBALL PREDICTOR AI — V1*\n\n"
        "Je t'aide à analyser les matchs à partir des données disponibles.\n\n"
        "Commandes :\n"
        "/today — matchs du jour\n"
        "/today AAAA-MM-JJ — matchs d'une date\n"
        "/predict ID — analyse un match avec son identifiant\n"
        "/help — aide\n\n"
        "⚠️ Les probabilités sont des estimations statistiques, jamais des certitudes. "
        "Ne mise pas d'argent que tu ne peux pas perdre."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date = context.args[0] if context.args else datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Format invalide. Utilise /today 2026-11-06")
        return

    await update.message.reply_text(f"🔎 Recherche des matchs du {date}…")
    try:
        fixtures = api_get("fixtures", {"date": date, "timezone": TIMEZONE})
        if not fixtures:
            await update.message.reply_text("Aucun match trouvé pour cette date avec la couverture actuelle de l'API.")
            return

        lines = [f"⚽ *Matchs du {date}* — {len(fixtures)} rencontre(s)"]
        for item in fixtures[:25]:
            fixture = item.get("fixture", {})
            teams = item.get("teams", {})
            league = item.get("league", {})
            home = teams.get("home", {}).get("name", "?")
            away = teams.get("away", {}).get("name", "?")
            fixture_id = fixture.get("id")
            kickoff = fixture.get("date", "")
            try:
                kickoff = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M")
            except Exception:
                kickoff = "heure inconnue"
            league_name = league.get("name", "Compétition")
            lines.append(f"\n`{fixture_id}` | {kickoff} | {league_name}\n{home} – {away}")
        lines.append("\nPour analyser un match : `/predict ID`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        log.exception("Erreur /today")
        await update.message.reply_text(f"Impossible de récupérer les matchs : {exc}")


async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /predict ID\nRécupère d'abord un identifiant avec /today.")
        return

    fixture_id = int(context.args[0])
    await update.message.reply_text(f"📊 Analyse du match {fixture_id}…")
    try:
        rows = api_get("predictions", {"fixture": fixture_id})
        if not rows:
            await update.message.reply_text(
                "Aucune prédiction disponible pour ce match. La compétition ou le match peut ne pas être couvert."
            )
            return

        item = rows[0]
        teams = item.get("teams", {})
        home = teams.get("home", {}).get("name", "Équipe domicile")
        away = teams.get("away", {}).get("name", "Équipe extérieure")
        pred = item.get("predictions", {})
        goals = pred.get("goals", {})

        # API-Football supplies expected-goal estimates for many, but not all, fixtures.
        try:
            home_xg = float(goals.get("home"))
            away_xg = float(goals.get("away"))
            if not (0 <= home_xg <= 8 and 0 <= away_xg <= 8):
                raise ValueError("Valeurs xG hors plage")
            model = estimate_markets(home_xg, away_xg)
            lines = [
                f"⚽ *{home} – {away}*",
                f"🧮 Buts attendus indicatifs : {home_xg:.2f} – {away_xg:.2f}",
                "",
                "*1X2 — modèle Poisson*",
                f"1️⃣ {home} : *{pct(model['home'])}*",
                f"❌ Nul : *{pct(model['draw'])}*",
                f"2️⃣ {away} : *{pct(model['away'])}*",
                "",
                "*Double chance*",
                f"1X : *{pct(model['home'] + model['draw'])}*",
                f"X2 : *{pct(model['draw'] + model['away'])}*",
                f"12 : *{pct(model['home'] + model['away'])}*",
                "",
                "*Buts*",
                f"Plus de 2,5 : *{pct(model['over25'])}*",
                f"Moins de 2,5 : *{pct(model['under25'])}*",
                f"BTTS — les deux marquent : *{pct(model['btts'])}*",
                "",
                "*3 scores les plus probables*"
            ]
            for probability, h, a in model["scores"]:
                lines.append(f"• {h}–{a} : {pct(probability)}")
            lines += [
                "",
                "*Mi-temps* : non estimée dans cette V1 (données mi-temps insuffisantes).",
                "*Corners* : non estimés avant match sans données fiables spécifiques.",
                "*Cartons* : non estimés avant match sans données fiables spécifiques.",
                "",
                "ℹ️ Estimations issues d'un modèle statistique simple utilisant les buts attendus fournis par l'API. "
                "Ce n'est pas une garantie ni un conseil de pari."
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except (TypeError, ValueError):
            advice = pred.get("advice") or "Pas de conseil automatique disponible."
            percent = pred.get("percent", {})
            lines = [
                f"⚽ *{home} – {away}*",
                "L'API ne fournit pas les buts attendus nécessaires au calcul Poisson.",
                f"Conseil brut du fournisseur : {advice}",
                "",
                f"Probabilités fournisseur : domicile {percent.get('home', 'N/D')}, "
                f"nul {percent.get('draw', 'N/D')}, extérieur {percent.get('away', 'N/D')}.",
                "",
                "BTTS, score exact, mi-temps, corners et cartons : données insuffisantes pour une estimation sérieuse."
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        log.exception("Erreur /predict")
        await update.message.reply_text(f"Erreur pendant l'analyse : {exc}")


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Variable TELEGRAM_BOT_TOKEN manquante.")
    if not FOOTBALL_API_KEY:
        raise RuntimeError("Variable FOOTBALL_API_KEY manquante.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("predict", predict))
    log.info("FOOTBALL PREDICTOR AI démarre…")
    app.run_polling()


if __name__ == "__main__":
    main()
