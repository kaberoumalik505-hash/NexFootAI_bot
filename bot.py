import os
import math
import logging
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# =========================================================
# NEXFOOTAI V12 - complet
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NexFootAI")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": FOOTBALL_API_KEY or ""}

TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Africa/Ouagadougou")
try:
    APP_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    APP_TIMEZONE = timezone.utc
    logger.warning("Fuseau horaire invalide : UTC utilisé.")

FORCED_TEAM_IDS = {
    "barca": 529, "barça": 529, "fc barcelona": 529, "barcelona": 529,
    "real madrid": 541, "real": 541,
    "psg": 85, "paris": 85,
    "argentine": 26, "argentina": 26,
    "bresil": 6, "brésil": 6, "brazil": 6,
    "france": 2,
    "burkina faso": 1121,
}

FINISHED_STATUSES = {"FT", "AET", "PEN"}

# --- Paramètres du modèle ---
LEAGUE_AVG = 1.30      # buts moyens par équipe et par match
HOME_BASE = 1.45       # buts moyens à domicile
AWAY_BASE = 1.15       # buts moyens à l'extérieur
DECAY = 0.92           # poids des matchs anciens (1 = tous égaux)
DAMPING = 0.85         # évite les prédictions extrêmes
DC_RHO = -0.10         # correction Dixon-Coles (0-0, 1-0, 0-1, 1-1)
MAX_GOALS = 10
N_MATCHES = 20


# =========================================================
# API
# =========================================================

def api_get(endpoint, params=None):
    if not FOOTBALL_API_KEY:
        raise RuntimeError("FOOTBALL_API_KEY est absente des variables Railway.")

    response = requests.get(
        f"{API_BASE}/{endpoint.lstrip('/')}",
        headers=HEADERS,
        params=params or {},
        timeout=25,
    )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Réponse JSON invalide de l'API-Football.")

    if data.get("errors"):
        raise RuntimeError(f"API-Football : {data['errors']}")

    return data.get("response", [])


async def api_get_async(endpoint, params=None):
    return await asyncio.to_thread(api_get, endpoint, params)


# =========================================================
# OUTILS
# =========================================================

def local_today():
    return datetime.now(APP_TIMEZONE).date()


def fixture_date(fx):
    return fx.get("fixture", {}).get("date", "") or ""


def fixture_id(fx):
    return fx.get("fixture", {}).get("id")


def match_names(fx):
    teams = fx.get("teams", {})
    return (
        teams.get("home", {}).get("name", "Domicile"),
        teams.get("away", {}).get("name", "Extérieur"),
    )


def split_message(message, limit=3900):
    if len(message) <= limit:
        return [message]
    parts, current = [], ""
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
    for part in split_message(message):
        await update.message.reply_text(part)


# =========================================================
# DONNÉES MATCHS
# =========================================================

async def get_recent_matches(team_id):
    """Derniers matchs terminés d'une équipe (du plus récent au plus ancien)."""
    matches = await api_get_async(
        "fixtures",
        {"team": team_id, "last": N_MATCHES, "timezone": TIMEZONE_NAME},
    )
    finished = [
        m for m in matches
        if m.get("fixture", {}).get("status", {}).get("short") in FINISHED_STATUSES
        and m.get("goals", {}).get("home") is not None
        and m.get("goals", {}).get("away") is not None
    ]
    finished.sort(key=fixture_date, reverse=True)
    return finished


async def get_team_by_name(query):
    normalized = query.strip().lower()

    if normalized in FORCED_TEAM_IDS:
        return {"id": FORCED_TEAM_IDS[normalized], "name": query.strip().title()}

    teams = await api_get_async("teams", {"search": query.strip()})
    if not teams:
        return None

    for item in teams:
        team = item.get("team", {})
        if team.get("name", "").lower() == normalized:
            return {"id": team.get("id"), "name": team.get("name")}

    team = teams[0].get("team", {})
    return {"id": team.get("id"), "name": team.get("name")}


