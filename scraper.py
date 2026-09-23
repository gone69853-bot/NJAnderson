import datetime
import json
import re

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

BLOCKED_TYPES = {
    "image",
    "media",
    "font",
    "manifest",
    "texttrack",
}

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


MATCH_PATTERN = re.compile(r"/line/football/\d+-[^/]+/\d+-[^/?]+")

COMPETITION_PATTERN = re.compile(
    r"/line/football/\d+-[^/?]+/?(?:\?.*)?$"
)


def with_mobile_param(url):

    if "platform_type=mobile" in url:
        return url

    separator = "&" if "?" in url else "?"

    return f"{url}{separator}platform_type=mobile"


def extract_teams_from_slug(href):

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


async def expand_accordions(page):

    try:

        return await page.evaluate(
            """
            () => {
                let compte = 0;

                document.querySelectorAll(
                    '[aria-expanded="false"]'
                ).forEach(el => {
                    el.click();
                    compte++;
                });

                document.querySelectorAll(
                    '.ui-accordion-trigger__arrow:not([data-deja-clique])'
                ).forEach(f => {
                    f.setAttribute('data-deja-clique', '1');
                    f.closest('[class*="accordion-trigger"]')?.click();
                    compte++;
                });

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

        url_ou_texte = (full_url + " " + text).lower()

        if "team-vs-player" in url_ou_texte or "team vs player" in url_ou_texte:
            continue

        count_match = re.search(
            r"(\d+)\s*$",
            text.strip()
        )

        count = int(count_match.group(1)) if count_match else 0

        links.append((full_url, count))

    links.sort(key=lambda pair: pair[1], reverse=True)

    return [url for url, _ in links]


async def click_maximize_buttons(page, bookmaker="", max_rounds=10):

    selector = (
        'button.ui-nav-link-toggle[aria-label="Maximize"]'
        '[aria-expanded="false"]'
    )

    total_clicked = 0

    for tour in range(max_rounds):
        clicked_this_round = 0

        try:
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

            await scroll_page(page)
            await page.wait_for_timeout(800)

            if clicked_this_round:
                print(
                    f"[{bookmaker}] Maximize : "
                    f"{clicked_this_round} bouton(s) ouvert(s) "
                    f"(tour {tour + 1})"
                )
            else:
                if tour >= 1:
                    break

        except Exception as error:
            print(f"[{bookmaker}] erreur Maximize : {error}")
            break

    return total_clicked


async def collect_hrefs_on_page(page, max_matches, max_stagnant=40):

    await expand_accordions(page)
    await click_maximize_buttons(page, urlparse(page.url).netloc)
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

    if len(matches) < max_matches:

        competition_links = await find_competition_links(page, base_url)

        for comp_url in competition_links[:max_competitions]:

            if len(matches) >= max_matches:
                break

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

            except Exception as error:

                print(
                    f"championnat ignoré ({comp_url}) : {error}"
                )
                continue

    return matches


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

        if result:
            break

        pos += 1

    return result


def parse_handicap_block(lines, max_span=40):

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
    # on lui laisse plus de tentatives.
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
            #
            # Garde-fou : sur certaines pages atypiques (paris
            # "vainqueur du championnat", outrights...), ce qui suit
            # "1X2" n'est pas un vrai nom d'équipe mais un fragment de
            # cote mal étiqueté (ex. "W12.39" vu en prod). On rejette
            # tout candidat contenant un motif décimal (chiffre(s) +
            # point + chiffre(s)), caractéristique d'une cote et
            # quasi absent des vrais noms d'équipe.
            MOTIF_COTE_DANS_NOM = re.compile(r"\d+\.\d+")

            def ressemble_a_une_equipe(candidat):
                return (
                    len(candidat) > 2
                    and candidat.lower() not in ("1", "x", "2", "draw", "nul")
                    and not MOTIF_COTE_DANS_NOM.search(candidat)
                )

            equipe_1 = match.get("equipe_1")
            equipe_2 = match.get("equipe_2")

            if len(noms_bloc) == 3:

                candidat_1, candidat_2 = noms_bloc[0], noms_bloc[2]

                if ressemble_a_une_equipe(candidat_1):
                    equipe_1 = candidat_1

                if ressemble_a_une_equipe(candidat_2):
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


WIN1_LISTING_URL = "https://1win.com/fr-CI/betting/prematch/football-18?p=mvh5"
WIN1_MAX_TENTATIVES = 300

WIN1_MOTIF_COTE = re.compile(r"\d.\d")


async def extraire_cartes_1win(page):

    return await page.evaluate(
        """
        () => {
            const cartes = document.querySelectorAll('[data-qa="match-card"]');
            const resultat = [];
            cartes.forEach(carte => {
                const teamsEl = carte.querySelector('[data-scope="TeamNames"]');
                const oddsEl = carte.querySelector('[data-qa="matchCardBaseOdds"]');
                resultat.push({
                    teamsText: teamsEl ? teamsEl.innerText : "",
                    oddsText: oddsEl ? oddsEl.innerText : ""
                });
            });
            return resultat;
        }
        """
    )


def parser_carte_1win(carte):

    lignes_equipes = [
        l.strip() for l in carte["teamsText"].split("\n") if l.strip()
    ]

    if len(lignes_equipes) < 2:
        return None

    equipe_1, equipe_2 = lignes_equipes[0], lignes_equipes[1]

    lignes_cotes = [
        l.strip() for l in carte["oddsText"].split("\n") if l.strip()
    ]

    resultat_1x2 = {}

    try:

        i = next(
            idx for idx, l in enumerate(lignes_cotes)
            if "résultat du temps réglementaire" in l.lower()
        )

        pos = i + 1
        cles_ordre = ["V1", "X", "V2"]

        for cle in cles_ordre:

            if pos + 1 >= len(lignes_cotes):
                break

            valeur = lignes_cotes[pos + 1].strip()

            resultat_1x2[cle] = valeur
            pos += 2

    except StopIteration:
        pass

    if not resultat_1x2:
        return None

    return {

        "bookmaker": "1win",

        "equipe_1": equipe_1,

        "equipe_2": equipe_2,

        "1X2": resultat_1x2,

        "Total_2.5": {
            "Plus de": None,
            "Moins de": None,
        },

        "url": WIN1_LISTING_URL,

        "derniere_maj":
            datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),

        "statut": "ok",
    }


async def ouvrir_plus_de_matchs_1win(page, max_tours=20):

    precedent = -1
    sans_nouveau = 0

    for tour in range(max_tours):
        try:
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

    return precedent


async def scrape_1win(playwright):

    result = []

    try:

        browser = await playwright.chromium.launch(headless=True)

        page = await browser.new_page()

        await page.goto(
            WIN1_LISTING_URL,
            timeout=1200000,
            wait_until="domcontentloaded"
        )

        await page.wait_for_timeout(5000)

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
                    f"[1win] {len(cartes)} carte(s) détectée(s), "
                    f"{nb_avec_cotes} avec cotes, "
                    f"après {tentative * 2}s"
                )

                break

            await page.wait_for_timeout(2000)

        await browser.close()

        for carte in cartes:

            parsed = parser_carte_1win(carte)

            if parsed:
                result.append(parsed)

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


MAX_WAIT_CYCLES = 20


async def route_handler(route):
    if should_block(route):
        await route.abort()
    else:
        await route.continue_()


async def run_bookmakers(context):

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

        await asyncio.gather(
            run_bookmakers(context),
            scrape_1win(playwright),
        )

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())





























