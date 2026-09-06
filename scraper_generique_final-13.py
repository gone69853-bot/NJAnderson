from playwright.sync_api import sync_playwright
import json
import datetime
import re
import sys
import os

# ============================================================
# MOTIFS DE DÉTECTION DES LIENS DE MATCH
# ============================================================
# Format "classique" (BetWinner, MelBet, GoldPari, WinWin, 1xBet, LineBet) :
#   /line/football/ID-championnat/ID-equipe1-equipe2
MOTIF_MATCH_DEFAUT = re.compile(r"/line/football/\d+-[^/]+/\d+-[^/?]+")

# Format 1win :
#   /betting/match/sport/equipe1-vs-equipe2-ID
MOTIF_MATCH_1WIN = re.compile(r"/betting/match/sport/[^/?]+-\d+")

# ============================================================
# CONFIGURATION DES BOOKMAKERS
# ============================================================
SITES = {
    "betwinner": {
        "listing_url": "https://betwinner.cm/fr/line/football?platform_type=mobile",
        "base_url": "https://betwinner.cm",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,   # 3 minutes
    },
    "melbet": {
        "listing_url": "https://melbet-cm.com/en/line?platform_type=mobile",
        "base_url": "https://melbet-cm.com",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,
    },
    "goldpari": {
        "listing_url": "https://goldpari-49096.com/fr/line?platform_type=mobile",
        "base_url": "https://goldpari-49096.com",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,
    },
    "1win": {
        "listing_url": "https://1win.com/betting/prematch?platform_type=mobile",
        "base_url": "https://1win.com",
        "motif": MOTIF_MATCH_1WIN,
        "max_tentatives": 600,  # ~20 minutes — 1win peut être très lent à charger
    },
    "winwin": {
        "listing_url": "https://winwin-97317.pro/en/line?platform_type=mobile",
        "base_url": "https://winwin-97317.pro",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,
    },
    "1xbet": {
        "listing_url": "https://1xbet.cm/fr/line?platform_type=mobile",
        "base_url": "https://1xbet.cm",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,
    },
    "linebet": {
        "listing_url": "https://linebet.com/fr/line?platform_type=mobile",
        "base_url": "https://linebet.com",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,
    },
    "888starz": {
        "listing_url": "https://888starz.bet/fr/line/football?platform_type=mobile",
        "base_url": "https://888starz.bet",
        "motif": MOTIF_MATCH_DEFAUT,
        "max_tentatives": 90,
    },
}

MAX_MATCHS_PAR_SITE = 50     # valeur intermédiaire pour tester le temps réel avant d'aller plus haut
NB_ESSAIS_PAR_MATCH = 2


def get_proxy_config():
    """Lit la config proxy depuis les variables d'environnement (GitHub Secrets).
    Retourne None si aucune variable n'est définie -> le navigateur se lance
    alors sans proxy (utile pour continuer à tester en local sur le PC
    d'Anderson, où le blocage géo ne s'applique pas)."""
    server = os.environ.get("PROXY_SERVER")
    if not server:
        return None

    return {
        "server": server,
        "username": os.environ.get("PROXY_USERNAME"),
        "password": os.environ.get("PROXY_PASSWORD"),
    }


def extraire_equipes(href, nom_site):
    """Best-effort : extrait les noms d'équipes depuis le slug de l'URL."""
    try:
        if nom_site == "1win":
            # .../equipe1-vs-equipe2-ID
            m = re.search(r"/([^/]+)-vs-([^/]+)-\d+$", href)
            if m:
                e1 = m.group(1).replace("-", " ").title()
                e2 = m.group(2).replace("-", " ").title()
                return e1, e2
        else:
            # .../ID-equipe1-equipe2  (tiret double possible dans un nom d'équipe)
            m = re.search(r"/\d+-([^/?]+)$", href)
            if m:
                slug = m.group(1)
                parties = slug.split("-")
                if len(parties) >= 2:
                    milieu = len(parties) // 2
                    e1 = " ".join(parties[:milieu]).title()
                    e2 = " ".join(parties[milieu:]).title()
                    return e1, e2
    except Exception:
        pass
    return None, None


