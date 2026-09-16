import datetime
import json
import re
import unicodedata

from pathlib import Path
from urllib.parse import urlparse

import asyncio

from playwright.async_api import async_playwright

from config import (
    BOOKMAKERS,
    BOOKMAKERS_LIST,
    MAX_MATCHES_PER_SITE,
    proxy_config,
)


# ============================================================
# DOSSIER DES DONNEES
# ============================================================

ROOT = Path(".")


# ============================================================
# RESSOURCES A BLOQUER
# ============================================================
#
# On NE bloque PAS les feuilles de style : certains de ces sites
# s'appuient sur le layout réel (dimensions/visibilité calculées
# via CSS) pour déclencher le chargement de la liste des matchs
# (listes virtualisées). Bloquer le CSS cassait ce déclenchement.
# ============================================================

BLOCKED_TYPES = {
    "image",
    "media",
    "font",
    "manifest",
    "texttrack",
}


# ============================================================
# ANALYTICS / TRACKERS / PUBLICITE
# ============================================================

BLOCKED_DOMAINS = (

    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "facebook.net",
    "hotjar",
    "clarity.ms",
    "segment.io",
    "mixpanel",
    "amplitude",
    "sentry.io",
    "analytics",
    "telemetry",
    "tracking",
    "tracker",
    "adservice",
    "adsystem",
    "pixel",
)


def should_block(route):

    request = route.request

    resource_type = request.resource_type
    url = request.url.lower()

    if resource_type in BLOCKED_TYPES:
        return True

    for domain in BLOCKED_DOMAINS:
        if domain in url:
            return True

    return False


# ============================================================
# MOTIF DE DETECTION DES LIENS DE MATCH
# ============================================================
#
# Format "classique" (Betwinner, Melbet, Megapari, Paripesa,
# Winwin, 1xbet) : /line/football/ID-championnat/ID-equipe1-equipe2
# ============================================================

MATCH_PATTERN = re.compile(r"/line/football/\d+-[^/]+/\d+-[^/?]+")

# Lien vers la page d'un championnat entier (ex. .../118593-uefa-
# europa-league), PAS vers un match précis : un seul segment
# "ID-slug" après /line/football/, pas deux.
COMPETITION_PATTERN = re.compile(
    r"/line/football/\d+-[^/?]+/?(?:\?.*)?$"
)


def with_mobile_param(url):

    if "platform_type=mobile" in url:
        return url

    separator = "&" if "?" in url else "?"

    return f"{url}{separator}platform_type=mobile"


def extract_teams_from_slug(href):
    """Best-effort : extrait les noms d'équipes depuis le slug de
    l'URL (.../ID-equipe1-equipe2), utilisé en repli si la page
    elle-même n'affiche pas clairement les noms."""

    try:

        match = re.search(r"/\d+-([^/?]+)$", href)

        if match:

            slug = match.group(1)
            parts = slug.split("-")

            if len(parts) >= 2:

                middle = len(parts) // 2

                team1 = " ".join(parts[:middle]).title()
                team2 = " ".join(parts[middle:]).title()

                return team1, team2

    except Exception:
        pass

    return None, None


# ============================================================
# DECOUVERTE DES MATCHS SUR LA PAGE DE LISTING
# ============================================================
#
# On scrolle (molette + touche Fin + JS + scroll des conteneurs
# internes, combinés car les sites ne réagissent pas tous au
# même déclencheur) et on déplie les accordéons de championnats
# repliés, jusqu'à avoir assez de liens de match ou jusqu'à ce
# que ça stagne.
# ============================================================

async def expand_accordions(page):

    try:

        return await page.evaluate(
            """
            () => {
                let compte = 0;

                // Méthode principale : attribut ARIA standard, utilisé
                // par la quasi-totalité des composants d'accordéon
                // (React/Vue...), quel que soit le nom de classe CSS
                // propre à chaque site.
                document.querySelectorAll(
                    '[aria-expanded="false"]'
                ).forEach(el => {
                    el.click();
                    compte++;
                });

                // Repli : ancienne classe personnalisée déjà repérée
                // sur certains sites de ce réseau, au cas où un site
                // n'utilise pas aria-expanded.
                document.querySelectorAll(
                    '.ui-accordion-trigger__arrow:not([data-deja-clique])'
                ).forEach(f => {
                    f.setAttribute('data-deja-clique', '1');
                    f.closest('[class*="accordion-trigger"]')?.click();
                    compte++;
                });

                // Repli n°2 : sur Betwinner/Melbet/Megapari/Winwin/1xbet/
                // Paripesa, le bouton "flèche" qui replie chaque
                // championnat (ex. "UEFA Europa League (22)") porte
                // directement la classe "ui-accordion__trigger" /
                // "ui-accordion-trigger", SANS aria-expanded. On le
                // cible donc lui-même, en le marquant après le premier
                // clic pour ne jamais le re-cliquer (sinon on le
                // refermerait au tour suivant au lieu de le laisser
                // ouvert).
                document.querySelectorAll(
                    '[class*="accordion__trigger"]:not([data-deja-ouvert]), ' +
                    '[class*="accordion-trigger"]:not([data-deja-ouvert])'
                ).forEach(btn => {
                    btn.setAttribute('data-deja-ouvert', '1');
                    btn.click();
                    compte++;
                });

                return compte;
            }
            """
        )

    except Exception:
        return 0


async def scroll_page(page):

    try:
        await page.mouse.wheel(0, 4000)
    except Exception:
        pass

    try:
        await page.keyboard.press("End")
    except Exception:
        pass

    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass

    try:

        await page.evaluate(
            """
            () => {
                document.querySelectorAll('div').forEach(el => {
                    if (el.scrollHeight > el.clientHeight + 50) {
                        el.scrollTop = el.scrollHeight;
                    }
                });
            }
            """
        )

    except Exception:
        pass


async def count_match_links(page):

    hrefs = await page.eval_on_selector_all(
        "a",
        "els => els.map(e => e.getAttribute('href'))"
    )

    count = sum(
        1 for h in hrefs
        if h and MATCH_PATTERN.search(h)
    )

    return hrefs, count


