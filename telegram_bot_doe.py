#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Bot Telegram « DOE » — interface Telegram de DOE (plans d'expériences).
#  Importe doe.py (même cœur que l'application de bureau ; calculs réalisés par R).
#  - Plans : criblage, factoriel (complet / fractionnaire), Taguchi, surface de réponse, mélange
#  - Phases : définition des facteurs et génération de la matrice → saisie des réponses →
#    analyse (ANOVA, effets, graphiques) → graphiques du modèle → prévision → optimisation
#  - Menus à boutons, résultats en texte à chasse fixe + graphiques en images
#  - Les plans d'une conversation restent en mémoire du bot (rien n'est écrit sur disque)
#
#  Configuration (aucun secret dans ce fichier) :
#    Token   : variable TELEGRAM_TOKEN, sinon ~/.telegram_config  [telegram] token_doe
#    Accès   : ALLOWED_IDS (variable ou .env, séparés par des virgules) ou
#              ~/.telegram_config  [telegram] allowed_ids ; le chat_id du fichier est toujours
#              autorisé. Si aucun identifiant n'est connu, le bot est ouvert à tous (déconseillé).
#    Un utilisateur refusé reçoit son identifiant Telegram, à ajouter à ALLOWED_IDS.
#  Dépendance : requests (+ R et r-cran-jsonlite pour les calculs)
#  Lancement  : python3 telegram_bot_doe.py
#  Service    : voir la fiche « Bot Telegram » (systemd, ...)
#  Auteur : Jean-François BRUNET – JFBConseils – Novembre 2026
#  Rev.   :
# =============================================================================

import atexit
import configparser
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    import doe
except ImportError:
    sys.path.insert(0, str(Path.home() / "Projects" / "DOE"))
    import doe

COMMANDES_MENU = [
    ("criblage", "Plan de criblage (Plackett-Burman, fractions)"),
    ("factoriel", "Plan factoriel complet ou fractionnaire"),
    ("taguchi", "Plan de Taguchi (tableaux orthogonaux)"),
    ("surface", "Plan de surface de réponse"),
    ("melange", "Plan de mélange"),
    ("famille", "Changer de plan actif"),
    ("plan", "Afficher le plan actif"),
    ("reponses", "Saisir les réponses mesurées"),
    ("analyse", "Analyser les résultats"),
    ("graphiques", "Graphiques du modèle (surface, mélange)"),
    ("prevoir", "Prévision avec intervalles"),
    ("optimiser", "Optimiser les réponses"),
    ("alpha", "Risque α"),
    ("export", "Récupérer le plan en CSV"),
    ("etat", "État du travail en cours"),
    ("effacer", "Effacer le plan actif"),
    ("annuler", "Annuler la saisie en attente"),
    ("aide", "Aide"),
]

LARGEUR_MOBILE = 42          # au-delà, les tableaux sont présentés en fiches
LIMITE_MESSAGE = 3600
MAX_FICHIER = 5 * 1024 * 1024
FAMILLES = doe.FAMILLES
NOMS_FAMILLES = {c: v["titre"][3:] for c, v in FAMILLES.items()}

BTN_MENU = [[("🔎 Criblage", "/criblage"), ("🧊 Factoriel", "/factoriel")],
            [("🧩 Taguchi", "/taguchi"), ("⛰ Surface", "/surface")],
            [("🧪 Mélange", "/melange"), ("📋 Plan actif", "/plan")],
            [("✍️ Réponses", "/reponses"), ("📊 Analyse", "/analyse")],
            [("🗺 Graphiques", "/graphiques"), ("🎯 Prévision", "/prevoir")],
            [("🏁 Optimiser", "/optimiser"), ("📤 Export CSV", "/export")]]

# Syntaxe de définition d'un plan, affichée par chaque commande de création
AIDE_PLAN = {
    "criblage": (
        "🔎 Plan de criblage\n\n"
        "Envoyez un facteur par ligne :\n"
        "  Nom : niveau_bas niveau_haut\n"
        "(deux niveaux qualitatifs : mots à la place des nombres)\n\n"
        "Options (ligne ou fin de message) :\n"
        "• methode=pb | fraction   (défaut : pb = Plackett-Burman)\n"
        "• essais=12   (pb : 8, 12, 16, 20, 24 ou 32 ; défaut automatique)\n"
        "• fraction=1   (numéro proposé pour ce nombre de facteurs) ou gen=D=AB,E=AC\n"
        "• rep=1  centres=0  alea=oui|non  graine=42"),
    "factoriel": (
        "🧊 Plan factoriel\n\n"
        "Envoyez un facteur par ligne :\n"
        "  Nom : niveau_bas niveau_haut\n"
        "(deux niveaux qualitatifs : mots à la place des nombres)\n\n"
        "Options (ligne ou fin de message) :\n"
        "• type=complet | fractionnaire   (défaut : complet)\n"
        "• fraction=1   (numéro proposé pour ce nombre de facteurs) ou gen=D=ABC\n"
        "• rep=1  centres=0  alea=oui|non  graine=42"),
    "taguchi": (
        "🧩 Plan de Taguchi\n\n"
        "Envoyez un facteur par ligne, avec ses niveaux séparés par des points-virgules (2, 3 ou 5 niveaux) :\n"
        "  Nom : niveau1 ; niveau2 ; niveau3\n\n"
        "Options (ligne ou fin de message) :\n"
        "• tableau=L9   (défaut : automatique ; L4 L8 L9 L12 L16 L18 L25 L27 L32)\n"
        "• interactions=A*B,C*D   (par lettres des facteurs)\n"
        "• reponses=3   (colonnes de réponses : répétitions ou conditions de bruit ; défaut 1)\n"
        "• alea=oui|non  graine=42"),
    "surface": (
        "⛰ Plan de surface de réponse\n\n"
        "Envoyez un facteur quantitatif par ligne :\n"
        "  Nom : niveau_bas niveau_haut\n\n"
        "Options (ligne ou fin de message) :\n"
        "• type=ccc | ccf | cci | bb   (défaut : ccc ; bb = Box-Behnken, 3 à 5 facteurs)\n"
        "• alpha=rotatable | orthogonal | valeur   (ccc seulement)\n"
        "• centres=…  rep=1  alea=oui|non  graine=42"),
    "melange": (
        "🧪 Plan de mélange\n\n"
        "Envoyez un composant par ligne, avec ses bornes de proportion (0 à 1) :\n"
        "  Nom : borne_basse borne_haute        (sans bornes : 0 et 1)\n\n"
        "Options (ligne ou fin de message) :\n"
        "• type=reseau | centroide | sommets   (défaut : reseau ; sommets si des bornes hautes contraignent)\n"
        "• degre=2   (réseau)  aretes=oui  faces=oui  (sommets)  axiaux=oui\n"
        "• centres=1  rep=1  alea=oui|non  graine=42"),
}

MODELES_COURTS = {c: [code for _, code in lst] for c, lst in doe.MODELES.items()}
BUTS = {"max": "max", "maximiser": "max", "min": "min", "minimiser": "min", "cible": "cible"}


# =============================================================================
#  Utilitaires Telegram (même structure que les autres bots)
# =============================================================================
def lire_config_telegram():
    cp = configparser.RawConfigParser()
    try:
        cp.read(Path.home() / ".telegram_config", encoding="utf-8")
    except Exception:
        pass
    return cp["telegram"] if cp.has_section("telegram") else {}


def attendre_ntp(timeout=60):
    """Attend (60 s max) que l'horloge du Pi soit synchronisée NTP, comme les autres bots."""
    for _ in range(timeout):
        try:
            r = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                               capture_output=True, text=True, timeout=3)
            if r.stdout.strip() == "yes":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def clavier(boutons):
    """Boutons [[(libellé, commande)]] → reply_markup Telegram (callback_data ≤ 64 octets)."""
    lignes = []
    for ligne in boutons:
        rang = [{"text": lib, "callback_data": cmd} for lib, cmd in ligne if len(cmd.encode("utf-8")) <= 64]
        if rang:
            lignes.append(rang)
    return json.dumps({"inline_keyboard": lignes})


