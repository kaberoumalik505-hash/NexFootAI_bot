
import os
import math
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

# =========================================================
# NEXFOOTAI V11
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NexFootAI")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

API_BASE = "https://v3.football.api-sports.io"

# API-Football : authentification directe par clé API
HEADERS = {
    "x-apisports-key": FOOTBALL_API_KEY or ""
}

# Fuseau horaire par défaut : Burkina Faso
TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Africa/Ouagadougou")

try:
    APP_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    APP_TIMEZONE = timezone.utc
    logger.warning("Fuseau horaire invalide : utilisation de UTC.")

# Équipes connues : identifiants API-Football
FORCED_TEAM_IDS = {
    "barca": 529,
    "barça": 529,
    "fc barcelona": 529,
    "barcelona": 529,
    "real madrid": 541,
    "real": 541,
    "psg": 85,
    "paris": 85,
    "argentine": 7,
    "argentina": 7,
    "bresil": 6,
    "brésil": 6,
    "brazil": 6,
    "france": 2,
    "burkina faso": 1121,
}

# Statuts considérés comme matchs terminés
FINISHED_STATUSES = {"FT", "AET", "PEN"}

# Valeurs de référence utilisées seulement lorsque
# les données récentes sont insuffisantes.
BASELINE_HOME_GOALS = 1.45
BASELINE_AWAY_GOALS = 1.15


# =========================================================
# API FOOTBALL
# =========================================================

def api_get(endpoint, params=None):
    """Effectue une requête à l'API-Football."""

    if not FOOTBALL_API_KEY:
        raise RuntimeError(
            "La variable FOOTBALL_API_KEY est absente de Railway."
        )

    url = f"{API_BASE}/{endpoint.lstrip('/')}"

    response = requests.get(
        url,
        headers=HEADERS,
        params=params or {},
        timeout=25,
    )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Réponse JSON invalide de l'API-Football.")

    errors = data.get("errors")

    if errors:
        raise RuntimeError(f"Erreur API-Football : {errors}")

    return data.get("response", [])


async def api_get_async(endpoint, params=None):
    """Évite de bloquer le bot Telegram pendant les appels API."""

    return await asyncio.to_thread(api_get, endpoint, params)


# =========================================================
# OUTILS GÉNÉRAUX
# =========================================================

def local_today():
    return datetime.now(APP_TIMEZONE).date()


def fixture_date(fixture):
    return fixture.get("fixture", {}).get("date", "")


def fixture_id(fixture):
    return fixture.get("fixture", {}).get("id")


def match_names(fixture):
    teams = fixture.get("teams", {})
    home = teams.get("home", {}).get("name", "Équipe domicile")
    away = teams.get("away", {}).get("name", "Équipe extérieure")
    return home, away


def poisson_probability(lmbda, goals):
    """Probabilité de marquer un nombre précis de buts."""

    if lmbda < 0:
        return 0.0

    return (
        math.exp(-lmbda)
        * (lmbda ** goals)
        / math.factorial(goals)
    )


def weighted_average(values):
    """Donne davantage de poids aux matchs les plus récents."""

    if not values:
        return None

    weights = list(range(len(values), 0, -1))
    total_weight = sum(weights)

    return sum(
        value * weight
        for value, weight in zip(values, weights)
    ) / total_weight


def shrink_to_baseline(value, sample_size, baseline):
    """
    Réduit l'influence d'une moyenne calculée sur peu de matchs.
    Plus l'échantillon est grand, moins la référence intervient.
    """

    if value is None or sample_size <= 0:
        return baseline

    confidence = sample_size / (sample_size + 5)

    return (
        confidence * value
        + (1 - confidence) * baseline
    )


def format_percentage(value):
    return f"{value:.1f} %"


def split_telegram_message(message, limit=3900):
    """Découpe les messages longs pour respecter Telegram."""

    if len(message) <= limit:
        return [message]

    parts = []
    current = ""

    for line in message.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                parts.append(current)
            current = line
        else:
            current += line

    if current:
        parts.append(current)

    return parts


async def reply_long(update, message):
    for part in split_telegram_message(message):
        await update.message.reply_text(part)


# =========================================================
# RÉCUPÉRATION DES MATCHS
# =========================================================