async def find_competition_links(page, base_url):
    """Récupère les liens vers les pages de championnat (ex.
    "Coupe d'Afrique des nations (48)"), triés par nombre de
    matchs annoncé décroissant, pour visiter les plus fournis
    en premier."""

    try:

        items = await page.eval_on_selector_all(
            "a",
            """
            els => els.map(e => ({
                href: e.getAttribute('href'),
                text: e.textContent || ""
            }))
            """
        )

    except Exception:
        return []

    links = []
    seen = set()

    for item in items:

        href = item.get("href")
        text = item.get("text", "")

        if not href:
            continue

        if MATCH_PATTERN.search(href):
            continue

        if not COMPETITION_PATTERN.search(href):
            continue

        full_url = (
            href if href.startswith("http")
            else base_url + href
        )

        if full_url in seen:
            continue

        seen.add(full_url)

        count_match = re.search(
            r"\((\d+)\)\s*$",
            text.strip()
        )

        count = int(count_match.group(1)) if count_match else 0

        links.append((full_url, count, text.strip()))

    # On priorise les grands championnats qui reviennent partout,
    # pour que TOUS les bookmakers regardent en premier les mêmes
    # compétitions (Ligue des Champions, Premier League, Liga...).
    # Sans ça, chaque site avance dans sa propre liste de championnats
    # dans un ordre différent, et le plafond de matchs par bookmaker
    # est atteint avant de croiser les mêmes matchs — donc rien à
    # comparer entre eux. Ordre = priorité (0 = en premier).
    GRANDS_CHAMPIONNATS = [
        "champions league",
        "premier league",
        "la liga",
        "laliga",
        "ligue 1",
        "serie a",
        "bundesliga",
        "europa league",
        "liga portugal",
        "eredivisie",
    ]

    def priorite(texte):

        texte_bas = texte.lower()

        for rang, mot_cle in enumerate(GRANDS_CHAMPIONNATS):
            if mot_cle in texte_bas:
                return rang

        return len(GRANDS_CHAMPIONNATS)

    links.sort(
        key=lambda item: (priorite(item[2]), -item[1])
    )

    return [url for url, _, _ in links]


async def click_maximize_buttons(page, bookmaker="", max_rounds=10):
    """
    Déploie les sections cachées derrière le bouton UI
    "Maximize" sur tous les bookmakers qui utilisent ce composant.

    Le sélecteur est volontairement précis pour ne pas cliquer sur
    d'autres contrôles :
      button.ui-nav-link-toggle[aria-label="Maximize"]
             [aria-expanded="false"]

    Si un bookmaker n'utilise pas ce bouton, la fonction ne fait rien.
    """
    selector = (
        'button.ui-nav-link-toggle[aria-label="Maximize"]'
        '[aria-expanded="false"]'
    )

    total_clicked = 0

    for tour in range(max_rounds):
        clicked_this_round = 0

        try:
            # Recompter à chaque tour : après un clic, le DOM peut être
            # recréé et de nouveaux boutons peuvent apparaître.
            count = await page.locator(selector).count()

            for i in range(count):
                try:
                    btn = page.locator(selector).nth(i)

                    if await btn.is_visible():
                        await btn.click(timeout=3000, force=True)
                        clicked_this_round += 1
                        total_clicked += 1
                        await page.wait_for_timeout(500)

                except Exception:
                    pass

            if clicked_this_round:
                await page.wait_for_timeout(1800)

            # Faire apparaître les éventuelles sections situées plus bas.
            await scroll_page(page)
            await page.wait_for_timeout(800)

            if clicked_this_round:
                print(
                    f"[{bookmaker}] Maximize : "
                    f"{clicked_this_round} bouton(s) ouvert(s) "
                    f"(tour {tour + 1})"
                )
            else:
                # Deux tours sans bouton = probablement tout est déjà ouvert.
                if tour >= 1:
                    break

        except Exception as error:
            print(f"[{bookmaker}] erreur Maximize : {error}")
            break

    return total_clicked


async def collect_hrefs_on_page(page, max_matches, max_stagnant=40):
    """Déplie les accordéons/boutons "Maximize" et scrolle la page
    actuellement chargée jusqu'à avoir assez de liens de match ou
    jusqu'à ce que ça stagne. Renvoie tous les hrefs vus."""

    await expand_accordions(page)
    await click_maximize_buttons(page, urlparse(page.url).netloc)
    # Délai plus long ici : la première passe peut ouvrir plusieurs
    # dizaines de championnats d'un coup (ex. "UEFA Europa League (22)"),
    # chacun déclenchant son propre chargement de matchs.
    await page.wait_for_timeout(4000)

    hrefs, found = await count_match_links(page)

    stagnant = 0

    while found < max_matches and stagnant < max_stagnant:

        await scroll_page(page)
        await expand_accordions(page)
        await click_maximize_buttons(page, urlparse(page.url).netloc, max_rounds=3)

        await page.wait_for_timeout(3000)

        hrefs, new_found = await count_match_links(page)

        if new_found <= found:
            stagnant += 1
        else:
            stagnant = 0

        found = new_found

    return hrefs


