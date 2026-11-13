#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Bot Telegram « Statistiques » — interface Telegram de Statistiques.
#  Importe statistiques.py (même cœur que l'application de bureau ; calculs réalisés par R).
#  - Données : un message texte (une série par colonne, noms de colonnes en première
#    ligne) ou un fichier joint (csv, txt, tsv, dat, xlsx) — saisie possible depuis le S23
#  - Analyses : descriptif, normalité, tests t, Z, F, Poisson, ANOVA, khi², estimation, identification de loi, corrélation,
#    régression, séries, cartes de contrôle (dont p, np, c et u, avant/après), graphiques, capabilité, R&R (croisée, emboîtée, type 1),
#    ANOVA à deux facteurs, Kappa, génération de données, analyse par groupe ;
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
    ("anova2", "ANOVA à deux facteurs"),
    ("anovan", "ANOVA à 3 facteurs ou plus"),
    ("rr", "R&R : croisée, emboîtée, type 1"),
    ("capa", "Capabilité (Pp/Ppk, Cp/Cpk)"),
    ("kappa", "Kappa de Cohen"),
    ("f", "Test F de comparaison de deux variances"),
    ("z", "Test Z (écart-type connu)"),
    ("poisson", "Test de Poisson (1 ou 2 échantillons)"),
    ("puissance", "Puissance et taille d'échantillon (courbe de puissance)"),
    ("estimation", "Estimation d'une moyenne (intervalle de confiance)"),
    ("identification", "Identification de loi"),
    ("correlation", "Corrélation"),
    ("regression", "Régression"),
    ("serie", "Série chronologique et tendances"),
    ("khi2", "Test du khi² d'indépendance"),
    ("carte", "Carte de contrôle (X-barre, I-MR, avant/après)"),
    ("cartep", "Cartes p, np, c et u (attributs)"),
    ("graphique", "Graphiques"),
    ("generer", "Générer des données aléatoires"),
    ("illustration", "Illustrations de référence"),
    ("vocabulaire", "Vocabulaire : définition d'un terme"),
    ("groupe", "Analyser par groupe (colonne de groupes)"),
    ("alpha", "Risque α des tests"),
    ("lecture", "Options de lecture (séparateur, décimale)"),
    ("export", "Récupérer les données en CSV"),
    ("supprimer", "Supprimer une ou plusieurs séries"),
    ("effacer", "Effacer toutes les données"),
    ("annuler", "Annuler la saisie en attente"),
    ("aide", "Aide"),
]

EXTENSIONS = {".csv", ".txt", ".tsv", ".dat", ".xlsx"}
LARGEUR_MOBILE = 42          # au-delà, les tableaux sont présentés en fiches
LIMITE_MESSAGE = 3600