async def get_recent_matches(team_id):
    """Récupère les 12 derniers matchs d'une équipe."""

    matches = await api_get_async(
        "fixtures",
        {
            "team": team_id,
            "last": 12,
            "timezone": TIMEZONE_NAME,
        },
    )

    finished = [
        match for match in matches
        if match.get("fixture", {})
        .get("status", {})
        .get("short") in FINISHED_STATUSES
        and match.get("goals", {}).get("home") is not None
        and match.get("goals", {}).get("away") is not None
    ]

    finished.sort(
        key=fixture_date,
        reverse=True,
    )

    return finished[:12]


async def get_team_by_name(query):
    """Recherche une équipe dans l'API."""

    normalized = query.strip().lower()

    if normalized in FORCED_TEAM_IDS:
        return {
            "id": FORCED_TEAM_IDS[normalized],
            "name": query.strip().title(),
        }

    teams = await api_get_async(
        "teams",
        {"search": query.strip()},
    )

    if not teams:
        return None

    # On privilégie une correspondance exacte.
    for item in teams:
        team = item.get("team", {})
        if team.get("name", "").lower() == normalized:
            return {
                "id": team.get("id"),
                "name": team.get("name"),
            }

    team = teams[0].get("team", {})

    return {
        "id": team.get("id"),
        "name": team.get("name"),
    }


async def get_team_fixtures(team_id):
    """Récupère les matchs à venir et les derniers résultats."""

    upcoming = await api_get_async(
        "fixtures",
        {
            "team": team_id,
            "next": 20,
            "timezone": TIMEZONE_NAME,
        },
    )

    recent = await api_get_async(
        "fixtures",
        {
            "team": team_id,
            "last": 15,
            "timezone": TIMEZONE_NAME,
        },
    )

    unique = {}

    for match in upcoming + recent:
        match_id = fixture_id(match)
        if match_id:
            unique[match_id] = match

    today = local_today().isoformat()

    future = []
    past = []

    for match in unique.values():
        date_string = fixture_date(match)[:10]

        if date_string >= today:
            future.append(match)
        else:
            past.append(match)

    future.sort(key=fixture_date)
    past.sort(key=fixture_date, reverse=True)

    return future + past


# =========================================================
# ANALYSE STATISTIQUE DES ÉQUIPES
# =========================================================

def calculate_team_statistics(matches, team_id):
    """
    Calcule les buts marqués et encaissés :
    - tous terrains confondus ;
    - à domicile ;
    - à l'extérieur.
    """

    scored_all = []
    conceded_all = []

    scored_home = []
    conceded_home = []

    scored_away = []
    conceded_away = []

    for match in matches:
        teams = match.get("teams", {})
        goals = match.get("goals", {})

        home_id = teams.get("home", {}).get("id")
        away_id = teams.get("away", {}).get("id")

        home_goals = goals.get("home")
        away_goals = goals.get("away")

        if home_goals is None or away_goals is None:
            continue

        if home_id == team_id:
            scored = home_goals
            conceded = away_goals

            scored_home.append(scored)
            conceded_home.append(conceded)

        elif away_id == team_id:
            scored = away_goals
            conceded = home_goals

            scored_away.append(scored)
            conceded_away.append(conceded)

        else:
            continue

        scored_all.append(scored)
        conceded_all.append(conceded)

    return {
        "matches": len(scored_all),

        "scored": weighted_average(scored_all),
        "conceded": weighted_average(conceded_all),

        "home_scored": weighted_average(scored_home),
        "home_conceded": weighted_average(conceded_home),

        "away_scored": weighted_average(scored_away),
        "away_conceded": weighted_average(conceded_away),

        "home_count": len(scored_home),
        "away_count": len(scored_away),
    }