async def discover_matches(
    page,
    listing_url,
    max_matches,
    max_stagnant=40,
    max_competitions=15
):

    url = with_mobile_param(listing_url)

    await page.goto(
        url,
        timeout=180000,
        wait_until="domcontentloaded"
    )

    # Laisser le premier lot de matchs se charger.
    await page.wait_for_timeout(15000)

    parsed = urlparse(listing_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    matches = []
    seen = set()

    def add_hrefs(hrefs):

        for href in hrefs:

            if not href:
                continue

            if MATCH_PATTERN.search(href):

                full_url = (
                    href if href.startswith("http")
                    else base_url + href
                )

                if full_url not in seen:

                    seen.add(full_url)

                    team1, team2 = extract_teams_from_slug(href)

                    matches.append({
                        "url": full_url,
                        "equipe_1": team1,
                        "equipe_2": team2,
                    })

            if len(matches) >= max_matches:
                break

    add_hrefs(
        await collect_hrefs_on_page(page, max_matches, max_stagnant)
    )

    # Sur certains bookmakers, la page d'accueil "/line/football" ne
    # montre qu'un résumé (quelques matchs à la une + un widget de
    # championnats avec leur nombre de matchs, ex. "Coupe d'Afrique
    # des nations (48)"), sans lister les matchs eux-mêmes : il faut
    # alors visiter chaque championnat pour les récupérer.
    if len(matches) < max_matches:

        competition_links = await find_competition_links(page, base_url)

        for comp_url in competition_links[:max_competitions]:

            if len(matches) >= max_matches:
                break

            # Sur melbet en particulier, le site rebondit entre
            # plusieurs domaines miroirs au moment de la navigation
            # ("interrupted by another navigation" / timeout) : on
            # lui laisse plus de tentatives et un délai plus long
            # entre chacune, le temps que la redirection se
            # stabilise, au lieu d'abandonner tout le championnat
            # dès le premier échec.
            max_essais_champ = 4 if "melbet" in comp_url else 2

            reussi = False

            for tentative in range(max_essais_champ):

                try:

                    await page.goto(
                        with_mobile_param(comp_url),
                        timeout=60000,
                        wait_until="domcontentloaded"
                    )
                    await page.wait_for_timeout(4000)

                    add_hrefs(
                        await collect_hrefs_on_page(
                            page,
                            max_matches,
                            max_stagnant=15
                        )
                    )

                    reussi = True
                    break

                except Exception as error:

                    if tentative < max_essais_champ - 1:
                        await page.wait_for_timeout(
                            5000 + tentative * 3000
                        )
                    else:
                        print(
                            f"championnat ignoré ({comp_url}) : "
                            f"{error}"
                        )

            if not reussi:
                continue

    return matches


# ============================================================
# PARSING DES COTES SUR LA PAGE DE MATCH
# ============================================================
#
# On cherche la ligne exacte "1X2" puis on prend les 3 paires
# (libellé, cote) qui suivent — peu importe si le libellé exact
# est "1"/"X"/"2" ou "V1"/"Draw"/"V2", l'ORDRE (victoire équipe 1,
# nul, victoire équipe 2) est toujours le même sur ces sites.
# ============================================================

def to_lines(text):

    return [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]


def parse_1x2_block(lines):

    try:
        i = lines.index("1X2")
    except ValueError:
        return None

    block = {}
    pos = i + 1

    for _ in range(3):

        if pos + 1 < len(lines):
            block[lines[pos]] = lines[pos + 1]
            pos += 2

    return block if len(block) == 3 else None


def parse_total_block(lines):

    try:
        i_total = lines.index("Total")
    except ValueError:
        return {"Plus de": None, "Moins de": None}

    for j in range(i_total, min(i_total + 60, len(lines) - 3)):

        if lines[j] == "2.5 Plus de":

            plus = lines[j + 1]

            moins = (
                lines[j + 3]
                if (
                    j + 2 < len(lines)
                    and lines[j + 2] == "2.5 Moins de"
                )
                else None
            )

            return {"Plus de": plus, "Moins de": moins}

    return {"Plus de": None, "Moins de": None}


# ============================================================
# MARCHES SUPPLEMENTAIRES
# ============================================================
#
# Même principe que 1X2/Total ci-dessus : chercher le titre du
# marché dans le texte de la page, puis lire les paires
# (libellé, cote) qui suivent, jusqu'à tomber sur une ligne qui
# ne correspond plus au motif attendu (signe que le bloc suivant
# a commencé).
# ============================================================

TOTAL_LINE_PLUS = re.compile(r"^(\d+(?:\.\d+)?) Plus de$")
TOTAL_LINE_MOINS = re.compile(r"^(\d+(?:\.\d+)?) Moins de$")
HANDICAP_LABEL = re.compile(r"^[12] \([+-]?\d+(?:\.\d+)?\)$")
SCORE_LABEL = re.compile(r"^\d+-\d+$")


def parse_double_chance_block(lines):

    try:
        i = lines.index("Double chance")
    except ValueError:
        return {"1X": None, "12": None, "2X": None}

    result = {}
    pos = i + 1

    for _ in range(3):

        if (
            pos + 1 < len(lines)
            and lines[pos] in ("1X", "12", "2X")
        ):
            result[lines[pos]] = lines[pos + 1]
            pos += 2
        else:
            break

    return {
        "1X": result.get("1X"),
        "12": result.get("12"),
        "2X": result.get("2X"),
    }


def parse_btts_block(lines):

    try:
        i = lines.index("Deux équipes vont marquer")
    except ValueError:
        return {"Oui": None, "Non": None}

    result = {}
    pos = i + 1

    for _ in range(2):

        if (
            pos + 1 < len(lines)
            and lines[pos] in ("Oui", "Non")
        ):
            result[lines[pos]] = lines[pos + 1]
            pos += 2
        else:
            break

    return {
        "Oui": result.get("Oui"),
        "Non": result.get("Non"),
    }


def parse_all_totals_block(lines, max_span=60):
    """Toutes les lignes de Total but disponibles (1.5, 2, 2.5,
    etc.), pas seulement 2.5. Renvoie par ex. :
    {"1.5": {"Plus de": "1.4", "Moins de": "2.64"}, "2": {...}, ...}
    """

    try:
        i = lines.index("Total")
    except ValueError:
        return {}

    result = {}
    pos = i + 1
    limit = min(i + max_span, len(lines))

    while pos < limit:

        line = lines[pos]

        m_plus = TOTAL_LINE_PLUS.match(line)
        m_moins = TOTAL_LINE_MOINS.match(line) if not m_plus else None

        if m_plus and pos + 1 < len(lines):
            result.setdefault(m_plus.group(1), {})["Plus de"] = lines[pos + 1]
            pos += 2
            continue

        if m_moins and pos + 1 < len(lines):
            result.setdefault(m_moins.group(1), {})["Moins de"] = lines[pos + 1]
            pos += 2
            continue

        # Une ligne qui ne colle plus au motif "X Plus de"/"X Moins
        # de" signale la fin du bloc Total (ex. "Handicap").
        if result:
            break

        pos += 1

    return result


def parse_handicap_block(lines, max_span=40):
    """Renvoie les lignes de handicap telles qu'affichées, ex. :
    {"1 (-1)": "3.83", "2 (+1)": "1.2", "1 (0)": "1.56", "2 (0)": "2.21"}
    """

    try:
        i = lines.index("Handicap")
    except ValueError:
        return {}

    result = {}
    pos = i + 1
    limit = min(i + max_span, len(lines))

    while pos < limit:

        line = lines[pos]

        if HANDICAP_LABEL.match(line) and pos + 1 < len(lines):
            result[line] = lines[pos + 1]
            pos += 2
            continue

        if result:
            break

        pos += 1

    return result


def parse_correct_score_block(lines, max_span=60):
    """Score exact, ex. {"1-0": "5.85", "0-0": "7.19", ...}"""

    try:
        i = lines.index("Score exact")
    except ValueError:
        return {}

    result = {}
    pos = i + 1
    limit = min(i + max_span, len(lines))

    while pos < limit:

        line = lines[pos]

        if SCORE_LABEL.match(line) and pos + 1 < len(lines):
            result[line] = lines[pos + 1]
            pos += 2
            continue

        if result:
            break

        pos += 1

    return result


async def scrape_match(
    page,
    bookmaker,
    match,
    max_wait_cycles,
    nb_essais=2
):

    # melbet rebondit fréquemment entre domaines miroirs
    # (melbet-cm.com <-> melbetjp.com) au moment de la navigation :
    # on lui laisse plus de tentatives et plus de temps entre
    # chacune pour que la redirection se stabilise.
    if bookmaker == "melbet":
        nb_essais = 4

    for attempt in range(nb_essais):

        try:

            await page.goto(
                match["url"],
                timeout=120000,
                wait_until="domcontentloaded"
            )

            text = ""
            found = False

            for _ in range(max_wait_cycles):

                text = await page.inner_text("body")

                if "1X2" in text:
                    found = True
                    break

                await page.wait_for_timeout(2000)

            if not found:
                continue

            lines = to_lines(text)

            block = parse_1x2_block(lines)

            if not block:
                continue

            noms_bloc = list(block.keys())
            values = list(block.values())

            odds = {
                "V1": values[0],
                "X": values[1],
                "V2": values[2],
            }

            # extract_teams_from_slug (utilisé à la découverte) coupe
            # le slug de l'URL "en deux au milieu du nombre de mots" :
            # ça casse dès qu'une équipe a un nom plus long que
            # l'autre (ex. "barcelona-racing-de-santander" devient
            # "Barcelona Racing" / "De Santander" au lieu de
            # "Barcelona" / "Racing De Santander"). Les libellés
            # affichés juste sous "1X2" sur la page sont les vrais
            # noms d'équipe : on leur fait confiance quand ils sont
            # disponibles, plutôt qu'au découpage du slug.
            equipe_1 = match.get("equipe_1")
            equipe_2 = match.get("equipe_2")

            if len(noms_bloc) == 3:

                candidat_1, candidat_2 = noms_bloc[0], noms_bloc[2]

                if (
                    len(candidat_1) > 2
                    and candidat_1.lower() not in ("1", "x", "2", "draw", "nul")
                ):
                    equipe_1 = candidat_1

                if (
                    len(candidat_2) > 2
                    and candidat_2.lower() not in ("1", "x", "2", "draw", "nul")
                ):
                    equipe_2 = candidat_2

            total = parse_total_block(lines)

            return {

                "bookmaker": bookmaker,

                "equipe_1": equipe_1,

                "equipe_2": equipe_2,

                "1X2": odds,

                "Total_2.5": total,

                "Double_Chance": parse_double_chance_block(lines),

                "BTTS": parse_btts_block(lines),

                "Totals": parse_all_totals_block(lines),

                "Handicap": parse_handicap_block(lines),

                "Score_Exact": parse_correct_score_block(lines),

                "url": match["url"],

                "derniere_maj":
                    datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),

                "statut": "ok",
            }

        except Exception:
            # Pause avant de retenter, plus longue à chaque échec
            # successif : sur melbet en particulier, retenter
            # immédiatement retombe souvent dans la même redirection
            # en boucle qu'à l'essai précédent.
            await page.wait_for_timeout(4000 + attempt * 3000)
            continue

    return None


