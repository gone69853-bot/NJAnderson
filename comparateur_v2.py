import json
import re
from difflib import SequenceMatcher

# Sites qui partagent la même plateforme technique (même catalogue de match_id)
SITES_PLATEFORME_COMMUNE = ["betwinner", "melbet", "goldpari", "winwin", "1xbet", "linebet", "888starz"]
SITE_A_PART = "1win"  # nécessite un rapprochement par nom d'équipe

TOUS_LES_SITES = SITES_PLATEFORME_COMMUNE + [SITE_A_PART]

SEUIL_SIMILARITE = 0.80


def normaliser(nom):
    if not nom:
        return ""
    nom = nom.lower().strip()
    remplacements = {
        "é": "e", "è": "e", "ê": "e",
        "à": "a", "â": "a",
        "î": "i", "ï": "i",
        "ô": "o", "ù": "u",
        "ç": "c",
    }
    for a, b in remplacements.items():
        nom = nom.replace(a, b)
    nom = re.sub(r"\([^)]*\)", "", nom)
    nom = re.sub(r"[^a-z0-9]+", " ", nom)
    return " ".join(nom.split())


def charger(fichier):
    try:
        with open(fichier, "r", encoding="utf-8") as f:
            data = json.load(f)
            # On ne garde que les matchs correctement récupérés
            return [m for m in data if m.get("statut") == "ok"]
    except FileNotFoundError:
        print(f"  (fichier {fichier} introuvable, ignoré)")
        return []


def similarite_equipes(m1, m2):
    e1a, e2a = normaliser(m1.get("equipe_1")), normaliser(m1.get("equipe_2"))
    e1b, e2b = normaliser(m2.get("equipe_1")), normaliser(m2.get("equipe_2"))
    if not e1a or not e1b:
        return 0
    score_direct = (SequenceMatcher(None, e1a, e1b).ratio() + SequenceMatcher(None, e2a, e2b).ratio()) / 2
    score_inverse = (SequenceMatcher(None, e1a, e2b).ratio() + SequenceMatcher(None, e2a, e1b).ratio()) / 2
    return max(score_direct, score_inverse)


def construire_groupes(donnees_par_site):
    """Regroupe les matchs équivalents entre sites en une liste de groupes.
    Chaque groupe = { site: match } pour un même événement réel."""
    groupes = []

    # 1) Grouper par match_id pour les sites à plateforme commune
    par_match_id = {}
    for site in SITES_PLATEFORME_COMMUNE:
        for match in donnees_par_site.get(site, []):
            mid = match["match_id"]
            par_match_id.setdefault(mid, {})[site] = match

    groupes = list(par_match_id.values())

    # 2) Rattacher 1win à un groupe existant par similarité de noms d'équipes,
    #    sinon créer un groupe à part pour lui seul.
    for match_1win in donnees_par_site.get(SITE_A_PART, []):
        meilleur_groupe = None
        meilleur_score = 0
        for groupe in groupes:
            # Comparer aux équipes de n'importe quel site déjà dans le groupe
            for autre in groupe.values():
                score = similarite_equipes(match_1win, autre)
                if score > meilleur_score:
                    meilleur_score = score
                    meilleur_groupe = groupe
        if meilleur_groupe is not None and meilleur_score >= SEUIL_SIMILARITE:
            meilleur_groupe[SITE_A_PART] = match_1win
        else:
            groupes.append({SITE_A_PART: match_1win})

    return groupes


def comparer_marche(groupe, marche_cle, sous_cles):
    """Compare un marché (ex: '1X2') à travers tous les sites du groupe.
    sous_cles = liste des libellés à comparer (ex: ['V1','X','V2'])."""
    lignes = []
    for sous_cle in sous_cles:
        valeurs = {}
        for site, match in groupe.items():
            bloc = match.get(marche_cle) or {}
            valeur = bloc.get(sous_cle)
            if valeur:
                try:
                    valeurs[site] = float(valeur)
                except (ValueError, TypeError):
                    pass
        if not valeurs:
            continue
        meilleur_site = max(valeurs, key=valeurs.get)
        lignes.append({
            "label": sous_cle,
            "valeurs": valeurs,
            "meilleur_site": meilleur_site,
            "meilleure_valeur": valeurs[meilleur_site],
        })
    return lignes


def comparer_tout(donnees_par_site):
    groupes = construire_groupes(donnees_par_site)
    resultats = []

    for groupe in groupes:
        # Nom des équipes : on prend le premier site disponible qui les a
        equipe_1 = equipe_2 = None
        for match in groupe.values():
            if match.get("equipe_1"):
                equipe_1, equipe_2 = match["equipe_1"], match["equipe_2"]
                break

        marches = []
        marches += comparer_marche(groupe, "1X2", ["V1", "X", "V2"])
        marches += comparer_marche(groupe, "Total_2.5", ["Plus de", "Moins de"])

        if not marches:
            continue

        resultats.append({
            "equipe_1": equipe_1 or "?",
            "equipe_2": equipe_2 or "?",
            "sites_presents": list(groupe.keys()),
            "marches": marches,
        })

    return resultats


def creer_html(resultats):
    html = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Zicote - Comparateur de cotes</title>
<style>
body { font-family: Arial, sans-serif; margin: 30px; background: #f5f5f5; }
.match { background: white; padding: 20px; margin-bottom: 25px; border-radius: 10px; }
h2 { margin-top: 0; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 10px; border-bottom: 1px solid #ddd; text-align: center; }
th { background: #222; color: white; }
.meilleur { font-weight: bold; background: #d9ffd9; }
.absent { color: #bbb; }
.sites { color: #888; font-size: 13px; margin-bottom: 10px; }
</style>
</head>
<body>
<h1>Comparateur Zicote</h1>
"""

    for match in resultats:
        html += f"""
<div class="match">
<h2>{match["equipe_1"]} — {match["equipe_2"]}</h2>
<div class="sites">Sites disponibles : {", ".join(match["sites_presents"])}</div>
<table>
<tr><th>Marché</th>"""
        for site in TOUS_LES_SITES:
            html += f"<th>{site}</th>"
        html += "<th>Meilleure cote</th></tr>"

        for ligne in match["marches"]:
            html += f"<tr><td>{ligne['label']}</td>"
            for site in TOUS_LES_SITES:
                val = ligne["valeurs"].get(site)
                classe = "meilleur" if site == ligne["meilleur_site"] else ("absent" if val is None else "")
                html += f'<td class="{classe}">{val if val is not None else "-"}</td>'
            html += f"<td><b>{ligne['meilleur_site']}</b> ({ligne['meilleure_valeur']})</td></tr>"

        html += "</table></div>"

    html += "</body></html>"

    with open("tableau.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("Tableau créé : tableau.html")


if __name__ == "__main__":
    donnees_par_site = {}
    for site in TOUS_LES_SITES:
        donnees_par_site[site] = charger(f"{site}.json")
        print(f"  {site} : {len(donnees_par_site[site])} match(s) valide(s) chargé(s)")

    resultats = comparer_tout(donnees_par_site)
    creer_html(resultats)

    print(f"{len(resultats)} matchs comparés au total")
