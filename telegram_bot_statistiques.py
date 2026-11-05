#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Bot Telegram « Statistiques » — interface Telegram de Statistiques.
#  Importe statistiques.py (même cœur que l'application de bureau ; calculs réalisés par R).
#  - Données : un message texte (une série par colonne, noms de colonnes en première
#    ligne) ou un fichier joint (csv, txt, tsv, dat, xlsx) — saisie possible depuis le S23
#  - Analyses : descriptif, normalité, tests t, ANOVA (+ Tukey), capabilité, Kappa ;
#    menus à boutons, résultats en texte à chasse fixe + graphiques en images
#  - Les données d'une conversation restent en mémoire du bot (rien n'est écrit sur disque)
#
#  Configuration (aucun secret dans ce fichier) :
#    Token   : variable TELEGRAM_TOKEN, sinon ~/.telegram_config  [telegram] token_statistiques
#    Accès   : ALLOWED_IDS (variable ou .env, séparés par des virgules) ou
#              ~/.telegram_config  [telegram] allowed_ids ; le chat_id du fichier est toujours
#              autorisé. Si aucun identifiant n'est connu, le bot est ouvert à tous (déconseillé).
#    Un utilisateur refusé reçoit son identifiant Telegram, à ajouter à ALLOWED_IDS.
#  Dépendance : requests (+ R et r-cran-jsonlite pour les calculs)
#  Lancement  : python3 telegram_bot_statistiques.py
#  Service    : voir la fiche « Bot Telegram » (systemd, ...)
#  Auteur : Jean-François BRUNET – JFBConseils – Octobre 2026
#  Rev.   :
# =============================================================================

import atexit
import html
import json
import os
import re
import sys
import tempfile
import threading
import time
import subprocess
import configparser
from datetime import datetime
from pathlib import Path

import requests

try:
    import statistiques as st
except ImportError:
    sys.path.insert(0, str(Path.home() / "Projects" / "Statistiques"))
    import statistiques as st

COMMANDES_MENU = [
    ("donnees", "Charger des données (texte ou fichier)"),
    ("etat", "Données chargées : colonnes et effectifs"),
    ("descriptif", "Statistiques descriptives"),
    ("normalite", "Test de normalité (Shapiro-Wilk)"),
    ("t", "Test t (1 ou 2 échantillons, apparié)"),
    ("anova", "ANOVA à un facteur (+ Tukey)"),
    ("capa", "Capabilité (Pp/Ppk, Cp/Cpk)"),
    ("kappa", "Kappa de Cohen"),
    ("lecture", "Options de lecture (séparateur, décimale)"),
    ("export", "Récupérer les données en CSV"),
    ("effacer", "Effacer les données"),
    ("annuler", "Annuler la saisie en attente"),
    ("aide", "Aide"),
]

EXTENSIONS = {".csv", ".txt", ".tsv", ".dat", ".xlsx"}
LARGEUR_MOBILE = 42          # au-delà, les tableaux sont présentés en fiches
LIMITE_MESSAGE = 3600

BTN_MENU = [[("📈 Descriptif", "/descriptif"), ("🔔 Normalité", "/normalite")],
            [("⚖️ Test t", "/t"), ("🧪 ANOVA", "/anova")],
            [("🎯 Capabilité", "/capa"), ("🤝 Kappa", "/kappa")],
            [("📤 Export CSV", "/export"), ("📋 Données", "/etat")]]


# =============================================================================
#  Utilitaires Telegram (même structure que le bot Problem Solving)
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


def items_resultat(res):
    blocs = []
    if res.titre:
        blocs.append(f"📊 <b>{esc(res.titre)}</b>")
    if res.lignes:
        blocs.append(esc("\n".join(res.lignes)))
    for nom, (cols, rows) in res.tables.items():
        large = st.table_texte(cols, rows, "large")
        mode = "large" if max(len(l) for l in large.split("\n")) <= LARGEUR_MOBILE else "fiche"
        corps = large if mode == "large" else st.table_texte(cols, rows, "fiche")
        blocs.append(f"<b>{esc(st.libelle(nom))}</b>")
        blocs.extend(blocs_pre(corps))
    if res.avertissements:
        blocs.append("<i>" + esc("\n".join("Remarque : " + a for a in res.avertissements)) + "</i>")
    items = [msg(t, html_=True) for t in regrouper(blocs)]
    items += [{"photo": f} for f in res.figures]
    items.append(msg("Autre analyse ?", BTN_MENU))
    return items