# ============================================================
# SCRAPER D'UN BOOKMAKER
# ============================================================

# Nombre d'onglets ouverts EN MÊME TEMPS, pour un seul bookmaker,
# lors de la visite des pages de match individuelles (la partie la
# plus lente du run, car elle représente jusqu'à 50 navigations
# séquentielles par site). Les bookmakers restent traités un par un
# (comme avant) pour ne jamais dépasser ce nombre de connexions
# simultanées via le proxy — seule la phase "détail de chaque match"
# à l'intérieur d'un bookmaker est parallélisée.
MATCH_CONCURRENCY = 8


async def scrape_bookmaker(
    context,
    bookmaker,
    config,
    max_matches,
    max_wait_cycles,
    concurrency=MATCH_CONCURRENCY
):

    listing_url = config["url"]

    result = []
    matches = []

    try:

        discovery_page = await context.new_page()

        try:

            for attempt in range(2):

                try:

                    matches = await discover_matches(
                        discovery_page,
                        listing_url,
                        max_matches
                    )

                    break

                except Exception as error:

                    print(
                        f"[{bookmaker}] "
                        f"erreur découverte (essai {attempt + 1}/2) : "
                        f"{error}"
                    )

                    await discovery_page.wait_for_timeout(3000)

        finally:
            await discovery_page.close()

        print(
            f"[{bookmaker}] "
            f"{len(matches)} match(s) découvert(s)"
        )

        if matches:

            semaphore = asyncio.Semaphore(concurrency)

            async def scrape_one(match):
                async with semaphore:
                    match_page = await context.new_page()
                    try:
                        return await scrape_match(
                            match_page,
                            bookmaker,
                            match,
                            max_wait_cycles
                        )
                    finally:
                        await match_page.close()

            scraped = await asyncio.gather(
                *(scrape_one(match) for match in matches)
            )

            result = [data for data in scraped if data]

    except Exception as error:

        print(
            f"[{bookmaker}] "
            f"ERREUR : {error}"
        )

    output = ROOT / f"{bookmaker}.json"

    output.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"[{bookmaker}] "
        f"{len(result)} matchs enregistrés "
        f"(sur {len(matches)} découverts)"
    )


# ============================================================
# 1WIN — moteur totalement différent (app Vue.js), et surtout
# pas besoin de proxy : l'IP US (celle du runner GitHub) n'est
# pas bloquée sur ce site, contrairement aux autres.
# ============================================================

WIN1_LISTING_URLS = [
    "https://1win.com/fr-CI/betting/prematch/football-18?p=mvh5&platform_type=mobile",
    "https://1win.com/fr-CI/betting/prematch/football-18/"
    "uefa-champions-league-39437?p=mvh5&platform_type=mobile",
    "https://1win.com/fr-CI/betting/prematch/football-18/"
    "league-cup-983?p=mvh5&platform_type=mobile",
    "https://1win.com/fr-CI/betting/prematch/football-18/"
    "premier-league-919?p=mvh5&platform_type=mobile",
    "https://1win.com/fr-CI/betting/prematch/football-18/"
    "laliga-1232?p=mvh5&platform_type=mobile",
]
WIN1_MAX_TENTATIVES = 300  # jusqu'à 10 min pour que les cartes se chargent

