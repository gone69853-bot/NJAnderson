from playwright.sync_api import sync_playwright
import json
import datetime

LISTING_URL = "https://1win.com/betting/prematch?platform_type=mobile"
MAX_TENTATIVES = 300  # 10 minutes max pour que les cartes de match apparaissent


import re

def extraire_cartes(page):
    """Extrait teamsText et oddsText de chaque carte de match visible."""
    return page.evaluate("""
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
    """)


MOTIF_COTE = re.compile(r"\d\.\d")  # détecte une vraie cote type "3.36"


def parser_carte(carte):
    lignes_equipes = [l.strip() for l in carte["teamsText"].split("\n") if l.strip()]
    if len(lignes_equipes) < 2:
        return None
    equipe_1, equipe_2 = lignes_equipes[0], lignes_equipes[1]

    lignes_cotes = [l.strip() for l in carte["oddsText"].split("\n") if l.strip()]

    resultat_1x2 = {}
    try:
        i = next(idx for idx, l in enumerate(lignes_cotes) if "full time result" in l.lower())
        # Après le titre : label, valeur, label, valeur, label, valeur
        correspondance = {"1": "V1", "x": "X", "2": "V2"}
        pos = i + 1
        while pos + 1 < len(lignes_cotes):
            label = lignes_cotes[pos].strip().lower()
            valeur = lignes_cotes[pos + 1].strip()
            if label in correspondance:
                resultat_1x2[correspondance[label]] = valeur
                pos += 2
            else:
                break
    except StopIteration:
        pass

    if not resultat_1x2:
        return None

    return {
        "equipe_1": equipe_1,
        "equipe_2": equipe_2,
        "1X2": resultat_1x2,
        "derniere_maj": datetime.datetime.now().isoformat(timespec="seconds"),
        "statut": "ok",
    }


def cle_carte(carte):
    """Clé unique pour dédupliquer une carte (équipes suffisent, les cotes
    peuvent légèrement bouger entre deux extractions du même match)."""
    return carte["teamsText"].strip()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        print("[1win] Chargement de la page de listing...")
        page.goto(LISTING_URL, timeout=1200000, wait_until="domcontentloaded")

        cartes_vues = {}  # clé équipes -> dernière carte extraite
        hauteur_precedente = -1
        stagnation = 0  # nb de scrolls consécutifs sans nouveau contenu

        for tentative in range(MAX_TENTATIVES):
            cartes = extraire_cartes(page)
            for carte in cartes:
                cle = cle_carte(carte)
                if cle:
                    cartes_vues[cle] = carte

            nb_avec_cotes = sum(
                1 for c in cartes_vues.values() if MOTIF_COTE.search(c["oddsText"])
            )

            if tentative % 10 == 0:
                print(
                    f"[1win] ... {len(cartes_vues)} match(s) uniques repérés "
                    f"({nb_avec_cotes} avec cotes) après {tentative*2}s"
                )

            # Scroll pour déclencher le chargement des matchs suivants
            # (liste virtualisée : sans scroll, seuls les premiers
            # matchs visibles sont présents dans le DOM).
            hauteur_actuelle = page.evaluate("document.body.scrollHeight")
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(2000)

            if hauteur_actuelle == hauteur_precedente:
                stagnation += 1
            else:
                stagnation = 0
            hauteur_precedente = hauteur_actuelle

            # Arrêt si plus rien de nouveau ne se charge après plusieurs
            # scrolls consécutifs (on a atteint le bas de la liste).
            if stagnation >= 8:
                print(
                    f"[1win] Fin de liste atteinte : {len(cartes_vues)} match(s) "
                    f"uniques au total, après {tentative*2}s"
                )
                break
        else:
            print(
                f"[1win] Temps maximum atteint : {len(cartes_vues)} match(s) "
                f"uniques collectés."
            )

        browser.close()

    resultats = []
    for i, (cle, carte) in enumerate(cartes_vues.items()):
        if i < 3:  # affiche le détail des 3 premières cartes pour diagnostic
            print(f"\n[1win] --- Carte {i} : teamsText ---")
            print(repr(carte["teamsText"]))
            print(f"[1win] --- Carte {i} : oddsText ---")
            print(repr(carte["oddsText"]))
        parsed = parser_carte(carte)
        if parsed:
            resultats.append(parsed)

    with open("1win.json", "w", encoding="utf-8") as f:
        json.dump(resultats, f, ensure_ascii=False, indent=2)

    print(f"\n[1win] {len(resultats)} match(s) avec cotes valides sauvegardés dans 1win.json")


if __name__ == "__main__":
    main()