def decouvrir_matchs(page, site_conf, max_matchs, nom_site):
    """Va sur la page de listing et extrait les liens vers des pages de match.
    Scrolle progressivement (molette + touche Fin + JS scrollTo, combinés
    car les sites réagissent différemment) pour déclencher le chargement
    des matchs suivants (liste à défilement infini)."""
    page.goto(site_conf["listing_url"], timeout=180000, wait_until="domcontentloaded")
    page.wait_for_timeout(15000)  # laisser le premier lot de matchs se charger

    def compter_liens_match():
        hrefs_actuels = page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
        return hrefs_actuels, sum(1 for h in hrefs_actuels if h and site_conf["motif"].search(h))

    def deplier_accordeons():
        """Clique sur les flèches de championnats repliés pour révéler leurs
        matchs (BetWinner/888starz regroupent les matchs par championnat
        dans des accordéons fermés par défaut)."""
        try:
            nb_clics = page.evaluate("""
                () => {
                    const fleches = document.querySelectorAll('.ui-accordion-trigger__arrow:not([data-deja-clique])');
                    let compte = 0;
                    fleches.forEach(f => {
                        f.setAttribute('data-deja-clique', '1');
                        f.closest('[class*="accordion-trigger"]')?.click();
                        compte++;
                    });
                    return compte;
                }
            """)
            return nb_clics
        except Exception:
            return 0

    def scroller():
        # Combine plusieurs techniques, car les sites ne réagissent pas tous
        # au même déclencheur de scroll.
        try:
            page.mouse.wheel(0, 4000)
        except Exception:
            pass
        try:
            page.keyboard.press("End")
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        try:
            # Si un conteneur interne scrollable existe (courant dans les
            # apps type liste virtualisée), on le scrolle aussi directement.
            page.evaluate("""
                () => {
                    document.querySelectorAll('div').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 50) {
                            el.scrollTop = el.scrollHeight;
                        }
                    });
                }
            """)
        except Exception:
            pass

    # Premier passage : déplier tous les championnats visibles avant de scroller
    deplier_accordeons()
    page.wait_for_timeout(2000)

    hrefs, nb_trouves = compter_liens_match()
    tentatives_scroll = 0
    max_tentatives_scroll = 40  # plus de patience pour laisser le temps au chargement

    while nb_trouves < max_matchs and tentatives_scroll < max_tentatives_scroll:
        scroller()
        deplier_accordeons()  # de nouveaux championnats peuvent apparaître au scroll
        page.wait_for_timeout(2500)
        hrefs, nouveau_nb = compter_liens_match()
        if nouveau_nb <= nb_trouves:
            tentatives_scroll += 1
        else:
            tentatives_scroll = 0
        nb_trouves = nouveau_nb

    matchs = []
    vus = set()

    for href in hrefs:
        if not href:
            continue
        if site_conf["motif"].search(href):
            url_complete = href if href.startswith("http") else site_conf["base_url"] + href
            if url_complete not in vus:
                vus.add(url_complete)
                dernier_segment = href.rstrip("/").split("/")[-1]
                m = re.match(r"(\d+)-", dernier_segment)
                match_id = m.group(1) if m else dernier_segment
                e1, e2 = extraire_equipes(href, nom_site)
                matchs.append({"match_id": match_id, "url": url_complete, "equipe_1": e1, "equipe_2": e2})
        if len(matchs) >= max_matchs:
            break

    return matchs


def parser_cotes(texte):
    lignes = [l.strip() for l in texte.split("\n") if l.strip()]
    resultat = {}

    def trouver_bloc(nom_marche, nb_paires):
        try:
            i = lignes.index(nom_marche)
        except ValueError:
            return None
        bloc = {}
        pos = i + 1
        for _ in range(nb_paires):
            if pos + 1 < len(lignes):
                bloc[lignes[pos]] = lignes[pos + 1]
                pos += 2
        return bloc

    resultat["1X2"] = trouver_bloc("1X2", 3)

    try:
        i_total = lignes.index("Total")
        for j in range(i_total, min(i_total + 60, len(lignes) - 3)):
            if lignes[j] == "2.5 Plus de":
                resultat["Total_2.5"] = {
                    "Plus de": lignes[j + 1],
                    "Moins de": lignes[j + 3] if lignes[j + 2] == "2.5 Moins de" else None,
                }
                break
    except ValueError:
        pass

    return resultat


