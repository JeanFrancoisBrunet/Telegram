#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Bot Telegram « Problem Solving » — interface Telegram du coach 8D / A3 / PDCA / K6pts.
#  Importe problem_solving.py (même logique que le terminal, sessions séparées par utilisateur).
#  - Texte : réponses aux étapes (elles s'additionnent), questions au coach, commandes
#  - Boutons : validation d'étape, choix du moteur et des modèles (/modele), projets
#    (/projets), titre… Un bouton = une commande (voir problem_solving.msg)
#  - Photo (ou image envoyée comme fichier) : analysée au regard de l'étape en cours ;
#    la légende de la photo sert de question
#  - Autre fichier (PDF, texte, tableur…) : joint à l'étape comme preuve, versé au rapport
#  - Projets enregistrés à chaque message et rechargeables (/projets, /charger) avec tout
#    l'historique des échanges ; au redémarrage du bot, le projet en cours est repris
#  - Le rapport (simple ou complet avec les échanges) est renvoyé dans la conversation
#
#  Configuration (aucun secret dans ce fichier) :
#    Token   : variable TELEGRAM_TOKEN, sinon ~/.telegram_config  [telegram] token_problemsolving
#    Accès   : ALLOWED_IDS (variable ou .env, séparés par des virgules) ou
#              ~/.telegram_config  [telegram] allowed_ids ; le chat_id du fichier est toujours
#              autorisé. Si aucun identifiant n'est connu, le bot est ouvert à tous (déconseillé).
#    Un utilisateur refusé reçoit son identifiant Telegram, à ajouter à ALLOWED_IDS.
#  Dépendance : requests
#  Lancement  : python3 telegram_bot_problemsolving.py
#  Service    : voir la fiche « Bot Telegram » (systemd, ...)
#  Auteur : Jean-François BRUNET – JFBConseils – Octobre 2026
#  Rev.   : 
# =============================================================================

import os
import sys
import json
import time
import threading
import subprocess
import configparser
from datetime import datetime
from pathlib import Path

import requests

try:
    import problem_solving as ps
except ImportError:
    sys.path.insert(0, str(Path.home() / "Projects" / "Problem_Solving"))
    import problem_solving as ps

COMMANDES_MENU = [
    ("8d", "Démarche 8D"),
    ("a3", "A3"),
    ("pdca", "PDCA"),
    ("k6pts", "Kaizen 6 points"),
    ("ok", "Valider l'étape"),
    ("etat", "Où en est-on ?"),
    ("guide", "Rappel de l'étape (attendu, points, outils)"),
    ("sauter", "Sauter la question en cours"),
    ("prec", "Question précédente"),
    ("reponse", "Relire mes réponses de l'étape"),
    ("modele", "Moteur, modèles IA et mode (boutons)"),
    ("mode", "Mode : guidé, rapide ou libre"),
    ("projets", "Mes projets : ré-ouvrir, supprimer"),
    ("sauver", "Enregistrer le projet (+ titre)"),
    ("titre", "Définir ou modifier le titre"),
    ("historique", "Relire les échanges de l'étape"),
    ("rapport", "Générer le rapport (complet avec échanges)"),
    ("retour", "Revenir à l'étape précédente"),
    ("passer", "Passer l'étape"),
    ("stop", "Mettre le projet de côté"),
    ("aide", "Aide"),
]

# Boutons qui modifient l'état : le clavier du message est retiré après le clic (pas de double clic)
COMMANDES_A_USAGE_UNIQUE = {"/ok", "/passer", "/retour", "/stop", "/nouveau", "/charger",
                            "/supprimer", "/8d", "/a3", "/pdca", "/k6pts", "/sauter", "/prec"}

def lire_config_telegram():
    cp = configparser.RawConfigParser()
    try:
        cp.read(Path.home() / ".telegram_config", encoding="utf-8")
    except Exception:
        pass
    return cp["telegram"] if cp.has_section("telegram") else {}

def attendre_ntp(timeout=60):
    """Attend (60 s max) que l'horloge du Pi soit synchronisée NTP, comme le bot Groq."""
    for _ in range(timeout):
        try:
            r = subprocess.run(
                ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=3,
            )
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
        rang = [{"text": lib, "callback_data": cmd} for lib, cmd in ligne
                if len(cmd.encode("utf-8")) <= 64]
        if rang:
            lignes.append(rang)
    return json.dumps({"inline_keyboard": lignes})