def expected_goals(home_stats, away_stats):
    """
    Estime les buts attendus à partir :
    - de l'attaque de l'équipe ;
    - de la défense adverse ;
    - du contexte domicile/extérieur ;
    - d'une référence générale lorsque les données manquent.

    Ce calcul est une estimation statistique, pas un xG mesuré.
    """

    home_attack = (
        home_stats["home_scored"]
        if home_stats["home_scored"] is not None
        else home_stats["scored"]
    )

    away_defence = (
        away_stats["away_conceded"]
        if away_stats["away_conceded"] is not None
        else away_stats["conceded"]
    )

    away_attack = (
        away_stats["away_scored"]
        if away_stats["away_scored"] is not None
        else away_stats["scored"]
    )

    home_defence = (
        home_stats["home_conceded"]
        if home_stats["home_conceded"] is not None
        else home_stats["conceded"]
    )

    # Moyenne attaque/défense adverse
    raw_home = None
    raw_away = None

    if home_attack is not None and away_defence is not None:
        raw_home = (
            0.58 * home_attack
            + 0.42 * away_defence
        )

    if away_attack is not None and home_defence is not None:
        raw_away = (
            0.58 * away_attack
            + 0.42 * home_defence
        )

    home_sample = min(
        home_stats["home_count"],
        away_stats["away_count"],
    )

    away_sample = min(
        away_stats["away_count"],
        home_stats["home_count"],
    )

    lambda_home = shrink_to_baseline(
        raw_home,
        home_sample,
        BASELINE_HOME_GOALS,
    )

    lambda_away = shrink_to_baseline(
        raw_away,
        away_sample,
        BASELINE_AWAY_GOALS,
    )

    # Bornes de sécurité pour le modèle de Poisson
    lambda_home = max(0.20, min(lambda_home, 3.80))
    lambda_away = max(0.20, min(lambda_away, 3.80))

    return lambda_home, lambda_away


# =========================================================
# MODÈLE DE PRÉDICTION
# =========================================================

def calculate_prediction(lambda_home, lambda_away):
    """Calcule les scores et les probabilités."""

    score_probs = []

    # On va jusqu'à 8 buts pour réduire la masse de probabilité perdue.
    for home_goals in range(9):
        for away_goals in range(9):

            probability = (
                poisson_probability(lambda_home, home_goals)
                * poisson_probability(lambda_away, away_goals)
            )

            score_probs.append(
                (home_goals, away_goals, probability)
            )

    total_probability = sum(
        item[2] for item in score_probs
    )

    if total_probability <= 0:
        raise RuntimeError("Impossible de calculer les probabilités.")

    # Normalisation des probabilités
    score_probs = [
        (home, away, probability / total_probability)
        for home, away, probability in score_probs
    ]

    home_win = sum(
        probability
        for home, away, probability in score_probs
        if home > away
    )

    draw = sum(
        probability
        for home, away, probability in score_probs
        if home == away
    )

    away_win = sum(
        probability
        for home, away, probability in score_probs
        if home < away
    )

    over_2_5 = sum(
        probability
        for home, away, probability in score_probs
        if home + away >= 3
    )

    under_2_5 = 1 - over_2_5

    both_teams_score = (
        (1 - math.exp(-lambda_home))
        * (1 - math.exp(-lambda_away))
    )

    top_scores = sorted(
        score_probs,
        key=lambda item: item[2],
        reverse=True,
    )[:5]

    return {
        "home_win": home_win * 100,
        "draw": draw * 100,
        "away_win": away_win * 100,

        "over_2_5": over_2_5 * 100,
        "under_2_5": under_2_5 * 100,

        "btts": both_teams_score * 100,

        "expected_home": lambda_home,
        "expected_away": lambda_away,
        "expected_total": lambda_home + lambda_away,

        "top_scores": top_scores,
    }


# =========================================================
# COMMANDE /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "⚽ BIENVENUE SUR NEXFOOTAI V11\n\n"
        "Analyse statistique des matchs de football.\n\n"
        "COMMANDES DISPONIBLES :\n\n"
        "/today - Matchs du jour\n"
        "/search nom_equipe - Rechercher une équipe\n"
        "/predict numero - Analyser un match\n\n"
        "Exemples :\n"
        "/search barcelona\n"
        "/predict 1\n\n"
        "Les probabilités sont des estimations statistiques. "
        "Elles ne garantissent pas le résultat d'un match."
    )

    await update.message.reply_text(message)


# =========================================================
# COMMANDE /TODAY
# =========================================================