def scraper_un_match(page, match, max_tentatives):
    for essai in range(1, NB_ESSAIS_PAR_MATCH + 1):
        try:
            print(f"    Essai {essai}/{NB_ESSAIS_PAR_MATCH} — {match['url']}")
            page.goto(match["url"], timeout=120000, wait_until="domcontentloaded")

            texte = ""
            trouve = False
            for _ in range(max_tentatives):
                texte = page.inner_text("body")
                if "1X2" in texte and ("V1" in texte or "1" in texte):
                    trouve = True
                    break
                page.wait_for_timeout(2000)

            if not trouve:
                print(f"    Échec essai {essai} : cotes non détectées")
                continue

            data = parser_cotes(texte)
            if not data.get("1X2") or len(data["1X2"]) < 3:
                print(f"    Échec essai {essai} : bloc 1X2 incomplet")
                continue

            data["match_id"] = match["match_id"]
            data["url"] = match["url"]
            data["equipe_1"] = match.get("equipe_1")
            data["equipe_2"] = match.get("equipe_2")
            data["derniere_maj"] = datetime.datetime.now().isoformat(timespec="seconds")
            data["statut"] = "ok"
            return data

        except Exception as e:
            print(f"    Erreur essai {essai} : {e}")
            continue

    return {
        "match_id": match["match_id"],
        "url": match["url"],
        "equipe_1": match.get("equipe_1"),
        "equipe_2": match.get("equipe_2"),
        "derniere_maj": datetime.datetime.now().isoformat(timespec="seconds"),
        "statut": "echec",
    }


def scraper_site(page, nom_site, site_conf):
    print(f"\n=== {nom_site.upper()} ===")
    print("  Découverte des matchs...")

    matchs = []
    for essai in range(1, 3):  # 2 essais pour la découverte
        try:
            matchs = decouvrir_matchs(page, site_conf, MAX_MATCHS_PAR_SITE, nom_site)
            break
        except Exception as e:
            print(f"  Erreur découverte (essai {essai}/2) : {e}")
            page.wait_for_timeout(3000)

    if not matchs:
        print(f"  Aucun match trouvé pour {nom_site} (découverte impossible).")
        resultats = []
        with open(f"{nom_site}.json", "w", encoding="utf-8") as f:
            json.dump(resultats, f, ensure_ascii=False, indent=2)
        return resultats

    print(f"  {len(matchs)} match(s) trouvé(s).")

    resultats = []
    for match in matchs:
        resultats.append(scraper_un_match(page, match, site_conf["max_tentatives"]))

    nb_ok = sum(1 for r in resultats if r["statut"] == "ok")
    nb_echec = sum(1 for r in resultats if r["statut"] == "echec")
    print(f"  Bilan {nom_site} : {nb_ok} ok / {nb_echec} échecs (sur {len(resultats)})")

    with open(f"{nom_site}.json", "w", encoding="utf-8") as f:
        json.dump(resultats, f, ensure_ascii=False, indent=2)

    return resultats


def main():
    # ============================================================
    # Pour tester un seul bookmaker, décommente la ligne SITE_UNIQUE
    # et mets son nom. Pour tous les tester (par défaut), laisse None.
    # Noms possibles : betwinner, melbet, goldpari, 1win, winwin, 1xbet, linebet
    # ============================================================
    SITE_UNIQUE = None

    # 1win est exclu ici : sa structure est différente (pas de texte "1X2"/"V1"),
    # il est scrapé séparément par test_1win_v2.py.
    sites_a_tester = [SITE_UNIQUE] if SITE_UNIQUE else [s for s in SITES.keys() if s != "1win"]

    resultats_globaux = {}

    proxy_config = get_proxy_config()
    if proxy_config:
        print(f"Proxy activé : {proxy_config['server']} (user: {proxy_config['username']})")
    else:
        print("Aucun proxy configuré (variables PROXY_* absentes) — connexion directe.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy=proxy_config)
        page = browser.new_page()

        for nom_site in sites_a_tester:
            try:
                resultats_globaux[nom_site] = scraper_site(page, nom_site, SITES[nom_site])
            except Exception as e:
                print(f"\n!! Erreur imprévue sur {nom_site}, on passe au suivant : {e}")
                resultats_globaux[nom_site] = []

        browser.close()

    print("\n=== BILAN GLOBAL ===")
    for nom_site, resultats in resultats_globaux.items():
        nb_ok = sum(1 for r in resultats if r["statut"] == "ok")
        print(f"  {nom_site} : {nb_ok}/{len(resultats)}")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