def esc(texte):
    """Échappement HTML minimal exigé par Telegram (&, <, >)."""
    return html.escape(texte, quote=False)


def sans_html(texte):
    return html.unescape(re.sub(r"</?(b|i|pre|code)>", "", texte))


# =============================================================================
#  Construction des messages (indépendante de Telegram : dictionnaires)
#    {"texte": str, "html": bool, "boutons": [[(lib, cmd)]], "remplace": bool}
#    {"photo": chemin}   {"document": chemin}
# =============================================================================
def msg(texte, boutons=None, remplace=False, html_=False):
    return {"texte": texte, "boutons": boutons, "remplace": remplace, "html": html_}


def blocs_pre(texte):
    """Texte à chasse fixe → blocs <pre> de taille compatible avec Telegram."""
    sortie, cur = [], ""
    for ligne in texte.split("\n"):
        if cur and len(cur) + len(ligne) + 1 > LIMITE_MESSAGE - 20:
            sortie.append(f"<pre>{esc(cur)}</pre>")
            cur = ""
        cur = (cur + "\n" + ligne) if cur else ligne
    if cur:
        sortie.append(f"<pre>{esc(cur)}</pre>")
    return sortie


def regrouper(blocs):
    messages, cur = [], ""
    for b in blocs:
        if cur and len(cur) + 2 + len(b) > LIMITE_MESSAGE:
            messages.append(cur)
            cur = b
        else:
            cur = (cur + "\n\n" + b) if cur else b
    if cur:
        messages.append(cur)
    return messages


ABREGES = {"Erreur_type": "ET", "Somme_carrés": "SC", "Carré_moyen": "CM", "p_value": "p", "Coefficient": "Coef", "Contribution_pct": "Contr.%",
           "Contribution_pure_pct": "Contr.pure%", "Valeur_codée": "Codé", "Valeur_réelle": "Réel", "IC_inf": "IC inf", "IC_sup": "IC sup",
           "Prévision": "Prév.", "Désirabilité": "Désir.", "Écart_type": "Écart-t.", "Ecart_type": "Écart-t."}


def table_fiches(cols, rows, libre=False):
    """Tableau trop large pour un mobile : une fiche compacte par ligne (1re colonne en titre)."""
    ent = [ABREGES.get(c, str(c).replace("_", " ") if libre else doe.libelle(c)) for c in cols]
    fiches = []
    for r in rows:
        cel = [doe.fmt_cellule(c, v, libre) for c, v in zip(cols, r)]
        lignes, cur = [], ""
        for e, c in zip(ent[1:], cel[1:]):
            morceau = f"{e} {c}"
            if cur and len(cur) + 3 + len(morceau) > 38:
                lignes.append(cur)
                cur = morceau
            else:
                cur = (cur + " · " + morceau) if cur else morceau
        if cur:
            lignes.append(cur)
        fiches.append(f"{ent[0]} : {cel[0].strip()}\n" + "\n".join("  " + l for l in lignes))
    return "\n".join(fiches)


def items_resultat(res, menu=BTN_MENU):
    blocs = []
    if res.titre:
        blocs.append(f"📊 <b>{esc(res.titre)}</b>")
    if res.lignes:
        blocs.append(esc("\n".join(res.lignes)))
    for nom, (cols, rows) in res.tables.items():
        libre = nom in res.libres
        large = doe.table_texte(cols, rows, libre=libre)
        if max(len(l) for l in large.split("\n")) <= LARGEUR_MOBILE:
            corps = large
        else:
            corps = table_fiches(cols, rows, libre)
        blocs.append(f"<b>{esc(doe.libelle(nom))}</b>")
        blocs.extend(blocs_pre(corps))
    if res.avertissements:
        blocs.append("<i>" + esc("\n".join("Remarque : " + a for a in res.avertissements)) + "</i>")
    items = [msg(t, html_=True) for t in regrouper(blocs)]
    items += [{"photo": f} for f in res.figures]
    items.append(msg("Suite ?", menu))
    return items


def lettre(i):
    return doe.lettre(i) if hasattr(doe, "lettre") else chr(65 + i)


# =============================================================================
#  Lecture des définitions de plan
# =============================================================================
def booleen(v, nom):
    t = v.strip().lower()
    if t in ("oui", "o", "1", "vrai", "true", "yes"):
        return True
    if t in ("non", "n", "0", "faux", "false", "no"):
        return False
    raise doe.ErreurDOE(f"Option « {nom} » : répondre oui ou non.")


def entier(v, nom, minimum=0):
    x = doe.lire_nombre(v, nom)
    if x is None or x != int(x) or x < minimum:
        raise doe.ErreurDOE(f"Option « {nom} » : saisir un entier supérieur ou égal à {minimum}.")
    return int(x)


def decouper_definition(texte):
    """Texte -> (lignes de facteurs, options clé->valeur). Une ligne « mot=valeur » sans « : » est une ligne d'options."""
    facteurs, options = [], {}
    for brut in texte.replace("\r", "").split("\n"):
        ligne = brut.strip()
        if not ligne:
            continue
        if ":" not in ligne and "=" in ligne:
            for jeton in ligne.replace(";", " ").split():
                if "=" not in jeton:
                    raise doe.ErreurDOE(f"Option non reconnue : « {jeton} » (forme attendue : option=valeur).")
                k, v = jeton.split("=", 1)
                options[k.strip().lower()] = v.strip()
        else:
            facteurs.append(ligne)
    return facteurs, options


def lire_facteurs(famille, lignes):
    res, vus = [], set()
    for ligne in lignes:
        if ":" in ligne:
            nom, reste = ligne.split(":", 1)
        else:
            jetons = ligne.split()
            nom, reste = jetons[0], " ".join(jetons[1:])
        nom = nom.strip()
        reste = reste.strip()
        if not nom:
            raise doe.ErreurDOE(f"Nom de facteur manquant dans « {ligne} ».")
        if nom in vus:
            raise doe.ErreurDOE(f"Le nom « {nom} » est utilisé plusieurs fois.")
        if nom.lower() in doe.RESERVES or any(ch in nom for ch in '",;\n\t'):
            raise doe.ErreurDOE(f"Le nom « {nom} » n'est pas permis (nom réservé, virgule, point-virgule ou guillemet).")
        vus.add(nom)
        if famille == "taguchi":
            niv = [x.strip() for x in (reste.split(";") if ";" in reste else reste.split()) if x.strip()]
            if len(niv) < 2:
                raise doe.ErreurDOE(f"« {nom} » : saisir au moins deux niveaux, séparés par des points-virgules.")
            if len(niv) == 4 or len(niv) > 5:
                raise doe.ErreurDOE(f"« {nom} » : 2, 3 ou 5 niveaux sont pris en charge.")
            if len(set(niv)) != len(niv):
                raise doe.ErreurDOE(f"« {nom} » : les niveaux doivent être différents.")
            res.append({"nom": nom, "niveaux": niv})
            continue
        jetons = reste.replace(";", " ").split()
        if famille == "melange":
            if not jetons:
                jetons = ["0", "1"]
            if len(jetons) != 2:
                raise doe.ErreurDOE(f"« {nom} » : saisir la borne basse et la borne haute.")
            lo, hi = doe.lire_nombre(jetons[0], f"la borne basse de « {nom} »"), doe.lire_nombre(jetons[1], f"la borne haute de « {nom} »")
            if lo < 0 or hi > 1 or lo > hi:
                raise doe.ErreurDOE(f"« {nom} » : les bornes doivent vérifier 0 ≤ borne basse ≤ borne haute ≤ 1.")
            res.append({"nom": nom, "bas": lo, "haut": hi})
            continue
        if len(jetons) != 2:
            raise doe.ErreurDOE(f"« {nom} » : saisir le niveau bas et le niveau haut (forme : Nom : bas haut).")
        a, b = jetons
        if doe.est_nombre(a) and doe.est_nombre(b):
            lo, hi = doe.lire_nombre(a), doe.lire_nombre(b)
            if lo >= hi:
                raise doe.ErreurDOE(f"« {nom} » : le niveau bas doit être inférieur au niveau haut.")
            res.append({"nom": nom, "type": "num", "bas": lo, "haut": hi})
        else:
            if famille == "surface":
                raise doe.ErreurDOE(f"« {nom} » : un plan de surface exige des facteurs quantitatifs (niveaux numériques).")
            if a == b:
                raise doe.ErreurDOE(f"« {nom} » : les deux niveaux doivent être différents.")
            res.append({"nom": nom, "type": "cat", "niveaux": [a, b]})
    return res