async def today_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "⏳ Récupération des matchs du jour..."
    )

    today = local_today().isoformat()

    try:
        fixtures = await api_get_async(
            "fixtures",
            {
                "date": today,
                "timezone": TIMEZONE_NAME,
            },
        )

        if not fixtures:
            context.user_data.pop("today_fixtures", None)

            await update.message.reply_text(
                f"Aucun match trouvé pour le {today}."
            )
            return

        fixtures.sort(key=fixture_date)

        context.user_data["today_fixtures"] = fixtures[:30]
        context.user_data.pop("search_fixtures", None)

        lines = [
            f"⚽ MATCHS DU {today}",
            "",
        ]

        for index, match in enumerate(fixtures[:30], start=1):

            home, away = match_names(match)

            date_string = fixture_date(match)
            time_string = (
                date_string[11:16]
                if len(date_string) >= 16
                else "Heure inconnue"
            )

            league = match.get("league", {}).get(
                "name",
                "Compétition inconnue",
            )

            status = match.get("fixture", {}).get(
                "status", {}
            ).get("short", "")

            score = match.get("goals", {})

            if status in FINISHED_STATUSES:
                score_text = (
                    f" | Score : {score.get('home')}-"
                    f"{score.get('away')}"
                )
            else:
                score_text = ""

            lines.append(
                f"{index}. {time_string} | {league}\n"
                f"   {home} - {away}{score_text}\n"
            )

        lines.append(
            "Pour analyser un match : /predict 1"
        )

        await reply_long(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur /today")

        await update.message.reply_text(
            "Impossible de récupérer les matchs.\n"
            f"Détail : {str(error)[:500]}"
        )


# =========================================================
# COMMANDE /SEARCH
# =========================================================

async def search_team(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text(
            "Indique le nom d'une équipe.\n"
            "Exemple : /search real madrid"
        )
        return

    query = " ".join(context.args).strip()

    await update.message.reply_text(
        f"🔎 Recherche des matchs de {query}..."
    )

    try:
        team = await get_team_by_name(query)

        if not team or not team.get("id"):
            await update.message.reply_text(
                f"Aucune équipe trouvée pour : {query}"
            )
            return

        fixtures = await get_team_fixtures(team["id"])

        if not fixtures:
            await update.message.reply_text(
                f"Aucun match trouvé pour {team['name']}."
            )
            return

        context.user_data["search_fixtures"] = fixtures[:25]
        context.user_data.pop("today_fixtures", None)

        lines = [
            f"📅 MATCHS DE {team['name'].upper()}",
            "",
        ]

        for index, match in enumerate(fixtures[:25], start=1):

            home, away = match_names(match)

            date_string = fixture_date(match)
            date_text = (
                date_string[:16].replace("T", " ")
                if date_string
                else "Date inconnue"
            )

            league = match.get("league", {}).get(
                "name",
                "Compétition inconnue",
            )

            status = match.get("fixture", {}).get(
                "status", {}
            ).get("short", "")

            if status in FINISHED_STATUSES:
                goals = match.get("goals", {})
                score_text = (
                    f" | Score : {goals.get('home')}-"
                    f"{goals.get('away')}"
                )
                icon = "✅"
            else:
                score_text = ""
                icon = "⏳"

            lines.append(
                f"{index}. {icon} {date_text}\n"
                f"   {league}\n"
                f"   {home} - {away}{score_text}\n"
            )

        lines.append(
            "Pour analyser un match : /predict 1"
        )

        await reply_long(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur /search")

        await update.message.reply_text(
            "Erreur pendant la recherche.\n"
            f"Détail : {str(error)[:500]}"
        )


# =========================================================
# COMMANDE /PREDICT
# =========================================================

async def predict_match(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:
        await update.message.reply_text(
            "Utilisation : /predict numero\n"
            "Exemple : /predict 1"
        )
        return

    argument = context.args[0].strip()

    match = None

    # Sélection d'un match dans la dernière liste consultée.
    if argument.isdigit():

        index = int(argument) - 1

        if "search_fixtures" in context.user_data:
            fixtures = context.user_data["search_fixtures"]

            if 0 <= index < len(fixtures):
                match = fixtures[index]

        elif "today_fixtures" in context.user_data:
            fixtures = context.user_data["today_fixtures"]

  