# =============================================================================
#  Session d'une conversation : données + commandes
# =============================================================================
class Session:
    def __init__(self):
        self.etude = st.Etude()
        self.attente = None                       # saisie en attente (valeur de référence, limites…)
        self.lecture = {"sep": "auto", "dec": "auto", "entete": "auto"}
        self.brut = None                          # ("texte", str) ou ("fichier", Path) pour relire avec d'autres options

    # -- données -------------------------------------------------------------------
    def donnees(self):
        if self.etude.donnees is None:
            raise st.ErreurStat("Aucune donnée chargée. Envoyez un tableau en message texte (une série par colonne, "
                                "noms de colonnes en première ligne) ou un fichier. /donnees pour le détail.")
        return self.etude.donnees

    def colonne(self, ref, kind="num"):
        d = self.donnees()
        ref = str(ref).strip()
        if ref.isdigit():
            i = int(ref) - 1
            if not 0 <= i < len(d.noms):
                raise st.ErreurStat(f"Numéro de colonne invalide : {ref} (voir /etat).")
            nom = d.noms[i]
        else:
            trouves = [n for n in d.noms if n.lower() == ref.lower()]
            if not trouves:
                raise st.ErreurStat(f"Colonne introuvable : « {ref} » (voir /etat).")
            nom = trouves[0]
        if kind == "num" and d.types[nom] != "num":
            raise st.ErreurStat(f"La colonne « {nom} » n'est pas numérique.")
        return nom

    def index(self, nom):
        return self.donnees().noms.index(nom) + 1

    def boutons_colonnes(self, prefixe, kind="num", exclure=(), supplements=()):
        d = self.donnees()
        noms = [n for n in d.noms if (kind == "tout" or d.types[n] == kind) and n not in exclure]
        if kind == "tout":
            noms = sorted(noms, key=lambda n: d.types[n] == "num")     # colonnes texte d'abord
        boutons, rang = [], []
        for n in noms:
            rang.append((f"{self.index(n)}. {n[:18]}", f"{prefixe} {self.index(n)}"))
            if len(rang) == 2:
                boutons.append(rang)
                rang = []
        if rang:
            boutons.append(rang)
        for lib, cmd in supplements:
            boutons.append([(lib, cmd)])
        return boutons, noms

    def liste_colonnes(self):
        d = self.donnees()
        return "\n".join(f"{i}. {n} ({'numérique' if d.types[n] == 'num' else 'texte'})" for i, n in enumerate(d.noms, 1))

    # -- chargement -------------------------------------------------------------------
    def apres_chargement(self, origine):
        d = self.donnees()
        txt = f"✅ Données chargées ({origine}) : {d.resume()}\n\n{self.liste_colonnes()}"
        if d.avertissements:
            txt += "\n\n⚠️ " + "\n⚠️ ".join(d.avertissements)
        txt += "\n\nLes numéros ci-dessus peuvent remplacer les noms dans les commandes."
        return [msg(txt, BTN_MENU)]

    def charger_texte(self, texte):
        self.etude.charger_texte(texte, **self.lecture)
        self.brut = ("texte", texte)
        return self.apres_chargement("saisie")

    def liberer_fichier(self):
        """Supprime le dossier temporaire du fichier mémorisé pour relecture."""
        if self.brut and self.brut[0] == "fichier":
            import shutil
            shutil.rmtree(self.brut[1].parent, ignore_errors=True)

    def charger_fichier(self, chemin, nom):
        self.etude.charger_fichier(chemin, **self.lecture)
        if self.brut and self.brut[0] == "fichier" and self.brut[1] != Path(chemin):
            self.liberer_fichier()
        self.brut = ("fichier", Path(chemin))
        return self.apres_chargement(nom)

    # -- commandes ----------------------------------------------------------------------
    def handle(self, texte):
        """Retourne (items, résultats à libérer après envoi)."""
        a_liberer = []
        try:
            return self._handle(texte, a_liberer), a_liberer
        except st.ErreurStat as e:
            return [msg("⚠️ " + str(e))], a_liberer

    def _res(self, res, a_liberer):
        a_liberer.append(res)
        return items_resultat(res)

    def _handle(self, texte, a_liberer):
        texte = texte.strip()
        if not texte.startswith("/"):
            return self.texte_libre(texte, a_liberer)
        parts = texte.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        if cmd not in ("/annuler",):
            self.attente = None if cmd != "/lecture" else self.attente
        table = {"/start": self.c_start, "/aide": self.c_aide, "/help": self.c_aide, "/donnees": self.c_donnees,
                 "/etat": self.c_etat, "/effacer": self.c_effacer, "/annuler": self.c_annuler, "/export": self.c_export,
                 "/lecture": self.c_lecture, "/descriptif": self.c_descriptif, "/normalite": self.c_normalite,
                 "/t": self.c_t, "/anova": self.c_anova, "/capa": self.c_capa, "/kappa": self.c_kappa}
        f = table.get(cmd)
        if f is None:
            return [msg("Commande inconnue. /aide pour la liste.")]
        return f(args, a_liberer)

    def texte_libre(self, texte, a_liberer):
        if self.attente:
            return self.reponse_attendue(texte, a_liberer)
        try:
            return self.charger_texte(texte)
        except st.ErreurStat as e:
            return [msg(f"⚠️ {e}\n\nEnvoyez des données (une série par colonne, noms de colonnes en première ligne) "
                        f"ou un fichier. /aide pour le détail.")]

    # -- aide -----------------------------------------------------------------------------
    def c_start(self, args, a_liberer):
        return [msg("📊 Statistiques\n\nEnvoyez vos données en message texte ou en fichier joint "
                    "(csv, txt, tsv, dat, xlsx), puis choisissez une analyse. /aide pour le détail.", BTN_MENU)]

    def c_aide(self, args, a_liberer):
        return [msg(
            "📊 Statistiques — aide\n\n"
            "Données\n"
            "• Message texte : une série par colonne, noms de colonnes en 1ère ligne.\n"
            "• Séparateurs reconnus : tabulation, point-virgule, virgule, espace ; décimale point ou virgule.\n"
            "• Fichier joint : csv, txt, tsv, dat, xlsx (20 Mo au maximum). Pour de gros volumes, préférer un fichier "
            "(un message Telegram est limité à 4096 caractères).\n"
            "• Chaque nouvel envoi remplace les données précédentes. /lecture permet de forcer séparateur, décimale, ligne de noms.\n\n"
            "Analyses (boutons ou commandes)\n"
            "/descriptif\n/normalite [colonne]\n"
            "/t [X] [Y|0] [welch|apparie|egales] [sup|inf] [mu=valeur]\n"
            "/anova [colonnes | mesures groupes]\n"
            "/capa [colonne] [LSL] [USL] [cible] [σ intra]  (« - » pour une valeur absente)\n"
            "/kappa [évaluation 1] [évaluation 2]\n"
            "Les colonnes se désignent par leur numéro (voir /etat) ou leur nom.\n\n"
            "Autres\n/etat · /export · /effacer · /annuler\n\n"
            "Les données ne sont conservées qu'en mémoire du bot, le temps de la conversation.", BTN_MENU)]

    def c_donnees(self, args, a_liberer):
        return [msg("Pour charger des données :\n• collez-les dans un message (une série par colonne, noms de colonnes en première ligne) ;\n"
                    "• ou joignez un fichier csv, txt, tsv, dat ou xlsx.\n\n"
                    "Une analyse par groupes demande une colonne de mesures et une colonne de groupes.")]

    def c_etat(self, args, a_liberer):
        d = self.donnees()
        t = f"{d.resume()}\n\n{self.liste_colonnes()}"
        o = self.lecture
        if o != {"sep": "auto", "dec": "auto", "entete": "auto"}:
            t += f"\n\nOptions de lecture : séparateur {o['sep']!r}, décimale {o['dec']!r}, noms de colonnes {o['entete']}."
        return [msg(t, BTN_MENU)]

    def c_effacer(self, args, a_liberer):
        self.etude.donnees = None
        self.liberer_fichier()
        self.brut = None
        return [msg("🗑 Données effacées.")]

    def c_annuler(self, args, a_liberer):
        self.attente = None
        return [msg("Saisie annulée.", BTN_MENU)]

    def c_export(self, args, a_liberer):
        d = self.donnees()
        dossier = Path(tempfile.mkdtemp(prefix="stat_export_"))
        chemin = dossier / f"donnees_{datetime.now():%Y%m%d_%H%M}.csv"
        chemin.write_text(d.vers_csv_texte(), encoding="utf-8-sig")
        a_liberer.append(_Fichier(dossier))
        return [{"document": chemin}]

    def c_lecture(self, args, a_liberer):
        if not args:
            o = self.lecture
            return [msg("Options de lecture actuelles :\n"
                        f"• séparateur : {o['sep']!r}\n• décimale : {o['dec']!r}\n• ligne de noms de colonnes : {o['entete']}\n\n"
                        "Modifier : /lecture sep=<tab|;|,|espace|auto> dec=<point|virgule|auto> entete=<oui|non|auto>\n"
                        "Réinitialiser : /lecture auto\nLes données déjà reçues sont relues avec les nouvelles options.")]
        corresp = {"sep": {"tab": "\t", "tabulation": "\t", ";": ";", "pointvirgule": ";", ",": ",", "virgule": ",",
                           "espace": " ", "auto": "auto"},
                   "dec": {"point": ".", ".": ".", "virgule": ",", ",": ",", "auto": "auto"},
                   "entete": {"oui": True, "non": False, "auto": "auto"}}
        if args == ["auto"]:
            self.lecture = {"sep": "auto", "dec": "auto", "entete": "auto"}
        else:
            for a in args:
                if "=" not in a:
                    raise st.ErreurStat("Syntaxe : /lecture sep=… dec=… entete=… (voir /lecture).")
                k, v = a.split("=", 1)
                k, v = k.lower(), v.lower()
                if k not in corresp or v not in corresp[k]:
                    raise st.ErreurStat(f"Option non reconnue : {a}")
                self.lecture[k] = corresp[k][v]
        if self.brut is None:
            return [msg("Options enregistrées ; elles s'appliqueront aux prochaines données.")]
        if self.brut[0] == "texte":
            return self.charger_texte(self.brut[1])
        return self.charger_fichier(self.brut[1], self.brut[1].name)

    # -- analyses -----------------------------------------------------------------------------
    def c_descriptif(self, args, a_liberer):
        self.donnees()
        return self._res(self.etude.descriptif(), a_liberer)

    def c_normalite(self, args, a_liberer):
        if not args:
            b, noms = self.boutons_colonnes("/normalite")
            self.verifier_choix(noms, "numérique")
            return [msg("Normalité : quelle série ?", b, remplace=True)]
        return self._res(self.etude.normalite(self.colonne(args[0])), a_liberer)

    def c_t(self, args, a_liberer):
        mots = {"welch", "apparie", "appariee", "egales", "sup", "inf"}
        mu = None
        libres = []
        options = set()
        for a in args:
            if a.lower().startswith("mu="):
                mu = st.lire_nombre(a[3:], "la valeur de référence")
            elif a.lower() in mots:
                options.add(a.lower())
            else:
                libres.append(a)
        if not libres:
            b, noms = self.boutons_colonnes("/t")
            self.verifier_choix(noms, "numérique")
            return [msg("Test t : choisir la série X.", b, remplace=True)]
        x = self.colonne(libres[0])
        ix = self.index(x)
        if len(libres) == 1:
            b, noms = self.boutons_colonnes(f"/t {ix}", exclure=(x,), supplements=[("Un seul échantillon (valeur de référence)", f"/t {ix} 0")])
            return [msg(f"Série X : {x}. Comparer à quelle série ?", b, remplace=True)]
        alt = "greater" if "sup" in options else "less" if "inf" in options else "two.sided"
        if libres[1] == "0":                                   # test à un échantillon
            if mu is None:
                self.attente = {"type": "mu", "x": x, "alt": alt}
                return [msg(f"Test t à un échantillon sur « {x} » : envoyez la valeur de référence (nombre). /annuler pour abandonner.")]
            return self._res(self.etude.test_t(x, None, mu, alternative=alt), a_liberer)
        y = self.colonne(libres[1])
        iy = self.index(y)
        mode = "apparie" if options & {"apparie", "appariee"} else "egales" if "egales" in options else "welch" if "welch" in options else None
        if mode is None:
            suf = ("" if alt == "two.sided" else (" sup" if alt == "greater" else " inf"))
            b = [[("Indépendants (Welch)", f"/t {ix} {iy} welch{suf}")], [("Appariés", f"/t {ix} {iy} apparie{suf}")],
                 [("Indépendants, variances égales", f"/t {ix} {iy} egales{suf}")]]
            return [msg(f"Comparer « {x} » et « {y} » : type de test ?", b, remplace=True)]
        return self._res(self.etude.test_t(x, y, 0.0, apparie=(mode == "apparie"), variances_egales=(mode == "egales"), alternative=alt), a_liberer)

    def c_anova(self, args, a_liberer):
        d = self.donnees()
        num, txt = d.noms_num, d.noms_txt
        if args and args[0].lower() == "colonnes":
            return self._res(self.etude.anova("large", colonnes=num), a_liberer)
        if len(args) >= 2:
            return self._res(self.etude.anova("long", valeur=self.colonne(args[0]), groupe=self.colonne(args[1], "tout")), a_liberer)
        if not num:
            raise st.ErreurStat("Aucune colonne numérique.")
        combos = [(v, g) for g in txt for v in num]
        if not combos:
            if len(num) < 2:
                raise st.ErreurStat("ANOVA : il faut au moins deux colonnes numériques (une par groupe) ou une colonne de mesures et une colonne de groupes.")
            return self._res(self.etude.anova("large", colonnes=num), a_liberer)
        if len(combos) == 1 and len(num) == 1:
            return self._res(self.etude.anova("long", valeur=combos[0][0], groupe=combos[0][1]), a_liberer)
        b = [[(f"{v[:14]} par {g[:14]}", f"/anova {self.index(v)} {self.index(g)}")] for v, g in combos[:8]]
        if len(num) >= 2:
            b.append([("Chaque colonne numérique = un groupe", "/anova colonnes")])
        return [msg("ANOVA : quelle organisation des données ?", b, remplace=True)]

    def c_capa(self, args, a_liberer):
        if not args:
            b, noms = self.boutons_colonnes("/capa")
            self.verifier_choix(noms, "numérique")
            return [msg("Capabilité : quelle série ?", b, remplace=True)]
        col = self.colonne(args[0])
        if len(args) == 1:
            self.attente = {"type": "capa", "col": col}
            return [msg(f"Capabilité de « {col} » : envoyez les limites sous la forme\nLSL USL [cible] [σ intra-sous-groupe]\n"
                        "séparées par des espaces ou des points-virgules ; « - » pour une valeur absente.\n"
                        "Sans σ intra-sous-groupe, seuls Pp et Ppk sont calculés. /annuler pour abandonner.")]
        return self._capa(col, args[1:], a_liberer)

    def _capa(self, col, jetons, a_liberer):
        v = [None if j in ("-", "_", "") else st.lire_nombre(j, "une limite") for j in jetons]
        v += [None] * (4 - len(v))
        if len(jetons) < 2 or len(jetons) > 4:
            raise st.ErreurStat("Indiquer de 2 à 4 valeurs : LSL USL [cible] [σ intra-sous-groupe].")
        lsl, usl, cible, sig = v[:4]
        return self._res(self.etude.capabilite(col, lsl, usl, cible, sig), a_liberer)

    def c_kappa(self, args, a_liberer):
        d = self.donnees()
        if len(d.noms) < 2:
            raise st.ErreurStat("Le Kappa demande 2 colonnes d'évaluations.")
        if not args:
            b, _ = self.boutons_colonnes("/kappa", "tout")
            return [msg("Kappa : évaluation de référence ?", b, remplace=True)]
        a = self.colonne(args[0], "tout")
        if len(args) == 1:
            b, _ = self.boutons_colonnes(f"/kappa {self.index(a)}", "tout", exclure=(a,))
            return [msg(f"Référence : {a}. Deuxième évaluation ?", b, remplace=True)]
        return self._res(self.etude.kappa(a, self.colonne(args[1], "tout")), a_liberer)

    def verifier_choix(self, noms, genre):
        if not noms:
            raise st.ErreurStat(f"Aucune colonne {genre} dans les données chargées (voir /etat).")

    # -- réponse à une question posée par le bot ----------------------------------------------------
    def reponse_attendue(self, texte, a_liberer):
        att = self.attente
        if att["type"] == "mu":
            mu = st.lire_nombre(texte, "la valeur de référence")
            if mu is None:
                raise st.ErreurStat("Envoyez un nombre.")
            self.attente = None
            return self._res(self.etude.test_t(att["x"], None, mu, alternative=att["alt"]), a_liberer)
        if att["type"] == "capa":
            jetons = [j for j in re.split(r"[;\s]+", texte.strip()) if j != ""]
            res = self._capa(att["col"], jetons, a_liberer)
            self.attente = None
            return res
        self.attente = None
        return [msg("Saisie annulée.")]