async def get_team_fixtures(team_id):
    upcoming = await api_get_async(
        "fixtures", {"team": team_id, "next": 10, "timezone": TIMEZONE_NAME}
    )
    recent = await api_get_async(
        "fixtures", {"team": team_id, "last": 10, "timezone": TIMEZONE_NAME}
    )

    unique = {}
    for m in upcoming + recent:
        mid = fixture_id(m)
        if mid:
            unique[mid] = m

    today = local_today().isoformat()
    future = sorted(
        (m for m in unique.values() if fixture_date(m)[:10] >= today),
        key=fixture_date,
    )
    past = sorted(
        (m for m in unique.values() if fixture_date(m)[:10] < today),
        key=fixture_date,
        reverse=True,
    )
    return future + past


# =========================================================
# MODÈLE
# =========================================================

def wavg(values):
    """Moyenne pondérée : le premier élément (le plus récent) pèse le plus."""
    if not values:
        return None
    weights = [DECAY ** i for i in range(len(values))]
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def shrink(value, n, prior, k):
    """Ramène vers la valeur de référence si l'échantillon est petit."""
    if value is None or n <= 0:
        return prior
    c = n / (n + k)
    return c * value + (1 - c) * prior


def team_profile(matches, team_id):
    """Buts marqués/encaissés (global, domicile, extérieur). matches : récent -> ancien."""
    s_all, c_all = [], []
    s_home, c_home = [], []
    s_away, c_away = [], []

    for m in matches:
        teams = m.get("teams", {})
        goals = m.get("goals", {})
        hid = teams.get("home", {}).get("id")
        aid = teams.get("away", {}).get("id")
        hg, ag = goals.get("home"), goals.get("away")
        if hg is None or ag is None:
            continue

        if hid == team_id:
            s_home.append(hg)
            c_home.append(ag)
            s_all.append(hg)
            c_all.append(ag)
        elif aid == team_id:
            s_away.append(ag)
            c_away.append(hg)
            s_all.append(ag)
            c_all.append(hg)

    return {
        "n": len(s_all),
        "scored": wavg(s_all),
        "conceded": wavg(c_all),
        "home_n": len(s_home),
        "home_scored": wavg(s_home),
        "home_conceded": wavg(c_home),
        "away_n": len(s_away),
        "away_scored": wavg(s_away),
        "away_conceded": wavg(c_away),
    }


def venue_rates(profile, venue):
    """Buts marqués / encaissés attendus pour un lieu (home ou away)."""
    overall_s = shrink(profile["scored"], profile["n"], LEAGUE_AVG, 4)
    overall_c = shrink(profile["conceded"], profile["n"], LEAGUE_AVG, 4)
    s = shrink(profile[f"{venue}_scored"], profile[f"{venue}_n"], overall_s, 5)
    c = shrink(profile[f"{venue}_conceded"], profile[f"{venue}_n"], overall_c, 5)
    return s, c


def damp(ratio):
    return 1 + DAMPING * (ratio - 1)


def expected_goals(home_profile, away_profile):
    hs, hc = venue_rates(home_profile, "home")
    as_, ac = venue_rates(away_profile, "away")

    att_h = hs / LEAGUE_AVG
    def_h = hc / LEAGUE_AVG
    att_a = as_ / LEAGUE_AVG
    def_a = ac / LEAGUE_AVG

    lam_home = HOME_BASE * damp(att_h) * damp(def_a)
    lam_away = AWAY_BASE * damp(att_a) * damp(def_h)

    lam_home = max(0.25, min(lam_home, 3.5))
    lam_away = max(0.25, min(lam_away, 3.5))
    return lam_home, lam_away


def poisson(lmbda, k):
    return math.exp(-lmbda) * lmbda ** k / math.factorial(k)