WIN1_MOTIF_COTE = re.compile(r"\d\.\d")


async def extraire_cartes_1win(page):
    """Extrait les cartes 1win sans dépendre d'un libellé de marché
    précis. 1win change régulièrement les textes/classes des marchés.
    On conserve le texte complet de la carte et les blocs de cotes pour
    permettre au parser de reconnaître plusieurs variantes."""
    return await page.evaluate(
        """
        () => {
            const cartes = document.querySelectorAll('[data-qa="match-card"]');
            const resultat = [];
            cartes.forEach(carte => {
                const teamsEl = carte.querySelector('[data-scope="TeamNames"]');
                const oddsEl = carte.querySelector('[data-qa="matchCardBaseOdds"]');
                const oddsNodes = [...carte.querySelectorAll(
                    '[data-qa*="Odds"], [data-qa*="odd"], [class*="odd" i]'
                )];
                const oddsTexts = oddsNodes.map(e => (e.innerText || '').trim()).filter(Boolean);
                const lienEl = carte.querySelector('a[href]')
                    || carte.closest('a[href]');
                resultat.push({
                    teamsText: teamsEl ? teamsEl.innerText : '',
                    oddsText: oddsEl ? oddsEl.innerText : '',
                    cardText: carte.innerText || '',
                    oddsTexts: oddsTexts,
                    html: carte.innerHTML || '',
                    lien: lienEl ? lienEl.href : ''
                });
            });
            return resultat;
        }
        """
    )


# 1win traduit certains noms d'équipe en français (ex. "Palais de
# Cristal" pour Crystal Palace), ce qui empêche comparateur.py de les
# rapprocher des mêmes matchs chez les autres bookmakers, qui gardent
# la graphie standard. Table construite à partir des traductions
# repérées dans nos runs — à compléter si d'autres apparaissent.
WIN1_ALIAS_EQUIPES = {
    "palais de cristal": "Crystal Palace",
    "celtique": "Celtic",
    "come": "Como",
    "seville": "Sevilla",
    "naples": "Napoli",
    "lentille": "Lens",
    "foret de nottingham": "Nottingham Forest",
    "ville de coventry": "Coventry City",
    "ville de norwich": "Norwich City",
    "ville de hull": "Hull City",
    "ville de fleetwood": "Fleetwood Town",
    "ville d ipswich": "Ipswich Town",
    "fc barcelone": "Barcelona",
    "union royale saint gilloise": "Royale Union Saint-Gilloise",
}


def traduire_equipe_1win(nom):
    """Convertit un nom d'équipe traduit par 1win vers la graphie
    standard utilisée par les autres bookmakers, quand on la connaît."""

    if not nom:
        return nom

    cle = unicodedata.normalize(
        "NFKD", nom
    ).encode("ascii", "ignore").decode("ascii").lower().strip()

    cle = re.sub(r"[^a-z0-9]+", " ", cle).strip()

    return WIN1_ALIAS_EQUIPES.get(cle, nom)


def _normaliser_cote(valeur):
    """Retourne une cote décimale plausible sous forme de chaîne."""
    if valeur is None:
        return None
    valeur = str(valeur).strip().replace(',', '.')
    m = re.fullmatch(r'(?:[1-9]\d?|0)\.\d{1,3}', valeur)
    if not m:
        return None
    try:
        n = float(valeur)
        if 1.01 <= n <= 1000:
            return valeur
    except ValueError:
        pass
    return None


def _extraire_cotes_texte(texte):
    """Extrait les nombres qui ressemblent à des cotes, en évitant
    les dates, scores et nombres entiers de l'interface."""
    if not texte:
        return []
    valeurs = []
    for m in re.finditer(r'(?<!\d)(\d{1,3}[\.,]\d{1,3})(?!\d)', texte):
        cote = _normaliser_cote(m.group(1))
        if cote and cote not in valeurs:
            valeurs.append(cote)
    return valeurs