class _Fichier:
    """Dossier temporaire à supprimer après envoi (même interface que Resultat)."""
    def __init__(self, dossier):
        self.dossier = dossier

    def liberer(self):
        import shutil
        shutil.rmtree(self.dossier, ignore_errors=True)


# =============================================================================
#  Bot
# =============================================================================
class Bot:
    def __init__(self):
        sec = lire_config_telegram()
        self.token = os.getenv("TELEGRAM_TOKEN") or sec.get("token_statistiques", "")
        if not self.token:
            sys.exit("Token introuvable : TELEGRAM_TOKEN ou token_statistiques dans ~/.telegram_config")
        brut = os.getenv("ALLOWED_IDS") or sec.get("allowed_ids", "")
        self.autorises = {x.strip() for x in brut.split(",") if x.strip()}
        self.proprio = sec.get("chat_id", "").strip()          # reçoit le message de démarrage
        if self.proprio:
            self.autorises.add(self.proprio)
        self.url = f"https://api.telegram.org/bot{self.token}"
        self.sessions = {}
        self.verrous = {}
        self.global_lock = threading.Lock()
        atexit.register(self._nettoyer)

    def _nettoyer(self):
        for sess in list(self.sessions.values()):
            sess.liberer_fichier()

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

    # -- message texte ou fichier
    def traiter(self, chat_id, m):
        def travail(session):
            if m.get("text"):
                items, lib = session.handle(m["text"])
                self._livrer(chat_id, items, lib)
            elif m.get("document"):
                doc = m["document"]
                nom = Path(doc.get("file_name") or "donnees").name
                if Path(nom).suffix.lower() not in EXTENSIONS:
                    self.envoyer_texte(chat_id, msg("⚠️ Format non pris en charge. Formats acceptés : csv, txt, tsv, dat, xlsx."))
                elif (doc.get("file_size") or 0) > 20 * 1024 * 1024:
                    self.envoyer_texte(chat_id, msg("⚠️ Fichier trop volumineux : 20 Mo au maximum."))
                else:
                    donnees = self.telecharger(doc["file_id"])
                    dossier = Path(tempfile.mkdtemp(prefix="stat_fichier_"))
                    chemin = dossier / nom
                    chemin.write_bytes(donnees)
                    try:
                        try:
                            items = session.charger_fichier(chemin, nom)
                        except st.ErreurStat as e:
                            items = [msg(f"⚠️ {e}")]
                        self._livrer(chat_id, items)
                    finally:
                        if not (session.brut and session.brut[0] == "fichier" and session.brut[1] == chemin):
                            _Fichier(dossier).liberer()
            elif m.get("photo"):
                self.envoyer_texte(chat_id, msg("Les images ne sont pas analysées ici. Envoyez les données en texte ou en fichier (csv, txt, tsv, dat, xlsx)."))
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
        ok_r, info_r = st.verifier_r()
        texte = (f"📊 Bot Statistiques démarré\n"
                 f"📅 {datetime.now():%d/%m/%Y %H:%M:%S}{'' if ntp_ok else chr(10) + '⚠️ Heure non synchronisée NTP'}\n"
                 f"{'✅' if ok_r else '⚠️'} {info_r}\n\n"
                 "Envoyez des données (texte ou fichier) pour commencer.")
        self.envoyer_texte(self.proprio, msg(texte, BTN_MENU))

    # -- boucle principale
    def run(self):
        try:
            self.api("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDES_MENU]))
        except Exception as e:
            print(f"[menu] {self.masquer(e)}")
        if not self.autorises:
            print("⚠️ Aucun identifiant autorisé : le bot est ouvert à tous.")
        print(f"Bot Statistiques démarré — {len(self.autorises)} identifiant(s) autorisé(s). Ctrl+C pour arrêter.")
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