def generateurs(opt, k, res_max):
    """Générateurs d'après gen=… ou fraction=n (liste proposée pour k facteurs)."""
    if "gen" in opt:
        g = [x.strip() for x in re.split(r"[,;]+", opt["gen"]) if x.strip()]
        if not g:
            raise doe.ErreurDOE("Option « gen » vide.")
        return g
    liste = doe.options_fractions(k, res_max=res_max)
    if not liste:
        raise doe.ErreurDOE(f"Aucune fraction proposée pour {k} facteurs : préciser les générateurs (gen=…).")
    n = entier(opt.get("fraction", "1"), "fraction", 1)
    if n > len(liste):
        raise doe.ErreurDOE(f"Fraction {n} inexistante : {len(liste)} proposition(s) pour {k} facteurs.")
    return liste[n - 1][1]


def parametres_plan(famille, facteurs, opt):
    """(commande R, paramètres) d'après les options saisies."""
    connues = {"criblage": {"methode", "essais", "fraction", "gen", "rep", "centres", "alea", "graine"},
               "factoriel": {"type", "fraction", "gen", "rep", "centres", "alea", "graine"},
               "taguchi": {"tableau", "interactions", "reponses", "alea", "graine"},
               "surface": {"type", "alpha", "centres", "rep", "alea", "graine"},
               "melange": {"type", "degre", "aretes", "faces", "axiaux", "centres", "rep", "alea", "graine"}}[famille]
    inconnues = sorted(set(opt) - connues)
    if inconnues:
        raise doe.ErreurDOE(f"Option(s) non reconnue(s) : {', '.join(inconnues)}.")
    base = {"aleatoire": booleen(opt["alea"], "alea") if "alea" in opt else True}
    graine = doe.lire_nombre(opt.get("graine"), "graine")
    base["graine"] = int(graine) if graine is not None else None
    rep = entier(opt.get("rep", "1"), "rep", 1)
    if famille in ("criblage", "factoriel"):
        base.update({"facteurs": facteurs, "repetitions": rep, "points_centraux": entier(opt.get("centres", "0"), "centres")})
        k = len(facteurs)
        if famille == "criblage":
            methode = opt.get("methode", "pb").lower()
            if methode in ("pb", "plackett", "plackett-burman"):
                ne = opt.get("essais", "auto")
                base.update({"methode": "pb", "nb_essais": 0 if ne.lower() in ("auto", "automatique") else entier(ne, "essais", 4)})
            elif methode in ("fraction", "fractionnaire"):
                base.update({"methode": "fraction", "generateurs": generateurs(opt, k, 4)})
            else:
                raise doe.ErreurDOE("methode : pb ou fraction.")
            return "plan_criblage", base
        t = opt.get("type", "fractionnaire" if ("gen" in opt or "fraction" in opt) else "complet").lower()
        if t in ("complet", "c"):
            base["type"] = "complet"
        elif t in ("fractionnaire", "fraction", "f"):
            base.update({"type": "fractionnaire", "generateurs": generateurs(opt, k, 99)})
        else:
            raise doe.ErreurDOE("type : complet ou fractionnaire.")
        return "plan_factoriel", base
    if famille == "taguchi":
        n = len(facteurs)
        inter = []
        for t in re.split(r"[,;\s]+", opt.get("interactions", "")):
            if not t:
                continue
            m = re.fullmatch(r"([A-Za-z])\*([A-Za-z])", t)
            if not m:
                raise doe.ErreurDOE(f"Interaction « {t} » : forme attendue A*B (lettres des facteurs).")
            a, b = sorted((ord(m.group(1).upper()) - 65, ord(m.group(2).upper()) - 65))
            if a == b or b >= n:
                raise doe.ErreurDOE(f"Interaction « {t} » : facteurs invalides (lettres de A à {lettre(n - 1)}).")
            if [a + 1, b + 1] not in inter:
                inter.append([a + 1, b + 1])
        tab = opt.get("tableau", "auto")
        base.update({"facteurs": facteurs, "interactions": inter, "tableau": "auto" if tab.lower() in ("auto", "automatique") else tab.upper()})
        entier(opt.get("reponses", "1"), "reponses", 1)
        return "plan_taguchi", base
    if famille == "surface":
        t = opt.get("type", "ccc").lower()
        if t not in dict(((c, 1) for _, c in doe.TYPES_SURFACE)):
            raise doe.ErreurDOE("type : ccc, ccf, cci ou bb.")
        base.update({"facteurs": facteurs, "type": t, "repetitions": rep})
        if "centres" in opt:
            base["points_centraux"] = entier(opt["centres"], "centres")
        if t == "ccc" and "alpha" in opt:
            a = opt["alpha"].lower()
            if a in ("rotatable", "orthogonal"):
                base["alpha_mode"] = a
            else:
                v = doe.lire_nombre(a, "alpha")
                if v is None or v <= 0:
                    raise doe.ErreurDOE("alpha : rotatable, orthogonal ou un nombre strictement positif.")
                base.update({"alpha_mode": "personnalise", "alpha": v})
        elif t == "ccc":
            base["alpha_mode"] = "rotatable"
        return "plan_surface", base
    t = opt.get("type", "reseau").lower()
    if t not in ("reseau", "centroide", "sommets"):
        raise doe.ErreurDOE("type : reseau, centroide ou sommets.")
    base.update({"composants": facteurs, "type": t, "repetitions": rep, "points_centraux": entier(opt.get("centres", "1"), "centres"),
                 "axiaux": booleen(opt.get("axiaux", "non"), "axiaux"), "aretes": booleen(opt.get("aretes", "non"), "aretes"),
                 "faces": booleen(opt.get("faces", "non"), "faces")})
    if t == "reseau":
        base["degre"] = entier(opt.get("degre", "2"), "degre", 1)
    return "plan_melange", base


class _Fichier:
    """Dossier temporaire à supprimer après envoi (même interface que Resultat)."""
    def __init__(self, dossier):
        self.dossier = dossier

    def liberer(self):
        shutil.rmtree(self.dossier, ignore_errors=True)