def parser_carte_1win(carte, url_source):
    """Parser tolérant au nouveau rendu 1win.

    Ancienne version : exigeait littéralement "Full Time Result" puis
    les lignes 1/X/2. Nouveau rendu : le marché peut être traduit,
    abrégé, ou ne plus exposer ce titre dans matchCardBaseOdds.
    On essaie donc d'abord les libellés 1/X/2, puis les trois premières
    cotes plausibles du bloc principal.
    """
    lignes_equipes = [
        l.strip() for l in carte.get("teamsText", "").split("\n") if l.strip()
    ]

    # Repli : certaines variantes n'alimentent plus TeamNames.
    if len(lignes_equipes) < 2:
        lignes_equipes = [
            l.strip() for l in carte.get("cardText", "").split("\n") if l.strip()
        ]

    if len(lignes_equipes) < 2:
        return None

    equipe_1, equipe_2 = lignes_equipes[0], lignes_equipes[1]
    equipe_1 = traduire_equipe_1win(equipe_1)
    equipe_2 = traduire_equipe_1win(equipe_2)

    textes_cotes = []
    for cle in ("oddsText", "cardText"):
        if carte.get(cle):
            textes_cotes.append(carte[cle])
    textes_cotes.extend(carte.get("oddsTexts", []))

    lignes_cotes = []
    for texte in textes_cotes:
        lignes_cotes.extend(
            l.strip() for l in texte.split("\n") if l.strip()
        )

    resultat_1x2 = {}
    correspondance = {"1": "V1", "x": "X", "2": "V2",
                      "home": "V1", "draw": "X", "away": "V2",
                      "n": "X", "nul": "X", "match nul": "X"}

    # 1) Cherche des couples label -> cote, avec ou sans titre de marché.
    for i, label_brut in enumerate(lignes_cotes):
        label = label_brut.strip().lower().rstrip('.')
        if label in correspondance and i + 1 < len(lignes_cotes):
            cote = _normaliser_cote(lignes_cotes[i + 1])
            if cote:
                resultat_1x2[correspondance[label]] = cote

    # 2) Cherche spécifiquement autour de "Full Time Result" et variantes.
    if len(resultat_1x2) < 2:
        texte_global = "\n".join(lignes_cotes)
        motifs_marche = [
            r'full\s*time\s*result', r'1x2', r'3\s*way',
            r'resultat\s*final', r'resultat\s*du\s*match',
            r'issue\s*du\s*match', r'ganador\s*del\s*partido'
        ]
        for motif in motifs_marche:
            m = re.search(motif, texte_global, re.I)
            if not m:
                continue
            apres = texte_global[m.end():m.end() + 700]
            labels = re.findall(r'(?i)(?:^|\n)\s*(1|x|2)\s*(?:\n|\s)', apres)
            cotes = _extraire_cotes_texte(apres)
            for label, cote in zip(labels, cotes):
                resultat_1x2[correspondance[label.lower()]] = cote
            if resultat_1x2:
                break

    # 3) Dernier repli : dans le bloc de base 1win, les 3 premières cotes
    # décimales sont généralement le triplet 1/X/2 quand les libellés
    # sont rendus uniquement sous forme de boutons.
    if len(resultat_1x2) < 3:
        bloc = carte.get("oddsText", "") or ""
        cotes = _extraire_cotes_texte(bloc)
        if len(cotes) >= 3:
            resultat_1x2.setdefault("V1", cotes[0])
            resultat_1x2.setdefault("X", cotes[1])
            resultat_1x2.setdefault("V2", cotes[2])

    if not resultat_1x2:
        return None

    # Total 2.5 : on ne l'invente jamais. On le récupère seulement si
    # des libellés over/under (ou plus/moins) sont présents dans la carte.
    total_25 = {"Plus de": None, "Moins de": None}
    texte_total = "\n".join(textes_cotes)
    lignes_total = [l.strip() for l in texte_total.split("\n") if l.strip()]
    for i, ligne in enumerate(lignes_total):
        low = ligne.lower().replace(',', '.')
        if re.search(r'(plus|over|o)\s*(?:de|than)?\s*2[\.]?5', low):
            if i + 1 < len(lignes_total):
                total_25["Plus de"] = _normaliser_cote(lignes_total[i + 1]) or total_25["Plus de"]
        if re.search(r'(moins|under|u)\s*(?:de|than)?\s*2[\.]?5', low):
            if i + 1 < len(lignes_total):
                total_25["Moins de"] = _normaliser_cote(lignes_total[i + 1]) or total_25["Moins de"]

    return {
        "bookmaker": "1win",
        "equipe_1": equipe_1,
        "equipe_2": equipe_2,
        "1X2": resultat_1x2,
        "Total_2.5": total_25,
        # Lien vers la page individuelle du match (ex. .../betting/
        # match/sport/brentford-vs-chelsea-39631841?p=mvh5), utilisé
        # ensuite pour aller chercher les autres marchés.
        "url_match": carte.get("lien") or None,
        "url": url_source,
        "derniere_maj": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "statut": "ok",
    }


WIN1_MOTIF_VALEUR_MARCHE = re.compile(r"^\d+(?:[.,]\d+)?$")
WIN1_TOTAL_LIGNE = re.compile(
    r"^(Moins De|Au-Dessus De)\s+(\d+(?:[.,]\d+)?)$", re.IGNORECASE
)
WIN1_HANDICAP_LIGNE = re.compile(r"^(.+?)\s+([+-]?\d+(?:[.,]\d+)?)$")
WIN1_TITRES_BTTS = [
    "Les deux équipes vont marquer",
    "Deux équipes vont marquer",
    "Les deux équipes marquent",
]


def parser_totals_1win(lignes):
    """
    1win affiche la table des Totaux sous forme "Moins De 0.5" /
    valeur / "Au-Dessus De 0.5" / valeur (répété pour chaque seuil),
    au lieu de "0.5 Plus de" comme les autres bookmakers. Renvoie
    {"0.5": {"Plus de": "1.02", "Moins de": "13.5"}, "1.0": {...}, ...}
    """
    resultat = {}
    for i, ligne in enumerate(lignes):
        m = WIN1_TOTAL_LIGNE.match(ligne)
        if not m or i + 1 >= len(lignes):
            continue
        sens = m.group(1).lower()
        seuil = m.group(2).replace(",", ".")
        valeur = lignes[i + 1]
        cle = "Plus de" if sens == "au-dessus de" else "Moins de"
        resultat.setdefault(seuil, {})[cle] = valeur
    return resultat


def parser_double_chance_1win(lignes, equipe_1, equipe_2):
    """
    1win libelle la double chance avec les noms d'équipe (ex.
    "Brentford Ou Match Nul") plutôt qu'avec les jetons 1X/12/2X.
    On retrouve le titre "Double chance" puis on associe chaque
    libellé (parfois sur deux lignes) à 1X/12/2X selon les noms qui
    y apparaissent.
    """
    try:
        depart = lignes.index("Double chance")
    except ValueError:
        return {"1X": None, "12": None, "2X": None}

    e1, e2 = equipe_1.lower(), equipe_2.lower()
    resultat = {"1X": None, "12": None, "2X": None}
    tampon = []
    trouvees = 0

    for ligne in lignes[depart + 1: depart + 31]:

        if trouvees >= 3:
            break

        if WIN1_MOTIF_VALEUR_MARCHE.match(ligne):

            if tampon:
                libelle = " ".join(tampon).lower()
                contient_e1 = e1 in libelle
                contient_e2 = e2 in libelle
                contient_nul = "nul" in libelle

                if contient_e1 and contient_nul:
                    resultat["1X"] = ligne
                elif contient_e1 and contient_e2:
                    resultat["12"] = ligne
                elif contient_e2 and contient_nul:
                    resultat["2X"] = ligne

                trouvees += 1
                tampon = []

            continue

        tampon.append(ligne)

    return resultat


def parser_handicap_1win(lignes, equipe_1, equipe_2):
    """
    1win libelle le handicap "Équipe -3.75" / "Équipe 3.75" (sans
    parenthèses, et sans "+" explicite côté positif) — contrairement
    à ma première hypothèse. On convertit en "1 (-1)" / "2 (+1)" pour
    rester comparable aux autres bookmakers (voir HANDICAP_LABEL).
    """
    try:
        depart = lignes.index("Handicap")
    except ValueError:
        return {}

    e1, e2 = equipe_1.lower(), equipe_2.lower()
    resultat = {}
    limite = min(depart + 81, len(lignes) - 1)

    for i in range(depart + 1, limite):

        m = WIN1_HANDICAP_LIGNE.match(lignes[i])

        if not m:
            continue

        nom = m.group(1).strip().lower()
        valeur = m.group(2).replace(",", ".")

        if e1 in nom:
            jeton = "1"
        elif e2 in nom:
            jeton = "2"
        else:
            continue

        # 1win n'affiche pas le "+" pour les valeurs positives
        # (ex. "Lille 3.75") : on l'ajoute pour matcher le format
        # "2 (+1)" utilisé par les autres bookmakers.
        if not valeur.startswith(("-", "+")) and valeur != "0":
            valeur = f"+{valeur}"

        resultat[f"{jeton} ({valeur})"] = lignes[i + 1]

    return resultat