BTN_MENU = [[("📈 Descriptif", "/descriptif"), ("🔔 Normalité", "/normalite")],
            [("⚖️ Test t", "/t"), ("🧪 ANOVA", "/anova")],
            [("🎯 Capabilité", "/capa"), ("🤝 Kappa", "/kappa")],
            [("📐 R&R", "/rr"), ("🧮 ANOVA 2 facteurs", "/anova2")],
            [("🔬 Test Z", "/z"), ("📐 Estimation", "/estimation")],
            [("🔎 Identif. de loi", "/identification"), ("🔗 Corrélation", "/correlation")],
            [("📉 Régression", "/regression"), ("⏳ Série chrono.", "/serie")],
            [("📊 Graphiques", "/graphique"), ("📏 Cartes", "/carte")],
            [("🎲 Générer", "/generer"), ("🖼 Illustrations", "/illustration")],
            [("📤 Export CSV", "/export"), ("📋 Données", "/etat")],
            [("🗑 Supprimer une série", "/supprimer")]]


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
        return "\n".join(f"{i}. {n} ({ {'num': 'numérique', 'date': 'date'}.get(d.types[n], 'texte') })" for i, n in enumerate(d.noms, 1))

    # -- chargement -------------------------------------------------------------------
    def _verifier_groupe(self):
        """Abandonne l'analyse par groupe si la colonne de groupes ou ses valeurs ont disparu des données."""
        g = self.etude.groupe
        d = self.etude.donnees
        if not g:
            return
        try:
            restantes = [v for v in g["valeurs"] if v in d.groupe_valeurs(g["colonne"], g.get("periode"))]
        except Exception:
            restantes = []
        self.etude.groupe = {**g, "valeurs": restantes} if restantes else None

    def apres_chargement(self, origine):
        d = self.donnees()
        self._verifier_groupe()
        txt = f"✅ Données chargées ({origine}) : {d.resume()}\n\n{self.liste_colonnes()}"
        if d.avertissements:
            txt += "\n\n⚠️ " + "\n⚠️ ".join(d.avertissements)
        if self.etude.groupe:
            txt += f"\n\n🗂 Analyse par groupe : {self.resume_groupe()} (/groupe aucun pour l'arrêter)."
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
                 "/etat": self.c_etat, "/effacer": self.c_effacer, "/supprimer": self.c_supprimer, "/annuler": self.c_annuler, "/export": self.c_export,
                 "/lecture": self.c_lecture, "/descriptif": self.c_descriptif, "/normalite": self.c_normalite,
                 "/t": self.c_t, "/anova": self.c_anova, "/anova2": self.c_anova2, "/anovan": self.c_anovan, "/rr": self.c_rr, "/capa": self.c_capa, "/kappa": self.c_kappa,
                 "/f": self.c_f, "/z": self.c_z, "/poisson": self.c_poisson, "/puissance": self.c_puissance, "/estimation": self.c_estimation,
                 "/identification": self.c_identification, "/correlation": self.c_correlation, "/regression": self.c_regression,
                 "/serie": self.c_serie, "/khi2": self.c_khi2, "/carte": self.c_carte, "/cartep": self.c_cartep,
                 "/graphique": self.c_graphique, "/generer": self.c_generer, "/illustration": self.c_illustration, "/vocabulaire": self.c_vocabulaire, "/alpha": self.c_alpha, "/groupe": self.c_groupe}
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
            "/anova2 [mesures facteurA facteurB] [interaction=non]\n"
            "/anovan mesures facteur1 facteur2 facteur3… [interactions=aucune|2|toutes]   (2 à 6 facteurs)\n"
            "/rr croisee|emboitee [pièces opérateurs mesures] [tol=…] [l=6] [alpha=0,05]   ou   /rr type1 mesures ref=… tol=… [k=20] [l=6]\n"
            "/capa [colonne] [LSL] [USL] [cible] [σ intra]  (« - » pour une valeur absente)\n"
            "/kappa [évaluation 1] [évaluation 2]\n"
            "/f [X] [Y] [sup|inf]\n"
            "/z [X] σ [mu=…] [sup|inf]   ou   /z X Y σX σY [mu=…]\n"
            "/puissance t1|tpaire|t2|z1|p1|p2|anova [sup|inf] [n=…] [diff=…] [puissance=…] [sd=…] [p0=…] [k=…] [courbe=10,20,40]   (laisser vide une des trois grandeurs n, diff, puissance : elle est calculée)\n"
            "/poisson X Y [ratio=…]   ou   /poisson X lambda0=… [duree=…]   ou   /poisson total=… duree=… lambda0=…\n"
            "/estimation [X] [niveau=95] [inf|sup] [marge=…] [sigma=…]   ou   /estimation n=… moyenne=… s=…\n"
            "/identification [colonnes] [lois=normale,gamma,…] [sim=200]\n"
            "/correlation [colonnes] [spearman|kendall]\n"
            "/regression Y X1 [X2 …] [deg=1|2|3]\n"
            "/serie Y [ordre=colonne] [modeles=lineaire,quadratique,exponentielle,logistique] [horizon=n]\n"
            "/khi2 A B  (indépendance, données brutes)\n"
            "/carte colonnes [type=imr|xbar_r|xbar_s] [ordre=col] [phase=col] [rupture=n]\n"
            "/cartep p|np|c|u comptages [effectifs] [n=…] [ordre=col] [phase=col] [rupture=n]   (carte c : sans effectif)\n"
            "/graphique type colonnes   (histogramme, secteurs, barres, bulles, nuage, individuelles, intervalles, boite, probabilite)\n"
            "/generer loi [paramètres] [n=30] [dec=3] [graine=…] [nom=…] [remplace]\n"
            "/groupe [colonne] [jour|mois|annee] [valeurs=a|b]   (aucun : analyse de l'ensemble) — toutes les analyses sont alors lancées groupe par groupe\n"
            "/illustration [sujet]   ·   /alpha [valeur]\n"
            "/vocabulaire [mot]   (définition d'un terme ; sans argument : la liste)\n"
            "Les colonnes se désignent par leur numéro (voir /etat) ou leur nom.\n\n"
            "Autres\n/etat · /export · /supprimer [colonnes] · /effacer · /annuler\n"
            "• /supprimer retire une ou plusieurs séries (colonnes) des données chargées ; sans argument, des boutons proposent chaque colonne.\n\n"
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
        if self.etude.groupe:
            t += f"\n\n🗂 Analyse par groupe : {self.resume_groupe()} (/groupe aucun pour l'arrêter)."
        return [msg(t, BTN_MENU)]

    def c_effacer(self, args, a_liberer):
        self.etude.donnees = None
        self.etude.groupe = None
        self.liberer_fichier()
        self.brut = None
        return [msg("🗑 Données effacées.")]

    def c_supprimer(self, args, a_liberer):
        d = self.donnees()
        libres, _ = self._kv(args)
        if libres and libres[0].lower() in ("tout", "toutes"):
            return self.c_effacer([], a_liberer)
        if not libres:
            boutons, _ = self.boutons_colonnes("/supprimer", "tout")
            boutons.append([("🗑 Tout effacer", "/effacer"), ("✅ Terminé", "/etat")])
            return [msg("Quelle série supprimer ?\n" + self.liste_colonnes() + "\n(ou /supprimer 2 3 · /supprimer nom)", boutons, remplace=True)]
        noms = []
        for ref in libres:
            n = self.colonne(ref, "tout")
            if n not in noms:
                noms.append(n)
        d.supprimer_colonnes(noms)
        self.brut = None                          # /lecture ne doit pas ressusciter les séries supprimées
        if not d.noms:
            return self.c_effacer([], a_liberer)
        self._verifier_groupe()
        boutons, _ = self.boutons_colonnes("/supprimer", "tout")
        boutons.append([("🗑 Tout effacer", "/effacer"), ("✅ Terminé", "/etat")])
        return [msg(f"🗑 Supprimé : {', '.join(noms)}.\n\n{d.resume()}\n\n{self.liste_colonnes()}\n\nAutre série à supprimer ?", boutons, remplace=True)]

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

    def c_anova2(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        suffixe = " interaction=non" if kv.get("interaction", "").lower() in ("non", "0", "false", "faux") else ""
        pref = "/anova2" + suffixe
        if not libres:
            return self._choisir_col(pref, "ANOVA à deux facteurs : colonne des mesures ? (option : interaction=non)")
        y = self.colonne(libres[0])
        if len(libres) == 1:
            return self._choisir_col(f"{pref} {self.index(y)}", f"Mesures : {y}. Facteur A ?", "tout", exclure=(y,))
        a = self.colonne(libres[1], "tout")
        if len(libres) == 2:
            return self._choisir_col(f"{pref} {self.index(y)} {self.index(a)}", f"Facteur A : {a}. Facteur B ?", "tout", exclure=(y, a))
        b = self.colonne(libres[2], "tout")
        return self._res(self.etude.commande("anova2", {"valeur": y, "facteur_a": a, "facteur_b": b, "interaction": not suffixe}), a_liberer)

    def c_anovan(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        if len(libres) < 3:
            return self._suite("/anovan", "ANOVA multi-facteurs : envoyez la colonne des mesures puis 2 à 6 colonnes de facteurs, "
                                          "par numéro ou par nom [interactions=aucune|2|toutes].\nExemple : 1 2 3 4")
        y = self.colonne(libres[0])
        fac = [self.colonne(x, "tout") for x in libres[1:]]
        inter = kv.get("interactions", "2").lower()
        return self._res(self.etude.commande("anovan", {"valeur": y, "facteurs": fac, "interactions": inter}), a_liberer)

    def c_rr(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        mode = libres[0].lower().replace("é", "e").replace("î", "i") if libres else ""
        mode = {"croisee": "croisee", "emboitee": "emboitee", "type1": "type1", "1": "type1"}.get(mode)
        if mode is None:
            b = [[("R&R croisée", "/rr croisee")], [("R&R emboîtée", "/rr emboitee")], [("R&R de type 1 (un étalon)", "/rr type1")]]
            return [msg("R&R : quel type d'étude ?\n• croisée : chaque opérateur mesure toutes les pièces\n"
                        "• emboîtée : chaque pièce n'est mesurée que par un seul opérateur (essai destructif)\n"
                        "• type 1 : une pièce étalon mesurée de nombreuses fois", b, remplace=True)]
        reste = libres[1:]
        options = "".join(f" {k}={v}" for k, v in kv.items())          # les options (tol, l, alpha…) suivent les boutons
        pref = f"/rr {mode}{options}"
        if mode == "type1":
            if not reste:
                return self._choisir_col(pref, "R&R de type 1 : colonne des mesures répétées de l'étalon ? (options : ref=…, tol=…, k=20, l=6)")
            col = self.colonne(reste[0])
            ref, tol = self._nombre(kv, "ref", "la valeur de référence"), self._nombre(kv, "tol", "la tolérance")
            if ref is None or tol is None:
                return self._suite(f"/rr type1 {self.index(col)}", f"R&R de type 1 sur « {col} » : envoyez ref=<valeur de référence de l'étalon> tol=<tolérance> [k=20] [l=6].")
            return self._res(self.etude.commande("rr1", {"colonne": col, "reference": ref, "tolerance": tol,
                                                         "k": self._nombre(kv, "k", "K"), "l": self._nombre(kv, "l", "L")}), a_liberer)
        if not reste:
            return self._choisir_col(pref, f"R&R {'croisée' if mode == 'croisee' else 'emboîtée'} : colonne des pièces ? (options : tol=…, l=6" + (", alpha=0,05)" if mode == "croisee" else ")"), "tout")
        piece = self.colonne(reste[0], "tout")
        if len(reste) == 1:
            return self._choisir_col(f"{pref} {self.index(piece)}", f"Pièces : {piece}. Colonne des opérateurs ?", "tout", exclure=(piece,))
        oper = self.colonne(reste[1], "tout")
        if len(reste) == 2:
            return self._choisir_col(f"{pref} {self.index(piece)} {self.index(oper)}", f"Opérateurs : {oper}. Colonne des mesures ?", exclure=(piece, oper))
        params = {"piece": piece, "operateur": oper, "mesure": self.colonne(reste[2]),
                  "tolerance": self._nombre(kv, "tol", "la tolérance"), "l": self._nombre(kv, "l", "L")}
        if mode == "croisee":
            params["alpha"] = self._nombre(kv, "alpha", "le seuil de l'interaction")
        return self._res(self.etude.commande("rr_croisee" if mode == "croisee" else "rr_emboitee", params), a_liberer)

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

    # -- nouvelles analyses ---------------------------------------------------------------------
    MOTS_ALT = {"sup": "greater", "inf": "less"}

    @staticmethod
    def _kv(args):
        """Sépare les arguments libres des arguments nom=valeur (noms en minuscules)."""
        libres, kv = [], {}
        for a in args:
            if "=" in a:
                k, v = a.split("=", 1)
                kv[k.strip().lower()] = v.strip()
            else:
                libres.append(a)
        return libres, kv

    @staticmethod
    def _nombre(kv, cle, nom):
        return st.lire_nombre(kv[cle], nom) if cle in kv else None

    def _alt(self, libres):
        """(arguments sans mots sup/inf, hypothèse alternative)."""
        alt = "two.sided"
        reste = []
        for a in libres:
            if a.lower() in self.MOTS_ALT:
                alt = self.MOTS_ALT[a.lower()]
            else:
                reste.append(a)
        return reste, alt

    def _suite(self, prefixe, texte):
        self.attente = {"type": "suite", "prefixe": prefixe}
        return [msg(texte + "\n/annuler pour abandonner.")]

    def _choisir_col(self, cmd, titre, kind="num", exclure=()):
        b, noms = self.boutons_colonnes(cmd, kind, exclure=exclure)
        self.verifier_choix(noms, "numérique" if kind == "num" else "")
        return [msg(titre, b, remplace=True)]

    def _cols(self, refs, kind="num"):
        return [self.colonne(r, kind) for r in refs]

    def _facultative(self, kv, cle):
        return self.colonne(kv[cle], "tout") if kv.get(cle) else None

    def c_alpha(self, args, a_liberer):
        if args:
            self.etude.alpha = st.lire_alpha(args[0])
        a = f"{self.etude.alpha:g}".replace(".", ",")
        return [msg(f"Risque α des tests : {a}. H0 est rejetée si la p-value est inférieure à α. Modifier : /alpha 0,01", BTN_MENU)]

    def c_f(self, args, a_liberer):
        self.donnees()
        libres, alt = self._alt(args)
        if not libres:
            return self._choisir_col("/f", "Test F : choisir la série X.")
        x = self.colonne(libres[0])
        if len(libres) == 1:
            return self._choisir_col(f"/f {self.index(x)}", f"Série X : {x}. Comparer à quelle série ?", exclure=(x,))
        return self._res(self.etude.test_f(x, self.colonne(libres[1]), alt), a_liberer)

    def c_z(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        libres, alt = self._alt(libres)
        if not libres:
            return self._choisir_col("/z", "Test Z : choisir la série X.")
        x = self.colonne(libres[0])
        if len(libres) == 1:
            return self._suite(f"/z {self.index(x)}", f"Test Z sur « {x} » : envoyez σ (écart-type connu de la population) [mu=valeur] [sup|inf],\n"
                                                       "ou, pour deux échantillons : Y σX σY [mu=différence].")
        mu = self._nombre(kv, "mu", "la valeur de référence") or 0.0
        if len(libres) == 2:
            params = {"mode": "1", "x": x, "sigma": st.lire_nombre(libres[1], "σ"), "mu": mu, "alternative": alt}
        elif len(libres) == 4:
            params = {"mode": "2", "x": x, "y": self.colonne(libres[1]), "sigma": st.lire_nombre(libres[2], "σX"),
                      "sigma2": st.lire_nombre(libres[3], "σY"), "mu": mu, "alternative": alt}
        else:
            raise st.ErreurStat("Syntaxe : /z X σ [mu=…]   ou   /z X Y σX σY [mu=…].")
        return self._res(self.etude.commande("z", params), a_liberer)

    TESTS_PUISSANCE = ("t1", "tpaire", "t2", "z1", "p1", "p2", "anova")

    def c_puissance(self, args, a_liberer):
        libres, kv = self._kv(args)
        libres, alt = self._alt(libres)
        if not libres or libres[0].lower() not in self.TESTS_PUISSANCE:
            return self._suite("/puissance", "Puissance et taille d'échantillon. Envoyez : test [sup|inf] n=… diff=… puissance=… sd=… (laisser vide une des trois grandeurs n, diff, puissance : elle est calculée).\n"
                                             "Tests : t1 (t à 1 échantillon), tpaire, t2 (2 échantillons), z1 (σ connu), p1 (1 proportion, binomiale exacte), p2 (2 proportions), anova (ajouter k=niveaux).\n"
                                             "Proportions : p0=proportion de référence, diff = proportion réelle − p0. Facultatif : courbe=10,20,40 (tailles tracées).\n"
                                             "Exemple : t2 diff=1 puissance=0,9 sd=1,5")
        n = lambda c, nom: self._nombre(kv, c, nom)
        p = {"test": libres[0].lower(), "alternative": alt, "n": n("n", "la taille d'échantillon"),
             "difference": n("diff", "la différence"), "puissance": n("puissance", "la puissance"),
             "ecart_type": n("sd", "l'écart-type"), "p0": n("p0", "la proportion de référence"),
             "k": n("k", "le nombre de niveaux") or 3, "n_courbe": kv.get("courbe", "")}
        return self._res(self.etude.commande_libre("puissance", p), a_liberer)

    def c_poisson(self, args, a_liberer):
        libres, kv = self._kv(args)
        libres, alt = self._alt(libres)
        if not libres and not kv:
            return self._suite("/poisson", "Test de Poisson. Envoyez par exemple :\n• X lambda0=2 [duree=1]  (un échantillon, colonne de comptages)\n"
                                           "• X Y [ratio=1]  (deux échantillons)\n• total=12 duree=4 lambda0=2  (totaux saisis)\n"
                                           "• total=12 duree=4 total2=15 duree2=5 [ratio=1]  (deux échantillons, totaux)")
        n = lambda c, nom: self._nombre(kv, c, nom)
        deux = bool(len(libres) >= 2 or "total2" in kv)
        p = {"mode": "2" if deux else "1", "alternative": alt}
        libre = "total" in kv
        if "total" in kv:
            p.update(source="resume", total=n("total", "le nombre total d'événements"), duree_tot=n("duree", "la taille totale observée"))
        else:
            if not libres:
                raise st.ErreurStat("Indiquer la colonne de comptages (ou total=… duree=…).")
            p.update(source="col", x=self.colonne(libres[0]), duree=n("duree", "la taille de chaque observation") or 1.0)
        if deux:
            if "total2" in kv:
                p.update(source2="resume", total2=n("total2", "le nombre total d'événements 2"), duree_tot2=n("duree2", "la taille totale observée 2"))
            else:
                p.update(source2="col", x2=self.colonne(libres[1]), duree2=n("duree2", "la taille de chaque observation 2") or 1.0)
                libre = False
            p["ratio0"] = n("ratio", "le rapport de référence") or 1.0
            libre = libre and p["source2"] == "resume"
        else:
            p["lambda0"] = n("lambda0", "le taux de référence λ0")
            if p["lambda0"] is None:
                raise st.ErreurStat("Indiquer le taux de référence : lambda0=…")
        if not libre:
            self.donnees()
        return self._res((self.etude.commande_libre if libre else self.etude.commande)("poisson", p), a_liberer)

    def c_estimation(self, args, a_liberer):
        libres, kv = self._kv(args)
        cote = "two.sided"
        reste = []
        for a in libres:
            if a.lower() == "inf":
                cote = "lower"
            elif a.lower() == "sup":
                cote = "upper"
            else:
                reste.append(a)
        niveau = self._nombre(kv, "niveau", "le niveau de confiance") or 95.0
        p = {"niveau": niveau, "cote": cote, "marge": self._nombre(kv, "marge", "la marge d'erreur"), "sigma": self._nombre(kv, "sigma", "σ")}
        if "n" in kv or "moyenne" in kv:
            p.update(source="resume", n=self._nombre(kv, "n", "n"), moyenne=self._nombre(kv, "moyenne", "la moyenne"),
                     ecart_type=self._nombre(kv, "s", "l'écart-type"))
            return self._res(self.etude.commande_libre("estimation", p), a_liberer)
        self.donnees()
        if not reste:
            return self._choisir_col("/estimation", "Estimation : quelle série ? (options : niveau=95, inf|sup, marge=…, sigma=…)")
        p.update(source="col", x=self.colonne(reste[0]))
        return self._res(self.etude.commande("estimation", p), a_liberer)

    def c_identification(self, args, a_liberer):
        d = self.donnees()
        libres, kv = self._kv(args)
        cols = self._cols(libres) if libres else d.noms_num
        if not cols:
            raise st.ErreurStat("Aucune colonne numérique.")
        lois = [x.strip() for x in kv.get("lois", "normale,lognormale,exponentielle,gamma,weibull,logistique,gumbel").split(",") if x.strip()]
        sim = self._nombre(kv, "sim", "le nombre de tirages") or 200
        return self._res(self.etude.commande("identification", {"colonnes": cols, "lois": lois, "simulations": sim}), a_liberer)

    def c_correlation(self, args, a_liberer):
        d = self.donnees()
        libres, kv = self._kv(args)
        methode = "pearson"
        reste = []
        for a in libres:
            if a.lower() in ("pearson", "spearman", "kendall"):
                methode = a.lower()
            else:
                reste.append(a)
        cols = self._cols(reste) if reste else d.noms_num
        if len(cols) < 2:
            raise st.ErreurStat("La corrélation demande au moins deux colonnes numériques.")
        return self._res(self.etude.commande("correlation", {"colonnes": cols, "methode": methode}), a_liberer)

    def c_regression(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        if not libres:
            return self._choisir_col("/regression", "Régression : choisir la réponse Y.")
        y = self.colonne(libres[0])
        if len(libres) == 1:
            return self._suite(f"/regression {self.index(y)}", f"Réponse : {y}. Envoyez la ou les variables explicatives X (numéros ou noms) [deg=2].")
        xs = [x for x in self._cols(libres[1:]) if x != y]
        deg = str(int(self._nombre(kv, "deg", "le degré") or 1))
        if deg not in ("1", "2", "3"):
            raise st.ErreurStat("Le degré doit être 1, 2 ou 3.")
        return self._res(self.etude.commande("regression", {"y": y, "x": xs, "degre": deg}), a_liberer)

    def c_serie(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        if not libres:
            return self._choisir_col("/serie", "Série chronologique : quelle série ? (options : ordre=colonne, modeles=…, horizon=n)")
        y = self.colonne(libres[0])
        modeles = [m.strip() for m in kv.get("modeles", "lineaire,quadratique,exponentielle,logistique").split(",") if m.strip()]
        return self._res(self.etude.commande("serie", {"y": y, "ordre": self._facultative(kv, "ordre"), "modeles": modeles,
                                                       "horizon": self._nombre(kv, "horizon", "l'horizon") or 0}), a_liberer)

    def c_khi2(self, args, a_liberer):
        d = self.donnees()
        if not args:
            return self._choisir_col("/khi2", "Khi² d'indépendance : variable A (lignes) ?", "tout")
        a = self.colonne(args[0], "tout")
        if len(args) == 1:
            return self._choisir_col(f"/khi2 {self.index(a)}", f"Variable A : {a}. Variable B (colonnes) ?", "tout", exclure=(a,))
        return self._res(self.etude.commande("khi2", {"test": "independance", "format": "brut", "a": a, "b": self.colonne(args[1], "tout")}), a_liberer)

    def c_carte(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        if not libres:
            return self._choisir_col("/carte", "Carte de contrôle : quelle série ? (plusieurs colonnes = sous-groupes ; options : type=imr|xbar_r|xbar_s, ordre=col, phase=col, rupture=n)")
        cols = self._cols(libres)
        type_ = kv.get("type", "auto")
        if type_ not in ("auto", "imr", "xbar_r", "xbar_s"):
            raise st.ErreurStat("Type de carte : auto, imr, xbar_r ou xbar_s.")
        p = {"type": type_, "format": "colonnes", "colonnes": cols, "valeur": "", "groupe": "", "tests": [1, 2, 3, 4],
             "ordre": self._facultative(kv, "ordre") or "", "phase": self._facultative(kv, "phase") or "",
             "rupture": self._nombre(kv, "rupture", "le numéro de point")}
        return self._res(self.etude.commande("carte", p), a_liberer)

    def c_cartep(self, args, a_liberer):
        self.donnees()
        libres, kv = self._kv(args)
        if not libres or libres[0].lower() not in ("p", "np", "c", "u"):
            return [msg("Cartes d'attributs : /cartep p|np|c|u comptages [effectifs] [n=effectif constant] [ordre=col] [phase=col] [rupture=n]\n"
                        "p : proportion d'unités défectueuses ; np : nombre d'unités défectueuses (effectif constant) ; "
                        "c : nombre de défauts par unité de contrôle de taille constante (sans effectif) ; u : défauts par unité.",
                        [[("Carte p", "/cartep p"), ("Carte np", "/cartep np")], [("Carte c", "/cartep c"), ("Carte u", "/cartep u")]], remplace=True)]
        type_ = libres[0].lower()
        if len(libres) == 1:
            suite = "(puis n=… ou la colonne d'effectifs)" if type_ != "c" else "(carte c : aucun effectif à fournir)"
            return self._choisir_col(f"/cartep {type_}", f"Quelle colonne de comptages ? {suite}")
        p = {"type": type_, "defauts": self.colonne(libres[1]),
             "effectif": self.colonne(libres[2]) if len(libres) > 2 and type_ != "c" else None,
             "n": self._nombre(kv, "n", "l'effectif constant"), "ordre": self._facultative(kv, "ordre"), "phase": self._facultative(kv, "phase"),
             "rupture": self._nombre(kv, "rupture", "le numéro de point"), "tests": [1, 2, 3, 4]}
        return self._res(self.etude.commande("carte_attributs", p), a_liberer)

    TYPES_GRAPH = {"histogramme": "Histogramme", "secteurs": "Secteurs", "barres": "Barres", "bulles": "Bulles", "nuage": "Nuage de points",
                   "individuelles": "Valeurs individuelles", "intervalles": "Intervalles", "boite": "Boîte à moustaches", "probabilite": "Diagramme de probabilité"}

    def c_graphique(self, args, a_liberer):
        d = self.donnees()
        libres, kv = self._kv(args)
        if not libres or libres[0].lower() not in self.TYPES_GRAPH:
            b = [[(lib, f"/graphique {code}") for code, lib in list(self.TYPES_GRAPH.items())[i:i + 2]] for i in range(0, len(self.TYPES_GRAPH), 2)]
            return [msg("Graphique : quel type ?", b, remplace=True)]
        code = libres[0].lower()
        if len(libres) == 1:
            aide = {"secteurs": "la colonne des catégories", "barres": "la colonne des catégories [valeur=colonne fonction=somme|moyenne]",
                    "bulles": "X Y Taille", "nuage": "X Y"}.get(code, "une ou plusieurs séries [groupe=colonne]")
            return self._suite(f"/graphique {code}", f"{self.TYPES_GRAPH[code]} : envoyez {aide} (numéros ou noms).")
        normale = any(c.lower() == "normale" for c in libres[1:]) or "normale" in kv
        cols = [c for c in libres[1:] if c.lower() != "normale"]
        p = {"type_graphique": code}
        if code in ("secteurs", "barres"):
            p.update(categorie=self.colonne(cols[0], "tout"), valeur=self._facultative(kv, "valeur"), groupe=self._facultative(kv, "groupe"),
                     fonction=kv.get("fonction", "effectif"))
        elif code in ("bulles", "nuage"):
            if len(cols) < 2 or (code == "bulles" and len(cols) < 3):
                raise st.ErreurStat("Nuage : X Y ; bulles : X Y Taille.")
            p.update(x=self.colonne(cols[0]), y=self.colonne(cols[1]), couleur=self._facultative(kv, "couleur"), ajust=True)
            if code == "bulles":
                p["taille"] = self.colonne(cols[2])
        else:
            p.update(colonnes=self._cols(cols), valeur=None, groupe=self._facultative(kv, "groupe"), normale=normale, loi=kv.get("loi", "normale"))
        return self._res(self.etude.commande("graphique", p), a_liberer)

    def c_generer(self, args, a_liberer):
        libres, kv = self._kv(args)
        lois = {l[1]: l for l in st.LOIS_GENERATION}
        if not libres or libres[0].lower() not in lois:
            b = [[(l[0], f"/generer {l[1]}") for l in st.LOIS_GENERATION[i:i + 2]] for i in range(0, len(st.LOIS_GENERATION), 2)]
            return [msg("Générer des données aléatoires : quelle loi ?", b, remplace=True)]
        lib, code, params, desc = lois[libres[0].lower()]
        valeurs = libres[1:]
        if not valeurs and not kv:
            noms = ", ".join(f"{p[1]} (défaut {p[2]})" for p in params)
            return self._suite(f"/generer {code}", f"{lib} — {desc}\nEnvoyez : {noms} [n=30] [dec=3] [graine=…] [nom=…] [remplace], ou « ok » pour les valeurs par défaut.")
        if valeurs == ["ok"]:
            valeurs = []
        if len(valeurs) > len(params):
            raise st.ErreurStat(f"Cette loi a {len(params)} paramètre(s).")
        val = {}
        for k, (cle, nom, defaut) in enumerate(params):
            v = st.lire_nombre(valeurs[k] if k < len(valeurs) else defaut, nom)
            val[cle] = v
        n = int(self._nombre(kv, "n", "le nombre de valeurs") or 30)
        dec = int(self._nombre(kv, "dec", "le nombre de décimales") if "dec" in kv else (0 if code == "aleatoire" else 3))
        graine = kv.get("graine")
        if code == "normale":
            vals = st.generer_normale(n, val["moyenne"], val["ecart_type"], dec, graine)
        elif code in ("uniforme", "aleatoire"):
            vals = st.generer_uniforme(n, val["min"], val["max"], dec, graine)
        else:
            vals = self.etude.generer_loi(code, val, n, dec, graine)
        nom = kv.get("nom") or f"Serie_{(len(self.etude.donnees.noms) if self.etude.donnees else 0) + 1}"
        nom = self.etude.generer(nom, vals, remplacer=("remplace" in libres or "remplace" in kv))
        self.brut = None
        return [msg(f"🎲 {len(vals)} valeur(s) générée(s) ({lib}) : variable « {nom} ».")] + self.apres_chargement("génération")

    def c_illustration(self, args, a_liberer):
        sujets = dict(st.ILLUSTRATIONS)
        if not args or args[0] not in sujets:
            b = [[(titre[:30], f"/illustration {cle}")] for cle, titre in st.ILLUSTRATIONS if st.images_illustration(cle)]
            if not b:
                return [msg("Aucune illustration n'est disponible dans le dossier « illustrations » du programme.")]
            return [msg("Illustrations : quel sujet ?", b, remplace=True)]
        images = st.images_illustration(args[0])
        if not images:
            return [msg(f"Aucune image pour « {sujets[args[0]]} ».")]
        return [msg(f"🖼 {sujets[args[0]]}")] + [{"photo": f} for f in images]

    def c_vocabulaire(self, args, a_liberer):
        termes = st.lire_vocabulaire()
        if not termes:
            return [msg("Le vocabulaire est vide : le fichier « vocabulaire.txt » est absent du dossier du programme.")]
        requete = " ".join(args).strip()
        if not requete:
            lettres = sorted({st.cle_tri_texte(t)[:1].upper() for t, _ in termes if st.cle_tri_texte(t)[:1].isalpha()})
            b = [[(l, f"/vocabulaire ={l}") for l in lettres[i:i + 6]] for i in range(0, len(lettres), 6)]
            return [msg(f"Vocabulaire : {len(termes)} terme(s). Envoyez /vocabulaire <mot> (par exemple /vocabulaire capabilité), ou choisissez une initiale.", b, remplace=True)]
        if requete.startswith("="):                                    # liste des termes d'une initiale
            ini = st.cle_tri_texte(requete[1:2])
            trouves = [e for e in termes if st.cle_tri_texte(e[0]).startswith(ini)]
            exact = None
        else:
            trouves = st.chercher_vocabulaire(termes, requete)
            q = st.cle_tri_texte(requete)
            exact = next((e for e in trouves if st.cle_tri_texte(e[0]) == q), None)
        if not trouves:
            return [msg(f"Aucun terme ne correspond à « {requete} ». /vocabulaire pour la liste.")]
        if exact or len(trouves) == 1:
            t, d = exact or trouves[0]
            return [msg(f"📖 {t}\n\n{d}", BTN_MENU)]
        boutons = []
        for t, _ in trouves[:16]:
            cmd = "/vocabulaire " + t
            while len(cmd.encode("utf-8")) > 60:
                cmd = cmd[:-1]
            boutons.append([(t[:40], cmd)])
        plus = f" (16 premiers sur {len(trouves)} : préciser le mot)" if len(trouves) > 16 else ""
        return [msg(f"{len(trouves)} terme(s) trouvé(s){plus} :", boutons, remplace=True)]

    # -- analyse par groupe ---------------------------------------------------------------------
    def resume_groupe(self):
        g = self.etude.groupe
        if not g:
            return "aucune"
        d = self.donnees()
        nom = g["colonne"] + ({"jour": " (par jour)", "mois": " (par mois)", "annee": " (par année)"}.get(g.get("periode") or "jour", "") if d.types.get(g["colonne"]) == "date" else "")
        return f"{nom} — {len(g['valeurs'])} groupe(s) : {', '.join(g['valeurs'][:12])}{' …' if len(g['valeurs']) > 12 else ''}"

    def c_groupe(self, args, a_liberer):
        d = self.donnees()
        libres, kv = self._kv(args)
        if libres and libres[0].lower() in ("aucun", "aucune", "non", "off"):
            self.etude.groupe = None
            return [msg("Analyse de l'ensemble des données (plus de groupes).", BTN_MENU)]
        if not libres:
            etat = f"Analyse par groupe : {self.resume_groupe()}.\n\n" if self.etude.groupe else ""
            b, _ = self.boutons_colonnes("/groupe", "tout", supplements=[("Aucun groupe (ensemble des données)", "/groupe aucun")])
            return [msg(etat + "Quelle colonne désigne les groupes ? Chaque analyse sera lancée groupe par groupe (résultats et graphiques par groupe).", b, remplace=True)]
        col = self.colonne(libres[0], "tout")
        periode = None
        if d.types[col] == "date":
            choix = next((x.lower() for x in libres[1:] if x.lower() in ("jour", "mois", "annee", "année")), None)
            if choix is None:
                i = self.index(col)
                return [msg(f"« {col} » est une colonne de dates : regrouper par ?", [[("Jour", f"/groupe {i} jour"), ("Mois", f"/groupe {i} mois"), ("Année", f"/groupe {i} annee")]], remplace=True)]
            periode = "annee" if choix.startswith("ann") else choix
        tous = d.groupe_valeurs(col, periode)
        if not tous:
            raise st.ErreurStat(f"La colonne « {col} » ne contient aucune valeur.")
        if len(tous) > st.MAX_GROUPES:
            raise st.ErreurStat(f"La colonne « {col} » compte {len(tous)} groupes : le maximum est {st.MAX_GROUPES}.")
        retenus = tous
        if kv.get("valeurs"):
            retenus = [v for v in tous if v in {x.strip() for x in kv["valeurs"].split("|")}]
            if not retenus:
                raise st.ErreurStat("Aucun de ces groupes n'existe. Groupes disponibles : " + ", ".join(tous) + ".")
        self.etude.groupe = {"colonne": col, "periode": periode, "valeurs": retenus}
        return [msg(f"🗂 Analyse par groupe : {self.resume_groupe()}.\nToutes les analyses sont maintenant lancées groupe par groupe. "
                    f"Pour ne garder que certains groupes : /groupe {self.index(col)}{' ' + periode if periode else ''} valeurs=a|b. /groupe aucun pour revenir à l'ensemble.", BTN_MENU)]

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
        if att["type"] == "suite":
            self.attente = None
            return self._handle(att["prefixe"] + " " + texte.strip(), a_liberer)
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