# =============================================================================
#  Session d'une conversation : plans + commandes
# =============================================================================
class Session:
    def __init__(self):
        self.plans = {}                           # famille -> doe.Plan
        self.optimums = {}                        # famille -> dernier optimum calculé
        self.courante = None
        self.attente = None                       # saisie en attente : {"type": "plan"|"reponses", ...}
        self.alpha = 0.05
        self.modeles = {}                         # famille -> dernières options de modèle (reprises par défaut)

    # -- plan actif ---------------------------------------------------------------
    def plan(self):
        if self.courante is None or self.courante not in self.plans:
            raise doe.ErreurDOE("Aucun plan en cours. Choisissez une famille de plans : /criblage, /factoriel, /taguchi, /surface ou /melange.")
        return self.plans[self.courante]

    def en_tete(self):
        p = self.plan()
        return f"{NOMS_FAMILLES[self.courante]} — {p.n} essais"

    # -- aiguillage -------------------------------------------------------------------
    def handle(self, texte):
        """Retourne (items, résultats à libérer après envoi)."""
        a_liberer = []
        try:
            return self._handle(texte, a_liberer), a_liberer
        except doe.ErreurDOE as e:
            return [msg("⚠️ " + str(e))], a_liberer

    def _res(self, res, a_liberer, famille=None):
        a_liberer.append(res)
        if res.optimum and famille:
            self.optimums[famille] = res.optimum
        return items_resultat(res)

    def _handle(self, texte, a_liberer):
        texte = texte.strip()
        if not texte.startswith("/"):
            return self.texte_libre(texte, a_liberer)
        premiere, _, suite = texte.partition("\n")
        parts = premiere.split()
        cmd = parts[0].lower().split("@")[0]
        cmd = cmd.replace("é", "e").replace("è", "e")
        args = parts[1:]
        if cmd != "/annuler":
            self.attente = None
        table = {"/start": self.c_start, "/aide": self.c_aide, "/help": self.c_aide, "/etat": self.c_etat, "/effacer": self.c_effacer,
                 "/annuler": self.c_annuler, "/export": self.c_export, "/famille": self.c_famille, "/plan": self.c_plan,
                 "/reponses": self.c_reponses, "/analyse": self.c_analyse, "/graphiques": self.c_graphiques, "/prevoir": self.c_prevoir,
                 "/optimiser": self.c_optimiser, "/alpha": self.c_alpha}
        if cmd[1:] in FAMILLES:
            return self.c_definir(cmd[1:], args, suite, a_liberer)
        f = table.get(cmd)
        if f is None:
            return [msg("Commande inconnue. /aide pour la liste.")]
        return f(args, suite, a_liberer)

    def texte_libre(self, texte, a_liberer):
        att = self.attente
        if att:
            self.attente = None
            if att["type"] == "plan":
                return self.definir(att["famille"], texte, a_liberer)
            if att["type"] == "reponses":
                return self.integrer(texte, att["standard"])
            if att["type"] == "points":
                return self.c_prevoir([], texte, a_liberer)
        if self.courante in self.plans:                       # colonnes de nombres sans commande : réponses
            try:
                _, vals = doe.lire_tableau_texte(texte)
            except doe.ErreurDOE:
                vals = None
            if vals is not None and len(vals) == self.plans[self.courante].n:
                return self.integrer(texte, False)
        return [msg("Je n'ai pas compris ce message. Utilisez les boutons ou /aide.", BTN_MENU)]

    # -- aide -----------------------------------------------------------------------------
    def c_start(self, args, suite, a_liberer):
        return [msg("🧪 DOE — plans d'expériences\n\nChoisissez une famille de plans, définissez les facteurs, saisissez les réponses "
                    "mesurées, puis analysez, prévoyez et optimisez. /aide pour le détail.", BTN_MENU)]

    def c_aide(self, args, suite, a_liberer):
        return [msg(
            "🧪 DOE — aide\n\n"
            "Démarche\n"
            "1. Créer un plan : /criblage /factoriel /taguchi /surface /melange (la commande affiche la syntaxe des facteurs).\n"
            "2. /reponses puis envoyer les mesures (une colonne par réponse, une ligne par essai, dans l'ordre d'exécution du plan ; "
            "« /reponses standard » pour l'ordre standard). Nom de colonne en première ligne facultatif.\n"
            "3. /analyse [rep=Y1] [modele=…] [reduire] [seuil=0,05]\n"
            "   Taguchi : /analyse [rep=Y1,Y2] [rsb=plus_grand|plus_petit|nominal_1|nominal_2] [regroupement=non] [confirmation=3]\n"
            "4. /graphiques [rep=Y1] [X Y] [contour|surface|les_deux] [A=valeur …]   (factoriel, surface, mélange)\n"
            "   Mélange : /graphiques [rep=Y1] [A B C]  (composants du triangle)\n"
            "5. /prevoir   puis un point par ligne : valeurs dans l'ordre des facteurs, ou A=… B=… (lettres des facteurs), "
            "ou « centre », ou « optimum »\n"
            "6. /optimiser [Y1=max] [Y2=min] [Y3=cible:12]   (limites : max:inf:cible, min:cible:sup, cible:valeur:inf:sup)\n\n"
            "Modèles (modele=) : criblage ordre1 ordre2 · factoriel ordre1 à ordre4 · surface lineaire interactions quadratique · "
            "melange lineaire quadratique cubique_special cubique.\n\n"
            "Autres\n/famille · /plan · /etat · /alpha [valeur] · /export · /effacer · /annuler\n\n"
            "Les plans ne sont conservés qu'en mémoire du bot, le temps de la conversation.", BTN_MENU)]

    def c_annuler(self, args, suite, a_liberer):
        self.attente = None
        return [msg("Saisie annulée.", BTN_MENU)]

    def c_alpha(self, args, suite, a_liberer):
        if not args:
            return [msg(f"Risque α actuel : {doe.fmt_nombre(self.alpha, 4)}. Modifier : /alpha 0,01")]
        self.alpha = doe.lire_alpha(args[0])
        return [msg(f"Risque α : {doe.fmt_nombre(self.alpha, 4)}.")]

    # -- création d'un plan --------------------------------------------------------------------
    def c_definir(self, famille, args, suite, a_liberer):
        texte = (" ".join(args) + "\n" + suite).strip()
        if texte:
            return self.definir(famille, texte, a_liberer)
        self.attente = {"type": "plan", "famille": famille}
        return [msg(AIDE_PLAN[famille] + "\n\n/annuler pour abandonner.")]

    def definir(self, famille, texte, a_liberer):
        lignes, opt = decouper_definition(texte)
        cfg = FAMILLES[famille]
        facteurs = lire_facteurs(famille, lignes)
        if not cfg["nmin"] <= len(facteurs) <= cfg["nmax"]:
            raise doe.ErreurDOE(f"Ce plan accepte de {cfg['nmin']} à {cfg['nmax']} {cfg['mot_f'].lower()} ({len(facteurs)} saisi(s)).")
        commande, params = parametres_plan(famille, facteurs, opt)
        plan = doe.generer_plan(famille, commande, params, facteurs)
        nrep = entier(opt.get("reponses", "1"), "reponses", 1) if famille == "taguchi" else 1
        for i in range(nrep):
            plan.ajouter_reponse(f"Y{i + 1}")
        self.plans[famille] = plan
        self.optimums.pop(famille, None)
        self.modeles.pop(famille, None)
        self.courante = famille
        return self.items_plan(plan, f"✅ Plan généré : {plan.n} essais.", avec_description=True)

    # -- affichage du plan -------------------------------------------------------------------
    def items_plan(self, plan, titre, avec_description=False):
        facteurs = plan.noms_facteurs
        codes = [lettre(i) for i in range(len(facteurs))]
        cols = [c for c in ("Ordre_essai",) if c in plan.cols] + facteurs + plan.reponses
        entetes = ["N°"] + [codes[facteurs.index(c)] if c in facteurs else c[:7] for c in cols[1:]] if cols and cols[0] == "Ordre_essai" else \
            [codes[facteurs.index(c)] if c in facteurs else c[:7] for c in cols]
        lignes = []
        for i in range(plan.n):
            ligne = []
            for c in cols:
                v = plan.cols[c][i]
                ligne.append("" if v is None else doe.fmt_nombre(v, 5)[:9])
            lignes.append(ligne)
        larg = [max(len(entetes[j]), *(len(l[j]) for l in lignes)) for j in range(len(cols))]
        fmt = lambda l: "  ".join(v.rjust(larg[j]) for j, v in enumerate(l))
        table = "\n".join([fmt(entetes), "  ".join("-" * w for w in larg)] + [fmt(l) for l in lignes])
        legende = "\n".join(f"{codes[i]} = {n}" + (f" ({doe.fmt_nombre(f['bas'], 6)} à {doe.fmt_nombre(f['haut'], 6)})" if "bas" in f and "haut" in f
                            else f" ({' ; '.join(str(x) for x in f['niveaux'])})" if "niveaux" in f else "")
                            for i, (n, f) in enumerate(zip(facteurs, plan.facteurs)))
        blocs = [f"<b>{esc(titre)}</b>", f"<b>{esc(plan.titre)}</b>", esc(legende)]
        blocs.extend(blocs_pre(table))
        lignes_desc = [l for l in plan.lignes if l.strip() != plan.titre.strip()]
        if avec_description and lignes_desc:
            blocs.append(esc("\n".join(lignes_desc)))
        if avec_description:
            for nom, t in plan.tables.items():
                cl = list(t.keys())
                rows = [list(r) for r in zip(*[doe._cellules(t[c]) for c in cl])]
                blocs.append(f"<b>{esc(doe.libelle(nom))}</b>")
                blocs.extend(blocs_pre(doe.table_texte(cl, rows, libre=True)))
        manque = [r for r in plan.reponses if plan.nb_renseignes(r) < plan.n]
        suite = ("Prochaine étape : /reponses pour saisir les mesures (colonne(s) : " + ", ".join(plan.reponses) + ").") if manque else "Réponses saisies : /analyse."
        items = [msg(t, html_=True) for t in regrouper(blocs)]
        items.append(msg(suite, [[("✍️ Saisir les réponses", "/reponses"), ("📤 Export CSV", "/export")], [("📊 Analyse", "/analyse")]]))
        return items

    def c_plan(self, args, suite, a_liberer):
        return self.items_plan(self.plan(), self.en_tete(), avec_description=bool(args and args[0].lower() == "detail"))

    def c_famille(self, args, suite, a_liberer):
        if args:
            cle = args[0].lower().replace("é", "e")
            if cle not in FAMILLES:
                raise doe.ErreurDOE("Famille inconnue : criblage, factoriel, taguchi, surface ou melange.")
            if cle not in self.plans:
                raise doe.ErreurDOE(f"Aucun plan « {NOMS_FAMILLES[cle]} » en cours : /{cle} pour en créer un.")
            self.courante = cle
            return self.items_plan(self.plans[cle], self.en_tete())
        if not self.plans:
            raise doe.ErreurDOE("Aucun plan en cours.")
        b = [[(("▶ " if c == self.courante else "") + f"{NOMS_FAMILLES[c]} ({p.n} essais)", f"/famille {c}")] for c, p in self.plans.items()]
        return [msg("Plan actif : choisir la famille.", b, remplace=True)]

    def c_etat(self, args, suite, a_liberer):
        if not self.plans:
            return [msg("Aucun plan en cours.", BTN_MENU)]
        lignes = []
        for c, p in self.plans.items():
            rep = ", ".join(f"{r} ({p.nb_renseignes(r)}/{p.n})" for r in p.reponses) or "aucune réponse"
            lignes.append(f"{'▶ ' if c == self.courante else '• '}{NOMS_FAMILLES[c]} : {p.n} essais ; facteurs : {', '.join(p.noms_facteurs)} ; réponses : {rep}")
        lignes.append(f"\nRisque α : {doe.fmt_nombre(self.alpha, 4)}")
        return [msg("\n".join(lignes), BTN_MENU)]

    def c_effacer(self, args, suite, a_liberer):
        p = self.plan()
        cle = self.courante
        del self.plans[cle]
        self.optimums.pop(cle, None)
        self.courante = next(iter(self.plans), None)
        return [msg(f"🗑 Plan « {NOMS_FAMILLES[cle]} » effacé.", BTN_MENU)]

    def c_export(self, args, suite, a_liberer):
        p = self.plan()
        dossier = Path(tempfile.mkdtemp(prefix="doe_export_"))
        chemin = dossier / f"plan_{self.courante}_{datetime.now():%Y%m%d_%H%M}.csv"
        p.exporter_csv(chemin)
        a_liberer.append(_Fichier(dossier))
        return [{"document": chemin}]

    # -- réponses ---------------------------------------------------------------------------------
    def c_reponses(self, args, suite, a_liberer):
        p = self.plan()
        standard = any(a.lower() in ("standard", "std") for a in args)
        texte = suite.strip()
        if texte:
            return self.integrer(texte, standard)
        self.attente = {"type": "reponses", "standard": standard}
        ordre = "l'ordre standard" if standard else "l'ordre d'exécution"
        return [msg(f"✍️ Réponses — plan « {NOMS_FAMILLES[self.courante]} » ({p.n} essais)\n\n"
                    f"Envoyez les mesures dans {ordre} du plan : une ligne par essai, une colonne par réponse "
                    f"(séparateurs : espace, tabulation ou point-virgule ; décimale point ou virgule).\n"
                    "Une première ligne de noms est facultative : sans noms, les colonnes vont aux réponses du plan "
                    f"({', '.join(p.reponses) or 'aucune'}) ; avec un nom nouveau, une réponse est créée.\n"
                    "Un fichier csv ou txt joint est aussi accepté.\n\n/annuler pour abandonner.")]

    def integrer(self, texte, standard):
        p = self.plan()
        n = doe.integrer_reponses(p, texte, standard)
        self.optimums.pop(self.courante, None)
        manque = [r for r in p.reponses if p.nb_renseignes(r) < p.n]
        txt = f"✅ {n} colonne(s) de réponses enregistrée(s) ({', '.join(p.reponses)})."
        if manque:
            txt += "\n⚠️ Valeurs manquantes : " + ", ".join(f"{r} ({p.nb_renseignes(r)}/{p.n})" for r in manque) + "."
        return [msg(txt, [[("📊 Analyse", "/analyse"), ("📋 Plan", "/plan")]])]

    def integrer_fichier(self, texte, standard=False):
        return self.integrer(texte, standard)

    # -- options communes -----------------------------------------------------------------------
    def _options(self, args):
        """args -> (clé=valeur, mots libres)."""
        opt, libres = {}, []
        for a in args:
            if "=" in a and not a.startswith("="):
                k, v = a.split("=", 1)
                opt[k.strip().lower()] = v.strip()
            else:
                libres.append(a)
        return opt, libres

    def _reponse(self, opt, libres, prefixe, a_liberer=None, invite="Quelle réponse ?", suppl=()):
        """Nom de la réponse choisie ; sinon retourne un message de choix (tuple (None, items))."""
        p = self.plan()
        dispo = [r for r in p.reponses if p.nb_renseignes(r) >= 2]
        if not dispo:
            raise doe.ErreurDOE("Aucune réponse renseignée : /reponses pour saisir les mesures.")
        r = opt.get("rep") or opt.get("reponse")
        if r is None and libres and libres[0] in dispo and prefixe:
            r = libres[0]
        if r is None:
            if len(dispo) == 1:
                return dispo[0], None
            b = [[(n, f"{prefixe} rep={n}")] for n in dispo] + [[(l, c)] for l, c in suppl]
            return None, [msg(invite, b, remplace=True)]
        trouve = [n for n in p.reponses if n.lower() == r.lower()]
        if not trouve:
            raise doe.ErreurDOE(f"Réponse introuvable : « {r} » (réponses : {', '.join(p.reponses)}).")
        return trouve[0], None

    def _modele(self, opt):
        cle = self.courante
        if cle == "taguchi":
            return None
        code = opt.get("modele", self.modeles.get(cle, {}).get("modele", doe.MODELE_DEFAUT[cle])).lower()
        if code not in MODELES_COURTS[cle]:
            raise doe.ErreurDOE(f"Modèle « {code} » inconnu pour ce plan. Modèles : {', '.join(MODELES_COURTS[cle])}.")
        return code

    def _params_modele(self, opt, libres):
        """Options du modèle ; celles de la dernière analyse sont reprises si rien n'est précisé."""
        prec = self.modeles.get(self.courante, {})
        if "reduire" in [l.lower() for l in libres]:
            reduire = True
        elif "reduire" in opt:
            reduire = opt["reduire"].lower() in ("oui", "1", "vrai")
        else:
            reduire = prec.get("reduire", False)
        seuil = doe.lire_alpha(opt["seuil"]) if "seuil" in opt else prec.get("alpha_sortie", 0.05)
        p = {"modele": self._modele(opt), "reduire": reduire, "alpha_sortie": seuil}
        if self.courante == "melange":
            p["pseudo"] = booleen(opt["pseudo"], "pseudo") if "pseudo" in opt else prec.get("pseudo", True)
        self.modeles[self.courante] = dict(p)
        return p

    def _params_taguchi(self, opt):
        p = self.plan()
        if "rep" in opt or "reponse" in opt or "reponses" in opt:
            noms = [x for x in re.split(r"[,;]+", opt.get("rep") or opt.get("reponse") or opt["reponses"]) if x.strip()]
            reps = []
            for n in noms:
                t = [r for r in p.reponses if r.lower() == n.strip().lower()]
                if not t:
                    raise doe.ErreurDOE(f"Réponse introuvable : « {n} » (réponses : {', '.join(p.reponses)}).")
                reps.append(t[0])
        else:
            reps = [r for r in p.reponses if p.nb_renseignes(r) == p.n]
        if not reps:
            raise doe.ErreurDOE("Aucune réponse complète : /reponses pour saisir les mesures.")
        for r in reps:
            if p.nb_renseignes(r) < p.n:
                raise doe.ErreurDOE(f"La réponse « {r} » n'est pas renseignée pour tous les essais.")
        rsb = opt.get("rsb", "plus_grand").lower()
        if rsb not in [c for _, c in doe.TYPES_RSB]:
            raise doe.ErreurDOE("rsb : plus_grand, plus_petit, nominal_1 ou nominal_2.")
        nc = doe.lire_nombre(opt.get("confirmation", "3"), "confirmation")
        if nc is None or nc < 1:
            raise doe.ErreurDOE("confirmation : au moins 1 essai.")
        return {"reponses": reps, "type_rsb": rsb, "regroupement": "aucun" if opt.get("regroupement", "auto").lower() in ("non", "aucun") else "auto",
                "n_confirmation": nc}

    # -- analyse ---------------------------------------------------------------------------------
    def c_analyse(self, args, suite, a_liberer):
        p = self.plan()
        opt, libres = self._options(args)
        if self.courante == "taguchi":
            params = self._params_taguchi(opt)
        else:
            r, items = self._reponse(opt, libres, "/analyse", invite="Analyse : quelle réponse ?")
            if items:
                return items
            if p.nb_renseignes(r) < 2:
                raise doe.ErreurDOE(f"La réponse « {r} » doit être renseignée pour au moins deux essais.")
            params = {"reponse": r, **self._params_modele(opt, libres)}
        return self._calcul("analyse", params, a_liberer)

    def _calcul(self, commande, params, a_liberer):
        res = doe.executer_r(self.plan(), commande, params, 1.2, self.alpha)
        return self._res(res, a_liberer, self.courante)

    # -- graphiques du modèle ----------------------------------------------------------------------
    def c_graphiques(self, args, suite, a_liberer):
        p = self.plan()
        cle = self.courante
        if cle not in ("factoriel", "surface", "melange"):
            raise doe.ErreurDOE("Les graphiques du modèle concernent les plans factoriel, de surface et de mélange (l'analyse fournit déjà les graphiques des autres plans).")
        opt, libres = self._options(args)
        r, items = self._reponse(opt, libres, "/graphiques", invite="Graphiques : quelle réponse ?")
        if items:
            return items
        params = {"reponse": r, **self._params_modele(opt, libres)}
        noms = p.noms_facteurs
        codes = {lettre(i).lower(): n for i, n in enumerate(noms)}
        codes.update({n.lower(): n for n in noms})
        mots = [l for l in libres if l.lower() not in ("reduire",) and l.lower() != r.lower()]
        if cle == "melange":
            if len(noms) < 3:
                raise doe.ErreurDOE("Le diagramme ternaire exige au moins trois composants.")
            choix = [codes[m.lower()] for m in mots if m.lower() in codes][:3] or noms[:3]
            if len(set(choix)) < 3:
                raise doe.ErreurDOE("Indiquer trois composants différents pour le triangle (ex. : A B C).")
            params["composants_axes"] = [noms.index(c) + 1 for c in choix]
            params["fixes"] = {codes[k.lower()]: doe.lire_nombre(v) for k, v in opt.items() if k.lower() in codes and v}
            params["type"] = "contour"
        else:
            quant = [f["nom"] for f in p.facteurs if f.get("type", "num") == "num"]
            axes = [codes[m.lower()] for m in mots if m.lower() in codes and codes[m.lower()] in quant]
            if len(axes) < 2:
                axes = quant[:2]
            if len(axes) < 2 or axes[0] == axes[1]:
                raise doe.ErreurDOE("Il faut deux facteurs quantitatifs différents pour les axes (ex. : A B).")
            type_ = next((m.lower() for m in mots if m.lower() in ("contour", "surface", "les_deux")), "contour")
            fixes = {}
            for k, v in opt.items():
                if k.lower() in codes and v:
                    n = codes[k.lower()]
                    f = next(f for f in p.facteurs if f["nom"] == n)
                    fixes[n] = v if f.get("type") == "cat" else doe.lire_nombre(v, f"la valeur de « {n} »")
            params.update({"axe_x": axes[0], "axe_y": axes[1], "type": type_, "fixes": fixes})
        return self._calcul("surfaces", params, a_liberer)

    # -- prévision -----------------------------------------------------------------------------------
    def _valeur_centrale(self, f):
        if "niveaux" in f:
            return str(f["niveaux"][0]) if self.courante == "taguchi" or f.get("type") == "cat" else None
        if self.courante == "melange":
            col = [v for v in self.plans[self.courante].cols[f["nom"]] if v is not None]
            return sum(col) / len(col) if col else None
        return (f["bas"] + f["haut"]) / 2

    def _point(self, ligne):
        p = self.plan()
        cle = self.courante
        noms = p.noms_facteurs
        codes = {lettre(i).lower(): n for i, n in enumerate(noms)}
        codes.update({n.lower(): n for n in noms})
        t = ligne.strip()
        pt = {}
        if t.lower() in ("centre", "central", "centrale", "milieu"):
            pass
        elif t.lower() == "optimum":
            o = self.optimums.get(cle)
            if not o:
                raise doe.ErreurDOE("Aucun optimum disponible : lancer d'abord " + ("/analyse." if cle == "taguchi" else "/optimiser."))
            for n, v in o.items():
                if n in noms:
                    pt[n] = v
        elif "=" in t:
            for jeton in t.replace(";", " ").split():
                if "=" not in jeton:
                    raise doe.ErreurDOE(f"« {jeton} » : forme attendue A=valeur.")
                k, v = jeton.split("=", 1)
                if k.lower() not in codes:
                    raise doe.ErreurDOE(f"Facteur inconnu : « {k} » (lettres : {', '.join(lettre(i) for i in range(len(noms)))}).")
                pt[codes[k.lower()]] = v
        else:
            vals = [x for x in re.split(r"[;\s]+", t) if x]
            if len(vals) != len(noms):
                raise doe.ErreurDOE(f"{len(noms)} valeur(s) attendue(s) (dans l'ordre : {', '.join(noms)}), {len(vals)} reçue(s).")
            pt = dict(zip(noms, vals))
        sortie = {}
        for f in p.facteurs:
            n = f["nom"]
            v = pt.get(n)
            if v is None:
                v = self._valeur_centrale(f)
            if v is None:
                raise doe.ErreurDOE(f"Valeur manquante pour « {n} ».")
            if cle == "taguchi" or f.get("type") == "cat":
                niv = [str(x) for x in f["niveaux"]]
                if str(v) not in niv:
                    raise doe.ErreurDOE(f"« {n} » : niveau « {v} » inconnu (niveaux : {' ; '.join(niv)}).")
                sortie[n] = str(v)
            else:
                sortie[n] = v if isinstance(v, (int, float)) else doe.lire_nombre(v, f"la valeur de « {n} »")
        return sortie

    CLES_MODELE = ("rep", "reponse", "reponses", "modele", "seuil", "pseudo", "rsb", "regroupement", "confirmation")

    def c_prevoir(self, args, suite, a_liberer):
        p = self.plan()
        opt, _ = self._options([a for a in args if "=" in a and a.split("=")[0].lower() in self.CLES_MODELE])
        lignes = [l for l in suite.replace("\r", "").split("\n") if l.strip()]
        inline = " ".join(a for a in args if not ("=" in a and a.split("=")[0].lower() in self.CLES_MODELE))
        if inline.strip():
            lignes.insert(0, inline.strip())
        if not lignes:
            self.attente = {"type": "points"}
            noms = p.noms_facteurs
            return [msg("🎯 Prévision — un point par ligne :\n"
                        f"• valeurs dans l'ordre des facteurs ({', '.join(noms)}), séparées par des espaces ;\n"
                        "• ou A=valeur B=valeur… (lettres des facteurs ; un facteur omis prend sa valeur centrale) ;\n"
                        "• ou « centre » ; ou « optimum » (dernier optimum calculé).\n\n/annuler pour abandonner.",
                        [[("Centre du domaine", "/prevoir centre"), ("Optimum", "/prevoir optimum")]])]
        points = [self._point(l) for l in lignes[:30]]
        if self.courante == "taguchi":
            params = self._params_taguchi(opt)
        else:
            r, items = self._reponse(opt, [], "")
            if items:                                           # plusieurs réponses : demander laquelle, en reprenant les points
                ptxt = " ".join(lignes[:1]) if len(lignes) == 1 else ""
                b = [[(n, f"/prevoir rep={n} {ptxt}".strip())] for n in p.reponses if p.nb_renseignes(n) >= 2]
                if len(lignes) > 1:
                    return [msg("Plusieurs points : préciser la réponse avec rep=Y1 (ex. : /prevoir rep=Y1 puis un point par ligne).")]
                return [msg("Prévision : quelle réponse ?", b, remplace=True)]
            params = {"reponse": r, **self._params_modele(opt, [])}
        params["points"] = points
        return self._calcul("prevision", params, a_liberer)

    # -- optimisation -----------------------------------------------------------------------------------
    def c_optimiser(self, args, suite, a_liberer):
        p = self.plan()
        if self.courante == "taguchi":
            raise doe.ErreurDOE("Pour un plan de Taguchi, les niveaux optimaux sont fournis par /analyse (puis /prevoir optimum).")
        opt, libres = self._options(args)
        dispo = [r for r in p.reponses if p.nb_renseignes(r) >= 2]
        if not dispo:
            raise doe.ErreurDOE("Aucune réponse renseignée : /reponses pour saisir les mesures.")
        objectifs = {}
        for cle, v in opt.items():
            if cle in ("modele", "seuil", "pseudo", "reduire"):
                continue
            nom = next((r for r in dispo if r.lower() == cle), None)
            if nom is None:
                raise doe.ErreurDOE(f"Réponse inconnue ou non renseignée : « {cle} » (réponses : {', '.join(dispo)}).")
            objectifs[nom] = v
        if not objectifs:
            if len(dispo) == 1:
                b = [[("Maximiser", f"/optimiser {dispo[0]}=max"), ("Minimiser", f"/optimiser {dispo[0]}=min")]]
                return [msg(f"Optimisation de « {dispo[0]} » : quel objectif ?\n(Plusieurs réponses : /optimiser Y1=max Y2=min Y3=cible:12)", b, remplace=True)]
            return [msg("🏁 Optimisation — indiquer l'objectif de chaque réponse :\n"
                        f"/optimiser {' '.join(r + '=max' for r in dispo[:2])}\n\n"
                        "Objectifs : max, min, cible:valeur\n"
                        "Limites facultatives : max:inf:cible · min:cible:sup · cible:valeur:inf:sup\n"
                        "Importance facultative : ajouter /2 (ex. : Y1=max/2)")]
        reps = []
        for nom, v in objectifs.items():
            imp = 1.0
            if "/" in v:
                v, i = v.rsplit("/", 1)
                imp = doe.lire_nombre(i, f"l'importance de « {nom} »")
                if imp is None or imp <= 0:
                    raise doe.ErreurDOE(f"L'importance de « {nom} » doit être strictement positive.")
            champs = v.split(":")
            but = BUTS.get(champs[0].lower())
            if but is None:
                raise doe.ErreurDOE(f"Objectif « {champs[0]} » inconnu pour « {nom} » : max, min ou cible.")
            nums = [doe.lire_nombre(c, f"« {nom} »") for c in champs[1:]]
            d = {"nom": nom, "but": but, "importance": imp}
            if but == "max":
                for k, x in zip(("inf", "cible"), nums):
                    if x is not None:
                        d[k] = x
            elif but == "min":
                for k, x in zip(("cible", "sup"), nums):
                    if x is not None:
                        d[k] = x
            else:
                if not nums:
                    raise doe.ErreurDOE(f"« {nom} » : indiquer la valeur cible (cible:valeur).")
                for k, x in zip(("cible", "inf", "sup"), nums):
                    if x is not None:
                        d[k] = x
            reps.append(d)
        params = {**self._params_modele(opt, libres), "reponses": reps}
        return self._calcul("optimisation", params, a_liberer)