def parser_btts_1win(lignes):
    """Titre exact inconnu côté 1win (non visible sur nos captures) :
    on essaie plusieurs formulations plausibles."""

    for titre in WIN1_TITRES_BTTS:

        try:
            depart = lignes.index(titre)
        except ValueError:
            continue

        resultat = {}
        pos = depart + 1

        for _ in range(2):

            if pos + 1 < len(lignes) and lignes[pos] in ("Oui", "Non"):
                resultat[lignes[pos]] = lignes[pos + 1]
                pos += 2
            else:
                break

        return {"Oui": resultat.get("Oui"), "Non": resultat.get("Non")}

    return {"Oui": None, "Non": None}


def parser_score_exact_1win(lignes, max_span=60):
    """Le format "1-0", "0-0" est le même quel que soit le bookmaker :
    on réutilise directement SCORE_LABEL."""

    try:
        depart = lignes.index("Score exact")
    except ValueError:
        return {}

    resultat = {}
    pos = depart + 1
    limite = min(depart + max_span, len(lignes))

    while pos < limite:

        ligne = lignes[pos]

        if SCORE_LABEL.match(ligne) and pos + 1 < len(lignes):
            resultat[ligne] = lignes[pos + 1]
            pos += 2
            continue

        if resultat:
            break

        pos += 1

    return resultat


async def extraire_details_marches_1win(page, equipe_1, equipe_2):
    """
    Va chercher, sur la page individuelle d'un match 1win déjà
    ouverte, les marchés Double chance / Total (tous les seuils) /
    Handicap / BTTS / Score exact — pour que 1win soit comparable
    aux autres bookmakers sur les mêmes marchés dans comparateur.py.
    """

    texte = ""

    for _ in range(6):

        texte = await page.inner_text("body")

        if "Double chance" in texte or "Total" in texte:
            break

        await page.wait_for_timeout(2000)

    lignes = to_lines(texte)

    totals = parser_totals_1win(lignes)

    return {
        "Total_2.5": totals.get(
            "2.5", {"Plus de": None, "Moins de": None}
        ),
        "Totals": totals,
        "Double_Chance": parser_double_chance_1win(
            lignes, equipe_1, equipe_2
        ),
        "Handicap": parser_handicap_1win(lignes, equipe_1, equipe_2),
        "BTTS": parser_btts_1win(lignes),
        "Score_Exact": parser_score_exact_1win(lignes),
    }


async def ouvrir_plus_de_matchs_1win(page, max_tours=20):
    """
    1win masque une partie des compétitions derrière des boutons
    "Maximize". On clique explicitement sur ces boutons pour déployer
    davantage de matchs avant de lire les cartes.
    """
    precedent = -1
    sans_nouveau = 0

    for tour in range(max_tours):
        try:
            # Cible le bouton fourni par l'interface 1win :
            # <button aria-label="Maximize" ... class="ui-nav-link-toggle ...">
            cliques = await page.locator(
                'button.ui-nav-link-toggle[aria-label="Maximize"]'
                '[aria-expanded="false"]'
            ).count()

            if cliques:
                for i in range(cliques):
                    try:
                        await page.locator(
                            'button.ui-nav-link-toggle[aria-label="Maximize"]'
                            '[aria-expanded="false"]'
                        ).nth(i).click(
                            timeout=3000,
                            force=True
                        )
                    except Exception:
                        pass

                await page.wait_for_timeout(1800)

            # Faire apparaître les sections éventuellement chargées plus bas.
            await page.mouse.wheel(0, 5000)
            await page.keyboard.press("End")
            await page.wait_for_timeout(1200)

            nb_cartes = await page.locator(
                '[data-qa="match-card"]'
            ).count()

            if nb_cartes <= precedent:
                sans_nouveau += 1
            else:
                sans_nouveau = 0
                print(
                    f"[1win] déploiement : {nb_cartes} carte(s)"
                )

            precedent = nb_cartes

            if sans_nouveau >= 3:
                break

        except Exception:
            break

    # Remonter en haut n'est pas nécessaire pour extraire les cartes :
    # le DOM contient aussi les cartes chargées hors écran.
    return precedent