class Attente:
    """Message « calcul en cours » : affiché seulement si le travail dépasse un court délai,
    puis supprimé dès que le résultat est prêt (les actions instantanées n'affichent rien)."""
    def __init__(self, bot, chat_id, delai=2.0, texte="⏳ Calcul en cours…"):
        self.bot, self.chat_id, self.texte = bot, chat_id, texte
        self.verrou = threading.Lock()
        self.msg_id = None
        self.fini = False
        self.minuteur = threading.Timer(delai, self._afficher)
        self.minuteur.daemon = True
        self.minuteur.start()

    def _afficher(self):
        with self.verrou:
            if self.fini:
                return
            try:
                rep = self.bot.api("sendMessage", chat_id=self.chat_id, text=self.texte)
                if rep.get("ok"):
                    self.msg_id = rep["result"]["message_id"]
            except Exception:
                pass

    def terminer(self):
        """Annule le minuteur et supprime le message d'attente s'il a été envoyé (appel répétable)."""
        self.minuteur.cancel()
        with self.verrou:
            self.fini = True
            if self.msg_id:
                try:
                    self.bot.api("deleteMessage", chat_id=self.chat_id, message_id=self.msg_id)
                except Exception:
                    pass
                self.msg_id = None


class Bot:
    def __init__(self):
        sec = lire_config_telegram()
        self.token = os.getenv("TELEGRAM_TOKEN") or sec.get("token_problemsolving", "")
        if not self.token:
            sys.exit("Token introuvable : TELEGRAM_TOKEN ou token_problemsolving dans "
                     "~/.telegram_config")
        brut = os.getenv("ALLOWED_IDS") or sec.get("allowed_ids", "")
        self.autorises = {x.strip() for x in brut.split(",") if x.strip()}
        self.proprio = sec.get("chat_id", "").strip()          # reçoit le message de démarrage
        if self.proprio:
            self.autorises.add(self.proprio)
        self.url = f"https://api.telegram.org/bot{self.token}"
        self.http = requests.Session()                         # connexion réutilisée entre les appels
        self.sessions = {}
        self.verrous = {}
        self.global_lock = threading.Lock()

    # -- utilitaires
    def masquer(self, texte):
        """Retire le token de tout texte destiné aux logs ou au chat."""
        return str(texte).replace(self.token, "***")

    def api(self, methode, **params):
        """Appel à l'API Telegram : connexion réutilisée, délais courts, un seul nouvel essai
        (échec de connexion ou 502/503/504), journal des appels lents (plus de 3 s)."""
        delai = 70 if methode == "getUpdates" else (5, 30)
        debut = time.monotonic()
        for essai in (1, 2):
            try:
                r = self.http.post(f"{self.url}/{methode}", data=params, timeout=delai)
            except requests.exceptions.ConnectTimeout:
                if essai == 2:
                    raise
                time.sleep(1)
                continue
            try:
                rep = r.json()
            except ValueError:                                 # réponse non JSON (page d'erreur d'une passerelle)
                rep = {"ok": False, "error_code": r.status_code, "description": f"réponse illisible (HTTP {r.status_code})"}
            if essai == 1 and methode != "getUpdates" and not rep.get("ok") and rep.get("error_code") in (502, 503, 504):
                time.sleep(1)
                continue
            break
        duree = time.monotonic() - debut
        if duree > 3 and methode != "getUpdates":
            print(f"[API] {methode} : {duree:.1f} s", flush=True)   # visible dans journalctl
        return rep

    def envoyer(self, chat_id, m):
        """Envoie un message (str ou dict avec boutons) ; les boutons vont sur le dernier morceau."""
        texte, boutons = ps.texte_de(m), ps.boutons_de(m)
        morceaux = [texte[i:i + 3900] for i in range(0, len(texte), 3900)] or ["…"]
        for i, morceau in enumerate(morceaux):
            params = {}
            if boutons and i == len(morceaux) - 1:
                params["reply_markup"] = clavier(boutons)
            self.api("sendMessage", chat_id=chat_id, text=morceau, **params)

    def editer(self, chat_id, message_id, m):
        """Remplace un message existant (menus). Retombe sur un nouvel envoi si l'édition échoue."""
        texte, boutons = ps.texte_de(m), ps.boutons_de(m)
        params = {"reply_markup": clavier(boutons)} if boutons else {}
        rep = self.api("editMessageText", chat_id=chat_id, message_id=message_id,
                       text=texte[:4000], **params)
        if not rep.get("ok") and "not modified" not in str(rep.get("description", "")):
            self.envoyer(chat_id, m)

    def envoyer_fichier(self, chat_id, chemin):
        with open(chemin, "rb") as f:
            requests.post(f"{self.url}/sendDocument", data={"chat_id": chat_id},
                          files={"document": (Path(chemin).name, f)}, timeout=60)

    def telecharger(self, file_id):
        info = self.api("getFile", file_id=file_id)
        if not info.get("ok"):
            raise RuntimeError("Telegram refuse le téléchargement du fichier")
        r = requests.get(f"https://api.telegram.org/file/bot{self.token}/"
                         f"{info['result']['file_path']}", timeout=60)
        r.raise_for_status()
        return r.content

    def _saisie(self, chat_id, arret):
        """Maintient l'indicateur « en train d'écrire » pendant les appels LLM longs."""
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
                self.sessions[cle] = ps.Session(cle)
                self.verrous[cle] = threading.Lock()
            return self.sessions[cle], self.verrous[cle]

    # -- exécution d'un travail pour un utilisateur (un seul à la fois par conversation)
    def _executer(self, chat_id, travail, bouton=None, retirer_clavier=False):
        """Exécute travail(session, attente). Un message, une photo ou un fichier attend son tour ;
        un clic de bouton est ignoré si une réponse est déjà en cours de préparation (évite les doublons).
        bouton : None, ou {"id": identifiant du clic, "message_id": message portant le clavier}.
        retirer_clavier : retire le clavier du message cliqué dès la prise en compte."""
        session, verrou = self.session(chat_id)
        if bouton is None:
            verrou.acquire()
        elif not verrou.acquire(blocking=False):
            try:
                self.api("answerCallbackQuery", callback_query_id=bouton["id"],
                         text="⏳ Une réponse est déjà en cours, merci de patienter.")
            except Exception:
                pass
            return
        attente = None
        arret = threading.Event()
        try:
            if bouton is not None:
                try:                                           # accusé de réception immédiat (petit message en haut de l'écran)
                    self.api("answerCallbackQuery", callback_query_id=bouton["id"], text="⏳ Pris en compte…")
                except Exception:
                    pass
                if retirer_clavier and bouton.get("message_id"):
                    try:
                        self.api("editMessageReplyMarkup", chat_id=chat_id, message_id=bouton["message_id"],
                                 reply_markup=json.dumps({"inline_keyboard": []}))
                    except Exception:
                        pass
            attente = Attente(self, chat_id, texte="⏳ Traitement en cours…")
            threading.Thread(target=self._saisie, args=(chat_id, arret), daemon=True).start()
            try:
                travail(session, attente)
            except Exception as e:
                try:
                    attente.terminer()
                    self.envoyer(chat_id, self.masquer(f"⚠️ Erreur interne : {type(e).__name__}: {e}"))
                except Exception:
                    pass
        finally:
            arret.set()
            if attente is not None:
                attente.terminer()
            verrou.release()

    def _livrer(self, chat_id, msgs, fichier, attente=None):
        if attente is not None:
            attente.terminer()                                 # le message d'attente disparaît avant l'arrivée du résultat
        for m in msgs:
            self.envoyer(chat_id, m)
        if fichier:
            self.envoyer_fichier(chat_id, fichier)

    # -- message texte ou photo
    def traiter(self, chat_id, msg):
        def travail(session, attente):
            fichier_id = None
            if msg.get("photo"):
                fichier_id = msg["photo"][-1]["file_id"]          # la plus grande définition
            elif (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
                fichier_id = msg["document"]["file_id"]

            if fichier_id:
                if session.backend == "ollama":
                    self.envoyer(chat_id, "⏳ Analyse locale sur le Pi5 en cours "
                                          "(jusqu'à quelques minutes)…")
                donnees = self.telecharger(fichier_id)
                msgs = session.analyser_image(donnees, (msg.get("caption") or "").strip())
                self._livrer(chat_id, msgs, None, attente)
            elif msg.get("text"):
                msgs, fichier = session.handle(msg["text"])
                self._livrer(chat_id, msgs, fichier, attente)
            elif msg.get("document"):                             # autre fichier : pièce jointe (preuve)
                doc = msg["document"]
                if (doc.get("file_size") or 0) > 20 * 1024 * 1024:
                    self.envoyer(chat_id, "⚠️ Fichier trop volumineux : 20 Mo au maximum.")
                else:
                    donnees = self.telecharger(doc["file_id"])
                    msgs = session.joindre_piece(donnees, doc.get("file_name") or "document",
                                                 (msg.get("caption") or "").strip())
                    self._livrer(chat_id, msgs, None, attente)
        self._executer(chat_id, travail)

    # -- clic sur un bouton : la commande du bouton est exécutée comme si elle était tapée
    def traiter_bouton(self, chat_id, cb):
        data = (cb.get("data") or "").strip()
        message_id = (cb.get("message") or {}).get("message_id")
        if not data.startswith("/"):
            try:
                self.api("answerCallbackQuery", callback_query_id=cb["id"])
            except Exception:
                pass
            return

        def travail(session, attente):
            msgs, fichier = session.handle(data)
            premier = msgs[0] if msgs else None
            if isinstance(premier, dict) and premier.get("remplace") and message_id:
                attente.terminer()
                self.editer(chat_id, message_id, premier)         # menu mis à jour sur place
                msgs = msgs[1:]
            self._livrer(chat_id, msgs, fichier, attente)
        # accusé de réception du clic, refus si une réponse est en cours, clavier retiré pour les commandes à usage unique
        self._executer(chat_id, travail, bouton={"id": cb["id"], "message_id": message_id},
                       retirer_clavier=data.split()[0] in COMMANDES_A_USAGE_UNIQUE)

    # -- message de démarrage
    def message_demarrage(self):
        session, _ = self.session(self.proprio)
        ntp_ok = attendre_ntp(timeout=60)
        ntp_avert = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
        b = session.backend
        texte = (f"🧩 Bot Problem Solving démarré\n"
                 f"📅 {datetime.now():%d/%m/%Y %H:%M:%S}{ntp_avert}\n\n"
                 f"📡 Moteur : {b}\n"
                 f"🧠 Texte : {session.modele('texte')}\n"
                 f"📷 Photos : {session.modele('vision')}\n"
                 f"🧭 Mode : {session.mode}\n")
        if session.methode:
            avance = ("terminé" if session.fini else f"étape {session.idx + 1}/{len(session.etapes)}")
            texte += (f"\n📂 Projet en cours : « {session.titre} » "
                      f"({ps.METHODES[session.methode]['nom']}, {avance})\n")
            boutons = [[("📊 État", "/etat"), ("📜 Historique", "/historique")],
                       [("📂 Projets", "/projets"), ("🤖 Modèle", "/modele")]]
        else:
            texte += "\n/8d · /a3 · /pdca · /k6pts pour commencer, /projets pour ré-ouvrir un projet.\n"
            boutons = ps.BTN_DEMARRER
        self.envoyer(self.proprio, ps.msg(texte, boutons))

    # -- boucle principale
    def run(self):
        try:
            self.api("setMyCommands", commands=json.dumps(
                [{"command": c, "description": d} for c, d in COMMANDES_MENU]))
        except Exception as e:
            print(f"[menu] {self.masquer(e)}")
        if not self.autorises:
            print("⚠️ Aucun identifiant autorisé : le bot est ouvert à tous (consomme votre quota).")
        print(f"Bot Problem Solving démarré — moteur par défaut : {ps.CFG['BACKEND']} "
              f"— {len(self.autorises)} identifiant(s) autorisé(s). Ctrl+C pour arrêter.")
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
                    print(f"[polling] Telegram refuse : {data.get('error_code')} "
                          f"{data.get('description')}")
                    time.sleep(10)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    cb = upd.get("callback_query")
                    msg = upd.get("message") or {}
                    chat_id = ((cb or {}).get("message") or msg).get("chat", {}).get("id")
                    if not chat_id:
                        continue
                    if self.autorises and str(chat_id) not in self.autorises:
                        self.envoyer(chat_id, f"Accès non autorisé (votre identifiant : {chat_id}).")
                        continue
                    if cb:
                        threading.Thread(target=self.traiter_bouton, args=(chat_id, cb),
                                         daemon=True).start()
                    else:
                        threading.Thread(target=self.traiter, args=(chat_id, msg),
                                         daemon=True).start()
            except KeyboardInterrupt:
                break
            except requests.exceptions.ReadTimeout:
                pass                                           # attente longue expirée : normal, on repart
            except Exception as e:
                print(f"[polling] {type(e).__name__}: {self.masquer(e)}")
                time.sleep(5)


if __name__ == "__main__":
    Bot().run()