# =============================================================================
#  Bot
# =============================================================================
class Bot:
    def __init__(self):
        sec = lire_config_telegram()
        self.token = os.getenv("TELEGRAM_TOKEN") or sec.get("token_doe", "")
        if not self.token:
            sys.exit("Token introuvable : TELEGRAM_TOKEN ou token_doe dans ~/.telegram_config")
        brut = os.getenv("ALLOWED_IDS") or sec.get("allowed_ids", "")
        self.autorises = {x.strip() for x in brut.split(",") if x.strip()}
        self.proprio = sec.get("chat_id", "").strip()          # reçoit le message de démarrage
        if self.proprio:
            self.autorises.add(self.proprio)
        self.url = f"https://api.telegram.org/bot{self.token}"
        self.sessions = {}
        self.verrous = {}
        self.global_lock = threading.Lock()

    # -- utilitaires
    def masquer(self, texte):
        """Retire le token de tout texte destiné aux logs ou au chat."""
        return str(texte).replace(self.token, "***")

    def api(self, methode, **params):
        r = requests.post(f"{self.url}/{methode}", data=params, timeout=70)
        return r.json()

    def envoyer_texte(self, chat_id, m):
        params = {}
        if m.get("boutons"):
            params["reply_markup"] = clavier(m["boutons"])
        if m.get("html"):
            rep = self.api("sendMessage", chat_id=chat_id, text=m["texte"], parse_mode="HTML", **params)
            if rep.get("ok"):
                return
            self.api("sendMessage", chat_id=chat_id, text=sans_html(m["texte"]), **params)   # repli sans mise en forme
            return
        texte = m["texte"]
        morceaux = [texte[i:i + 3900] for i in range(0, len(texte), 3900)] or ["…"]
        for i, morceau in enumerate(morceaux):
            p = params if i == len(morceaux) - 1 else {}
            self.api("sendMessage", chat_id=chat_id, text=morceau, **p)

    def editer(self, chat_id, message_id, m):
        """Remplace un message existant (menus). Retombe sur un nouvel envoi si l'édition échoue."""
        params = {"reply_markup": clavier(m["boutons"])} if m.get("boutons") else {}
        rep = self.api("editMessageText", chat_id=chat_id, message_id=message_id, text=m["texte"][:4000], **params)
        if not rep.get("ok") and "not modified" not in str(rep.get("description", "")):
            self.envoyer_texte(chat_id, m)

    def envoyer_fichier(self, chat_id, chemin, photo=False):
        methode, champ = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
        with open(chemin, "rb") as f:
            requests.post(f"{self.url}/{methode}", data={"chat_id": chat_id},
                          files={champ: (Path(chemin).name, f)}, timeout=60)

    def telecharger(self, file_id):
        info = self.api("getFile", file_id=file_id)
        if not info.get("ok"):
            raise RuntimeError("Telegram refuse le téléchargement du fichier")
        r = requests.get(f"https://api.telegram.org/file/bot{self.token}/{info['result']['file_path']}", timeout=60)
        r.raise_for_status()
        return r.content

    def _saisie(self, chat_id, arret):
        """Maintient l'indicateur « en train d'écrire » pendant les calculs R."""
        while not arret.is_set():
            try:
                self.api("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            arret.wait(4)

    def session(self, chat_id):
        with self.global_lock:
            cle = str(chat_id)
            if cle not in self.sessions:
                self.sessions[cle] = Session()
                self.verrous[cle] = threading.Lock()
            return self.sessions[cle], self.verrous[cle]

    # -- exécution d'un travail pour un utilisateur (un seul à la fois par conversation)
    def _executer(self, chat_id, travail):
        session, verrou = self.session(chat_id)
        with verrou:
            arret = threading.Event()
            threading.Thread(target=self._saisie, args=(chat_id, arret), daemon=True).start()
            try:
                travail(session)
            except Exception as e:
                try:
                    self.envoyer_texte(chat_id, msg(self.masquer(f"⚠️ Erreur interne : {type(e).__name__}: {e}")))
                except Exception:
                    pass
            finally:
                arret.set()

    def _livrer(self, chat_id, items, a_liberer=(), message_id=None):
        try:
            for i, it in enumerate(items):
                if "photo" in it:
                    self.envoyer_fichier(chat_id, it["photo"], photo=True)
                elif "document" in it:
                    self.envoyer_fichier(chat_id, it["document"])
                elif i == 0 and it.get("remplace") and message_id:
                    self.editer(chat_id, message_id, it)
                else:
                    self.envoyer_texte(chat_id, it)
        finally:
            for r in a_liberer:
                r.liberer()

    # -- message texte ou fichier (réponses au format csv / txt)
    def traiter(self, chat_id, m):
        def travail(session):
            if m.get("text"):
                items, lib = session.handle(m["text"])
                self._livrer(chat_id, items, lib)
            elif m.get("document"):
                doc = m["document"]
                nom = Path(doc.get("file_name") or "reponses").name
                if Path(nom).suffix.lower() not in (".csv", ".txt", ".tsv", ".dat"):
                    self.envoyer_texte(chat_id, msg("⚠️ Format non pris en charge. Formats acceptés pour les réponses : csv, txt, tsv, dat."))
                elif (doc.get("file_size") or 0) > MAX_FICHIER:
                    self.envoyer_texte(chat_id, msg("⚠️ Fichier trop volumineux : 5 Mo au maximum."))
                else:
                    contenu = self.telecharger(doc["file_id"])
                    try:
                        texte = contenu.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        texte = contenu.decode("latin-1")
                    try:
                        standard = bool(session.attente and session.attente.get("type") == "reponses" and session.attente.get("standard"))
                        session.attente = None
                        items = session.integrer(texte, standard)
                    except doe.ErreurDOE as e:
                        items = [msg(f"⚠️ {e}")]
                    self._livrer(chat_id, items)
            elif m.get("photo"):
                self.envoyer_texte(chat_id, msg("Les images ne sont pas analysées ici. Envoyez les mesures en texte ou en fichier (csv, txt)."))
        self._executer(chat_id, travail)

    # -- clic sur un bouton : la commande du bouton est exécutée comme si elle était tapée
    def traiter_bouton(self, chat_id, cb):
        data = (cb.get("data") or "").strip()
        message_id = (cb.get("message") or {}).get("message_id")
        try:
            self.api("answerCallbackQuery", callback_query_id=cb["id"])
        except Exception:
            pass
        if not data.startswith("/"):
            return

        def travail(session):
            items, lib = session.handle(data)
            self._livrer(chat_id, items, lib, message_id)
        self._executer(chat_id, travail)

    # -- message de démarrage
    def message_demarrage(self):
        ntp_ok = attendre_ntp(timeout=60)
        ok_r, info_r = doe.verifier_r()
        texte = (f"🧪 Bot DOE démarré\n"
                 f"📅 {datetime.now():%d/%m/%Y %H:%M:%S}{'' if ntp_ok else chr(10) + '⚠️ Heure non synchronisée NTP'}\n"
                 f"{'✅' if ok_r else '⚠️'} {info_r}\n\n"
                 "Choisissez une famille de plans pour commencer.")
        self.envoyer_texte(self.proprio, msg(texte, BTN_MENU))

    # -- boucle principale
    def run(self):
        try:
            self.api("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDES_MENU]))
        except Exception as e:
            print(f"[menu] {self.masquer(e)}")
        if not self.autorises:
            print("⚠️ Aucun identifiant autorisé : le bot est ouvert à tous.")
        print(f"Bot DOE démarré — {len(self.autorises)} identifiant(s) autorisé(s). Ctrl+C pour arrêter.")
        if self.proprio:                                       # confirmation de démarrage dans Telegram
            try:
                self.message_demarrage()
            except Exception as e:
                print(f"[démarrage] message non envoyé : {self.masquer(e)}")
        offset = None
        while True:
            try:
                params = {"timeout": 50}
                if offset:
                    params["offset"] = offset
                data = self.api("getUpdates", **params)
                if not data.get("ok"):                         # 409 (2 instances), 401, 429…
                    print(f"[polling] Telegram refuse : {data.get('error_code')} {data.get('description')}")
                    time.sleep(10)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    cb = upd.get("callback_query")
                    m = upd.get("message") or {}
                    chat_id = ((cb or {}).get("message") or m).get("chat", {}).get("id")
                    if not chat_id:
                        continue
                    if self.autorises and str(chat_id) not in self.autorises:
                        self.envoyer_texte(chat_id, msg(f"Accès non autorisé (votre identifiant : {chat_id})."))
                        continue
                    if cb:
                        threading.Thread(target=self.traiter_bouton, args=(chat_id, cb), daemon=True).start()
                    else:
                        threading.Thread(target=self.traiter, args=(chat_id, m), daemon=True).start()
            except KeyboardInterrupt:
                break
            except requests.exceptions.ReadTimeout:
                pass                                           # attente longue expirée : normal, on repart
            except Exception as e:
                print(f"[polling] {type(e).__name__}: {self.masquer(e)}")
                time.sleep(5)


if __name__ == "__main__":
    Bot().run()