async def scrape_1win(playwright):

    result = []
    equipes_vues = set()

    try:

        # Pas de proxy pour 1win : l'IP du runner n'est pas
        # bloquée sur ce site.
        browser = await playwright.chromium.launch(headless=True)

        page = await browser.new_page(
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"}
        )

        # On visite la page générale ET les championnats spécifiques
        # demandés (Ligue des Champions, Coupe de la Ligue, Premier
        # League) : ça évite de dépendre uniquement du déploiement
        # des sections sur la page "football-18" globale.
        for url in WIN1_LISTING_URLS:

            try:

                await page.goto(
                    url,
                    timeout=1200000,
                    wait_until="domcontentloaded"
                )

                # Laisser l'application Vue.js initialiser les championnats.
                await page.wait_for_timeout(5000)

                # Fonction dédiée à 1win : clique sur les boutons "Maximize"
                # (le chevron "v" à droite de chaque championnat) ET scrolle
                # jusqu'en bas pour forcer le chargement des championnats
                # suivants. C'est ce qui débloque le plafond à 13 matchs.
                await ouvrir_plus_de_matchs_1win(page, max_tours=30)

                cartes = []

                for tentative in range(WIN1_MAX_TENTATIVES):

                    cartes = await extraire_cartes_1win(page)

                    nb_avec_cotes = sum(
                        1 for c in cartes
                        if WIN1_MOTIF_COTE.search(c["oddsText"])
                    )

                    if (
                        len(cartes) > 0
                        and nb_avec_cotes >= len(cartes) * 0.5
                    ):

                        print(
                            f"[1win] {url} : {len(cartes)} carte(s) "
                            f"détectée(s), {nb_avec_cotes} avec cotes, "
                            f"après {tentative * 2}s"
                        )

                        break

                # Fichier de diagnostic (une seule fois) : aucun lien
                # n'a été trouvé dans les cartes lors du dernier run
                # (url_match toujours null) — on écrit le HTML brut
                # d'une carte pour comprendre comment 1win structure
                # la navigation vers la page d'un match.
                debug_1win_html = ROOT / "debug_1win_carte.html"

                if not debug_1win_html.exists() and cartes:

                    debug_1win_html.write_text(
                        cartes[0].get("html", ""),
                        encoding="utf-8"
                    )

                    await page.wait_for_timeout(2000)

                rejetes = 0
                for carte in cartes:

                    parsed = parser_carte_1win(carte, url)

                    if not parsed:
                        rejetes += 1
                        continue

                    cle = (parsed["equipe_1"], parsed["equipe_2"])

                    # On évite les doublons : un même match peut
                    # apparaître à la fois sur la page générale et
                    # sur la page de son championnat.
                    if cle in equipes_vues:
                        continue

                    equipes_vues.add(cle)
                    result.append(parsed)

                if rejetes:
                    print(f"[1win] {url} : {rejetes} carte(s) rejetée(s) par le parser")

            except Exception as error:

                print(f"[1win] ERREUR sur {url} : {error}")

        # Deuxième passage : on va chercher, sur la page individuelle
        # de CHAQUE match trouvé, les marchés Double chance, Total
        # (tous les seuils), Handicap, BTTS et Score exact — pour que
        # 1win soit comparable aux autres bookmakers sur ces mêmes
        # marchés (voir comparateur.py). Plusieurs onglets en
        # parallèle pour ne pas exploser la durée du run.
        a_enrichir = [p for p in result if p.get("url_match")]

        if a_enrichir:

            semaphore = asyncio.Semaphore(MATCH_CONCURRENCY)

            async def enrichir(parsed):

                async with semaphore:

                    detail_page = await browser.new_page(
                        locale="fr-FR",
                        extra_http_headers={
                            "Accept-Language": "fr-FR,fr;q=0.9"
                        }
                    )

                    try:

                        for tentative in range(2):

                            try:

                                await detail_page.goto(
                                    parsed["url_match"],
                                    timeout=60000,
                                    wait_until="domcontentloaded"
                                )

                                details = (
                                    await extraire_details_marches_1win(
                                        detail_page,
                                        parsed["equipe_1"],
                                        parsed["equipe_2"]
                                    )
                                )

                                if (
                                    details["Total_2.5"].get("Plus de")
                                    or details["Total_2.5"].get(
                                        "Moins de"
                                    )
                                ):
                                    parsed["Total_2.5"] = (
                                        details["Total_2.5"]
                                    )

                                parsed["Totals"] = details["Totals"]
                                parsed["Double_Chance"] = (
                                    details["Double_Chance"]
                                )
                                parsed["Handicap"] = details["Handicap"]
                                parsed["BTTS"] = details["BTTS"]
                                parsed["Score_Exact"] = (
                                    details["Score_Exact"]
                                )

                                return

                            except Exception:
                                await detail_page.wait_for_timeout(4000)

                        print(
                            f"[1win] détail indisponible : "
                            f"{parsed['equipe_1']} vs "
                            f"{parsed['equipe_2']}"
                        )

                    finally:
                        await detail_page.close()

            await asyncio.gather(
                *(enrichir(p) for p in a_enrichir)
            )

            print(
                f"[1win] détail récupéré pour {len(a_enrichir)} "
                f"match(s)"
            )

        # Les matchs sans lien de détail gardent quand même des
        # champs vides pour ces marchés, pour rester dans le même
        # format que les autres bookmakers.
        for parsed in result:
            parsed.setdefault("Totals", {})
            parsed.setdefault(
                "Double_Chance", {"1X": None, "12": None, "2X": None}
            )
            parsed.setdefault("Handicap", {})
            parsed.setdefault("BTTS", {"Oui": None, "Non": None})
            parsed.setdefault("Score_Exact", {})

        await browser.close()

    except Exception as error:

        print(f"[1win] ERREUR : {error}")

    output = ROOT / "1win.json"

    output.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"[1win] "
        f"{len(result)} matchs enregistrés"
    )


# ============================================================
# MAIN
# ============================================================

# Nombre de cycles d'attente (2s chacun) avant d'abandonner un
# match si les cotes "1X2" ne s'affichent pas. 40 cycles = 80s
# max par match — un compromis entre patience et durée totale
# du run (avec plusieurs sites x plusieurs matchs, 90 cycles
# comme dans le script d'origine ferait un run bien trop long
# en CI si plusieurs matchs échouent).
MAX_WAIT_CYCLES = 20  # réduit de 40 : 40s max d'attente par match au lieu de 80s


async def route_handler(route):
    if should_block(route):
        await route.abort()
    else:
        await route.continue_()


async def run_bookmakers(context):
    """Traite les bookmakers (hors 1win) un par un, dans l'ordre —
    chacun utilise jusqu'à MATCH_CONCURRENCY onglets en parallèle en
    interne (voir scrape_bookmaker). On ne lance PAS les bookmakers
    entre eux en parallèle : ça éviterait de cumuler encore plus de
    connexions simultanées via un proxy dont on ne connaît pas les
    limites exactes."""

    for bookmaker in BOOKMAKERS_LIST:

        if bookmaker == "1win":
            continue

        config = BOOKMAKERS[bookmaker]

        await scrape_bookmaker(
            context,
            bookmaker,
            config,
            MAX_MATCHES_PER_SITE,
            MAX_WAIT_CYCLES
        )


async def main():

    proxy = proxy_config()

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True,
            proxy=proxy
        )

        context = await browser.new_context(
            viewport={
                "width": 390,
                "height": 844
            },

            java_script_enabled=True,

            service_workers="block",

            locale="fr-FR",
        )

        await context.route("**/*", route_handler)

        # ------------------------------------------------------
        # 1WIN tourne dans son PROPRE navigateur, sans proxy (l'IP
        # du runner n'y est pas bloquée). Comme il n'utilise jamais
        # le proxy, le lancer EN MÊME TEMPS que les autres bookmakers
        # ne consomme aucune connexion proxy supplémentaire : c'est
        # du temps gagné "gratuitement" sur la durée totale du run.
        # ------------------------------------------------------
        await asyncio.gather(
            run_bookmakers(context),
            scrape_1win(playwright),
        )

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

