def dc_tau(h, a, lh, la, rho):
    """Correction Dixon-Coles pour les petits scores."""
    if h == 0 and a == 0:
        return 1 - lh * la * rho
    if h == 0 and a == 1:
        return 1 + lh * rho
    if h == 1 and a == 0:
        return 1 + la * rho
    if h == 1 and a == 1:
        return 1 - rho
    return 1.0


def calculate_prediction(lh, la):
    grid = []
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            p = poisson(lh, h) * poisson(la, a) * dc_tau(h, a, lh, la, DC_RHO)
            grid.append((h, a, max(p, 0.0)))

    total = sum(p for _, _, p in grid)
    grid = [(h, a, p / total) for h, a, p in grid]

    home_win = sum(p for h, a, p in grid if h > a)
    draw = sum(p for h, a, p in grid if h == a)
    away_win = sum(p for h, a, p in grid if h < a)
    over25 = sum(p for h, a, p in grid if h + a >= 3)
    btts = sum(p for h, a, p in grid if h >= 1 and a >= 1)
    top = sorted(grid, key=lambda x: x[2], reverse=True)[:5]

    return {
        "home_win": home_win * 100,
        "draw": draw * 100,
        "away_win": away_win * 100,
        "over25": over25 * 100,
        "under25": (1 - over25) * 100,
        "btts": btts * 100,
        "lh": lh,
        "la": la,
        "top": top,
    }


def reliability(n_home, n_away):
    n = min(n_home, n_away)
    if n >= 15:
        return "correcte"
    if n >= 8:
        return "moyenne"
    return "faible"


# =========================================================
# COMMANDES
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ NEXFOOTAI V12\n\n"
        "/today - Matchs du jour\n"
        "/search equipe - Matchs d'une équipe\n"
        "/predict numero - Analyser un match de la dernière liste\n\n"
        "Exemples :\n/search barcelona\n/predict 1\n\n"
        "Ce sont des estimations statistiques, pas des certitudes."
    )


async def today_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Récupération des matchs du jour...")
    today = local_today().isoformat()

    try:
        fixtures = await api_get_async(
            "fixtures", {"date": today, "timezone": TIMEZONE_NAME}
        )
        if not fixtures:
            context.user_data.pop("last_list", None)
            await update.message.reply_text(f"Aucun match trouvé pour le {today}.")
            return

        fixtures.sort(key=fixture_date)
        fixtures = fixtures[:30]
        context.user_data["last_list"] = fixtures

        lines = [f"⚽ MATCHS DU {today}", ""]
        for i, m in enumerate(fixtures, start=1):
            home, away = match_names(m)
            ds = fixture_date(m)
            hour = ds[11:16] if len(ds) >= 16 else "?"
            league = m.get("league", {}).get("name", "Compétition")
            status = m.get("fixture", {}).get("status", {}).get("short", "")
            goals = m.get("goals", {})
            score = (
                f" | {goals.get('home')}-{goals.get('away')}"
                if status in FINISHED_STATUSES
                else ""
            )
            lines.append(f"{i}. {hour} | {league}\n   {home} - {away}{score}\n")

        lines.append("Pour analyser : /predict 1")
        await reply_long(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur /today")
        await update.message.reply_text(
            f"Impossible de récupérer les matchs.\nDétail : {str(error)[:500]}"
        )


async def search_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Indique une équipe.\nExemple : /search real madrid"
        )
        return

    query = " ".join(context.args).strip()
    await update.message.reply_text(f"🔎 Recherche des matchs de {query}...")

    try:
        team = await get_team_by_name(query)
        if not team or not team.get("id"):
            await update.message.reply_text(f"Aucune équipe trouvée : {query}")
            return

        fixtures = await get_team_fixtures(team["id"])
        if not fixtures:
            await update.message.reply_text(f"Aucun match pour {team['name']}.")
            return

        fixtures = fixtures[:20]
        context.user_data["last_list"] = fixtures

        lines = [f"📅 MATCHS DE {team['name'].upper()}", ""]
        for i, m in enumerate(fixtures, start=1):
            home, away = match_names(m)
            ds = fixture_date(m)
            date_text = ds[:16].replace("T", " ") if ds else "Date inconnue"
            league = m.get("league", {}).get("name", "Compétition")
            status = m.get("fixture", {}).get("status", {}).get("short", "")
            if status in FINISHED_STATUSES:
                goals = m.get("goals", {})
                score, icon = f" | {goals.get('home')}-{goals.get('away')}", "✅"
            else:
                score, icon = "", "⏳"
            lines.append(f"{i}. {icon} {date_text}\n   {league}\n   {home} - {away}{score}\n")

        lines.append("Pour analyser : /predict 1")
        await reply_long(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur /search")
        await update.message.reply_text(
            f"Erreur pendant la recherche.\nDétail : {str(error)[:500]}"
        )


async def predict_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /predict numero\nExemple : /predict 1")
        return

    fixtures = context.user_data.get("last_list")
    if not fixtures:
        await update.message.reply_text("Fais d'abord /today ou /search equipe.")
        return

    index = int(context.args[0]) - 1
    if not 0 <= index < len(fixtures):
        await update.message.reply_text(f"Choisis un numéro entre 1 et {len(fixtures)}.")
        return

    match = fixtures[index]
    teams = match.get("teams", {})
    home_id = teams.get("home", {}).get("id")
    away_id = teams.get("away", {}).get("id")
    home_name, away_name = match_names(match)

    await update.message.reply_text(f"⏳ Analyse de {home_name} - {away_name}...")

    try:
        home_matches, away_matches = await asyncio.gather(
            get_recent_matches(home_id),
            get_recent_matches(away_id),
        )

        if not home_matches or not away_matches:
            await update.message.reply_text(
                "Pas assez de matchs récents pour une analyse fiable."
            )
            return

        hp = team_profile(home_matches, home_id)
        ap = team_profile(away_matches, away_id)

        lh, la = expected_goals(hp, ap)
        r = calculate_prediction(lh, la)
        conf = reliability(hp["n"], ap["n"])

        top_lines = "\n".join(
            f"  {h}-{a} : {p * 100:.1f} %" for h, a, p in r["top"]
        )

        message = (
            f"⚽ {home_name} - {away_name}\n"
            f"{match.get('league', {}).get('name', '')}\n\n"
            f"Buts attendus : {r['lh']:.2f} - {r['la']:.2f}\n\n"
            f"1 ({home_name}) : {r['home_win']:.1f} %\n"
            f"N (nul) : {r['draw']:.1f} %\n"
            f"2 ({away_name}) : {r['away_win']:.1f} %\n\n"
            f"+2,5 buts : {r['over25']:.1f} %\n"
            f"-2,5 buts : {r['under25']:.1f} %\n"
            f"Les deux marquent : {r['btts']:.1f} %\n\n"
            f"Scores les plus probables :\n{top_lines}\n\n"
            f"Données : {hp['n']} et {ap['n']} matchs récents\n"
            f"Fiabilité : {conf}\n\n"
            "Estimation statistique : elle ne tient pas compte des blessures, "
            "suspensions, compositions ni de l'enjeu du match."
        )
        await reply_long(update, message)

    except Exception as error:
        logger.exception("Erreur /predict")
        await update.message.reply_text(
            f"Erreur pendant l'analyse.\nDétail : {str(error)[:500]}"
        )


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Erreur non gérée", exc_info=context.error)


# =========================================================
# LANCEMENT
# =========================================================

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN est absent des variables Railway.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_matches))
    app.add_handler(CommandHandler("search", search_team))
    app.add_handler(CommandHandler("predict", predict_match))
    app.add_error_handler(error_handler)

    logger.info("NexFootAI V12 démarré.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
