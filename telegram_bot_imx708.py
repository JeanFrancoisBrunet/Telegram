#!/usr/bin/env python3
# ===============================================================================
#  Bot Telegram – Caméra IMX708 (Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe)
#
#  Commandes disponibles :
#    /photo          – Capture une image avec l'IMX708 et l'envoie
#    /rafale <n>     – Capture n images en séquence rapide (maxi 10)
#    /video <sec>    – Capture une vidéo (maxi 120 s) et l'envoie
#    /liste          – 5 derniers fichiers cliquables (envoi + suppression)
#    /status         – Température CPU + espace disque
#    /motion         – Chgt mode Motion (fichier.mkv ou flux continu pour visio)
#    /aide           – Liste des commandes
#
#  Gestion automatique du service Motion :
#    Si Motion est actif lors d'une capture (photo/rafale/vidéo),
#    il est arrêté automatiquement avant la prise de vue, puis
#    relancé après. L'utilisateur est informé à chaque étape.
#
#  Prérequis :
#    pip install python-telegram-bot   (v20+)
#    rpicam-still et rpicam-vid doivent être disponibles sur le Pi
#    sudo visudo  :  jfbrunet ALL=(ALL) NOPASSWD: /usr/bin/systemctl * motion,
#                                                 /usr/bin/pkill -15 -x motion
#  Configuration :
#    Créer ~/.telegram_config avec :        (fichier caché)
#      [telegram]
#      token   = VOTRE_TOKEN
#      chat_id = VOTRE_CHAT_ID
#
#  Auteur  : Jean-François BRUNET – JFBConseils – Juin 2026
# ===============================================================================

import asyncio
import configparser
import datetime
import logging
import os
import shutil
import subprocess
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import sys
sys.path.insert(0, "/home/jfbrunet/Projects/Telegram")
from motion_mode_manager import set_mode, get_mode

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Configuration (lecture depuis ~/.telegram_config)
# ------------------------------------------------------------
def charger_config() -> tuple[str, int]:
    """Charge TOKEN et CHAT_ID depuis ~/.telegram_config"""
    cfg_path = os.path.expanduser("~/.telegram_config")
    cfg = configparser.ConfigParser()

    if not os.path.exists(cfg_path):
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {cfg_path}\n"
            "Créez-le avec :\n"
            "  [telegram]\n"
            "  token   = VOTRE_TOKEN\n"
            "  chat_id = VOTRE_CHAT_ID"
        )

    cfg.read(cfg_path)
    token   = cfg["telegram"]["token_imx708"].strip()
    chat_id = int(cfg["telegram"]["chat_id"].strip())
    return token, chat_id

TOKEN, CHAT_ID = charger_config()

# ------------------------------------------------------------
# Dossier de sauvegarde des images
# ------------------------------------------------------------
SAVE_DIR = os.path.expanduser("~/Projects/Telegram/images_bot")
os.makedirs(SAVE_DIR, exist_ok=True)

# Nombre de fichiers affichés par /liste
LISTE_MAX_FILES = 5

# ------------------------------------------------------------
# Gestion automatique du service Motion
# ------------------------------------------------------------
SYSTEMCTL         = "/usr/bin/systemctl"
MOTION_STOP_GRACE = 3   # secondes entre pkill et systemctl stop


class MotionGuard:
    """Arrête Motion avant une capture et le relance ensuite si nécessaire.
    Usage typique dans un handler :
        motion_actif, mode_avant = await asyncio.to_thread(MotionGuard.stop_if_running)
        # ... capture ...
        if motion_actif:
            await asyncio.to_thread(MotionGuard.start_with_mode, mode_avant)"""

    @staticmethod
    def is_running() -> bool:
        try:
            r = subprocess.run(
                [SYSTEMCTL, "is-active", "motion"],
                capture_output=True, text=True,
            )
            return r.stdout.strip() == "active"
        except Exception:
            return False

    @staticmethod
    def stop_if_running() -> tuple[bool, str | None]:
        """Arrête Motion si actif.

        Retourne (motion_était_actif, mode_avant_arrêt).
        Le mode ("stream" ou "record") est lu via motion_mode_manager avant
        l'arrêt afin de pouvoir le restaurer après la capture via start_with_mode().
        """
        if not MotionGuard.is_running():
            return False, None

        # Sauvegarder le mode courant avant l'arrêt
        mode_avant = None
        try:
            mode_avant = get_mode()
        except Exception as e:
            logger.warning(f"MotionGuard : impossible de lire le mode Motion : {e}")

        # SIGTERM propre avant systemctl stop
        try:
            subprocess.run(
                ["sudo", "pkill", "-15", "-x", "motion"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        time.sleep(MOTION_STOP_GRACE)
        try:
            subprocess.run(
                ["sudo", SYSTEMCTL, "stop", "motion"],
                capture_output=True, text=True, timeout=25,
            )
        except Exception:
            pass
        return True, mode_avant

    @staticmethod
    def start_with_mode(mode: str | None) -> tuple[bool, str]:
        """Relance Motion via systemctl start, puis restaure le mode dans
        motion.conf via motion_mode_manager si le mode est connu.
        Retourne (succès, message).
        """
        try:
            r = subprocess.run(
                ["sudo", SYSTEMCTL, "start", "motion"],
                capture_output=True, text=True, timeout=25,
            )
            started = r.returncode == 0
            msg     = (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return False, "Timeout lors du redémarrage de Motion."
        except Exception as e:
            return False, str(e)

        # Restaurer le mode dans motion.conf si connu
        if started and mode in ("stream", "record"):
            try:
                set_mode(mode, caller="telegram_bot_imx708")
                logger.info(f"MotionGuard : mode Motion restauré → '{mode}'")
            except Exception as e:
                logger.warning(
                    f"MotionGuard : impossible de restaurer le mode '{mode}' : {e}"
                )

        return started, msg

# ------------------------------------------------------------
# Lecture du mode Motion (enregistrement ou flux continu)
# ------------------------------------------------------------
def get_motion_mode() -> str:
    """Retourne le mode Motion via motion_mode_manager (source de vérité unique).
    Retourne '🎥 Enregistrement .mkv' ou '📡 Flux continu (Visio seule)'."""
    try:
        return "🎥 Enregistrement .mkv" if get_mode() == "record" else "📡 Flux continu (Visio)"
    except Exception:
        return "🎥 Enregistrement .mkv"  # défaut si manager inaccessible

def _set_motion_movie_output(activer: bool) -> bool:
    """Modifie le mode Motion via motion_mode_manager (verrou inter-processus).
    activer=True  → mode 'record'  (movie_output on)
    activer=False → mode 'stream'  (movie_output off)
    Retourne True si l'écriture a réussi."""
    try:
        return set_mode("record" if activer else "stream",
                        caller="telegram_bot_imx708")
    except Exception as e:
        logger.error(f"_set_motion_movie_output : {e}")
        return False

# ------------------------------------------------------------
# Décorateur de sécurité : filtre par CHAT_ID
# ------------------------------------------------------------
def acces_autorise(handler):
    """N'exécute la commande que si l'expéditeur est le CHAT_ID autorisé."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            logger.warning(
                f"Accès refusé pour chat_id={update.effective_chat.id}"
            )
            await update.message.reply_text("⛔ Accès non autorisé.")
            return
        await handler(update, context)
    return wrapper

# ------------------------------------------------------------
# Utilitaires système
# ------------------------------------------------------------
def get_cpu_temp() -> str:
    """Retourne la température CPU sous forme de chaîne."""
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        try:
            with open(p) as f:
                temp = int(f.read().strip()) / 1000.0
                return f"{temp:.1f} °C"
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=2
        )
        return r.stdout.strip().replace("temp=", "").replace("'C", " °C")
    except Exception:
        return "N/A"

def get_espace_disque() -> str:
    """Retourne l'espace disque disponible sur /home."""
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        libre = usage.free / 1024**3
        total = usage.total / 1024**3
        return f"{libre:.1f} Go free / {total:.1f} Go"
    except Exception:
        return "N/A"

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} o"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} Ko"
    else:
        return f"{size_bytes / 1024**2:.1f} Mo"

def _list_recent_files(n: int = LISTE_MAX_FILES) -> list[str]:
    """Retourne les n fichiers image/vidéo les plus récents de SAVE_DIR."""
    extensions = (".jpg", ".jpeg", ".png", ".mp4", ".h264")
    try:
        tous = [
            f for f in os.listdir(SAVE_DIR)
            if os.path.splitext(f)[1].lower() in extensions
        ]
        tous.sort(
            key=lambda f: os.path.getmtime(os.path.join(SAVE_DIR, f)),
            reverse=True,
        )
        return tous[:n]
    except Exception:
        return []

# ------------------------------------------------------------
# Capture photo
# ------------------------------------------------------------
def capturer_imx708(fichier: str) -> bool:
    """Déclenche rpicam-still sur la caméra 0 (IMX708).
    Retourne True si la capture a réussi."""
    cmd = [
        "rpicam-still",
        "--camera", "0",
        "--output", fichier,
        "--timeout", "3000",
        "--quality", "90",
        "--immediate",
        "--nopreview",
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"

    try:
        result = subprocess.run(
            cmd,
            timeout=30,
            capture_output=True,
            text=True,
            env=env
        )
        if result.returncode != 0:
            logger.error(f"rpicam-still erreur : {result.stderr}")
            return False
        return os.path.exists(fichier) and os.path.getsize(fichier) > 1000
    except subprocess.TimeoutExpired:
        logger.error("rpicam-still : timeout dépassé")
        return False
    except Exception as e:
        logger.error(f"rpicam-still : exception {e}")
        return False

# ------------------------------------------------------------
# Capture vidéo
# ------------------------------------------------------------
def capturer_video_imx708(fichier: str, duree_sec: int) -> bool:
    """Déclenche rpicam-vid sur la caméra 0 (IMX708).
    duree_sec : durée en secondes (1–120).
    Retourne True si la capture a réussi."""
    cmd = [
        "rpicam-vid",
        "--camera", "0",
        "--output", fichier,
        "--timeout", str(duree_sec * 1000),
        "--width",  "1920",
        "--height", "1080",
        "--framerate", "30",
        "--nopreview",
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"

    try:
        result = subprocess.run(
            cmd,
            timeout=duree_sec + 15,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            logger.error(f"rpicam-vid erreur : {result.stderr}")
            return False
        return os.path.exists(fichier) and os.path.getsize(fichier) > 1000
    except subprocess.TimeoutExpired:
        logger.error("rpicam-vid : timeout dépassé")
        return False
    except Exception as e:
        logger.error(f"rpicam-vid : exception {e}")
        return False

# ------------------------------------------------------------
# Helpers Motion — messages d'état pour les handlers
# ------------------------------------------------------------
async def _stop_motion_si_actif(update: Update) -> tuple[bool, str | None]:
    """Arrête Motion en tâche de fond si nécessaire.
    Informe l'utilisateur et retourne (motion_était_actif, mode_avant_arrêt)
    afin que le handler puisse restaurer le mode via _relancer_motion()."""
    motion_actif, mode_avant = await asyncio.to_thread(MotionGuard.stop_if_running)
    if motion_actif:
        await update.message.reply_text(
            "⏸ *Motion détecté — arrêt temporaire pour libérer la caméra…*",
            parse_mode="Markdown",
        )
    return motion_actif, mode_avant

async def _relancer_motion(update: Update, mode_avant: str | None = None):
    """Relance Motion avec restauration du mode d'avant la capture
    (via motion_mode_manager) et informe l'utilisateur du résultat."""
    ok, msg = await asyncio.to_thread(MotionGuard.start_with_mode, mode_avant)
    if ok:
        await update.message.reply_text(
            "▶️ *Motion relancé automatiquement.*",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"⚠️ *Impossible de relancer Motion :* `{msg}`\n"
            "Relancez-le manuellement via le bot Motion.",
            parse_mode="Markdown",
        )

# ------------------------------------------------------------
# Handlers des commandes
# ------------------------------------------------------------
@acces_autorise
async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/photo – Capture une image et l'envoie dans le chat."""
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)

    await update.message.reply_text("📷 Capture en cours…")

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx708_{horodatage}.jpg")

    succes = await asyncio.to_thread(capturer_imx708, fichier)

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "❌ La capture a échoué. Vérifiez que l'IMX708 est connectée."
        )
        return

    legende = (
        f"📸 IMX708 – {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
    )

    with open(fichier, "rb") as photo:
        await update.message.reply_photo(photo=photo, caption=legende)

    logger.info(f"Image envoyée : {fichier}")

@acces_autorise
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status – Affiche la température CPU, l'espace disque et l'état Motion."""
    temp   = get_cpu_temp()
    disque = get_espace_disque()
    heure  = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    running     = MotionGuard.is_running()
    motion_etat = "🟢 actif" if running else "🔴 arrêté"
    motion_mode = get_motion_mode() if running else "—"

    texte = (
        f"🖥 *Statut Raspberry Pi 5*\n"
        f"──────────────────────\n"
        f"🕐 {heure}\n\n"
        f"🌡 CPU : `{temp}`\n"
        f"💾 : `{disque}`\n"
        f"📁 : `{SAVE_DIR}`\n"
        f"🎥 Motion : {motion_etat}\n"
        f"   ↳ Mode : {motion_mode}"
    )
    await update.message.reply_text(texte, parse_mode="Markdown")

@acces_autorise
async def cmd_motion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/motion – Affiche et permet de changer le mode Motion (enregistrement / flux)."""
    running     = MotionGuard.is_running()
    motion_etat = "🟢 actif" if running else "🔴 arrêté"
    mode_actuel = get_motion_mode()

    texte = (
        f"🎥 *Mode Motion*\n"
        f"──────────────────────\n"
        f"Statut : {motion_etat}\n"
        f"Mode actuel : {mode_actuel}\n\n"
        f"Choisissez le nouveau mode :\n"
        f"_(Motion redémarrera automatiquement s'il est actif)_"
    )
    keyboard = [[
        InlineKeyboardButton(
            "🎥  Fichier.mkv",
            callback_data="motion_mode:enregistrement"),
        InlineKeyboardButton(
            "📡  Flux continu",
            callback_data="motion_mode:flux"),
    ]]
    await update.message.reply_text(
        texte,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

@acces_autorise
async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/aide – Liste des commandes disponibles."""
    now = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    running     = MotionGuard.is_running()
    motion_etat = "🟢" if running else "🔴"
    mode_actuel = get_motion_mode() if running else "—"
    texte = (
        f"🤖 *Bot Caméra IMX708 — Cdes*\n"
        f"───────────────────────\n\n"
        f"🎥 Motion : {motion_etat}  ↳ {mode_actuel}\n\n"
        "📷 /photo — 1 photo\n"
        "🔁 /rafale <n> — jusqu'à 10 photos\n"
        "🎬 /video <sec> — jusqu'à 120 sec\n"
        "📂 /liste — 5 derniers fichiers\n"
        "📊 /status — Température + Disque\n"
        "🎥 /motion — Changer le mode Motion\n"
        "❓ /aide — Ce menu\n\n"
        "ℹ️ résolution 4608 x 2592 ~ 1.5 Mo\n"
    )
    await update.message.reply_text(texte, parse_mode="Markdown")

@acces_autorise
async def cmd_rafale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rafale <n> – Capture n images en séquence rapide (maxi 10)."""
    try:
        n = int(context.args[0]) if context.args else 3
    except (ValueError, IndexError):
        await update.message.reply_text(
            "⚠️ Usage : /rafale <nombre>  (ex. /rafale 5)"
        )
        return

    n = max(1, min(n, 10))

    motion_was_running, mode_avant = await _stop_motion_si_actif(update)

    await update.message.reply_text(f"🔁 Rafale de {n} image(s) en cours…")

    horodatage_base = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichiers_ok = []

    for i in range(1, n + 1):
        fichier = os.path.join(SAVE_DIR, f"imx708_rafale_{horodatage_base}_{i:02d}.jpg")
        succes = await asyncio.to_thread(capturer_imx708, fichier)
        if succes:
            fichiers_ok.append(fichier)
            logger.info(f"Rafale {i}/{n} : {fichier}")
        else:
            logger.warning(f"Rafale {i}/{n} : échec capture")

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not fichiers_ok:
        await update.message.reply_text("❌ Aucune image capturée.")
        return

    from telegram import InputMediaPhoto
    media = []
    for idx, f in enumerate(fichiers_ok):
        with open(f, "rb") as fp:
            data = fp.read()
        legende = f"🔁 Rafale {idx + 1}/{len(fichiers_ok)} – {os.path.basename(f)}" if idx == 0 else ""
        media.append(InputMediaPhoto(media=data, caption=legende))

    await update.message.reply_media_group(media=media)
    logger.info(f"Rafale envoyée : {len(fichiers_ok)}/{n} images")

@acces_autorise
async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/video <sec> – Capture une vidéo et l'envoie (maxi 120 s)."""
    try:
        duree = int(context.args[0]) if context.args else 10
    except (ValueError, IndexError):
        await update.message.reply_text(
            "⚠️ Usage : /video <secondes>  (ex. /video 30)"
        )
        return

    duree = max(1, min(duree, 120))

    motion_was_running, mode_avant = await _stop_motion_si_actif(update)

    await update.message.reply_text(
        f"🎬 Enregistrement vidéo de {duree} s en cours… merci de patienter."
    )

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx708_video_{horodatage}.mp4")

    succes = await asyncio.to_thread(capturer_video_imx708, fichier, duree)

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "❌ La capture vidéo a échoué. Vérifiez que l'IMX708 est connectée."
        )
        return

    taille_mo = os.path.getsize(fichier) / 1024**2
    legende = (
        f"🎬 Vidéo {duree} s – IMX708\n"
        f"📅 {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"💾 {taille_mo:.1f} Mo"
    )

    if taille_mo > 50:
        await update.message.reply_text(
            f"⚠️ Vidéo trop lourde pour Telegram ({taille_mo:.1f} Mo > 50 Mo).\n"
            f"Fichier conservé sur le Pi : {fichier}"
        )
        return

    with open(fichier, "rb") as vid:
        await update.message.reply_video(
            video=vid,
            caption=legende,
            supports_streaming=True,
        )
    logger.info(f"Vidéo envoyée : {fichier}  ({taille_mo:.1f} Mo)")

# ------------------------------------------------------------
# /liste — boutons cliquables : envoi ou suppression
# ------------------------------------------------------------
@acces_autorise
async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/liste – Affiche les 5 derniers fichiers avec boutons Envoyer / Supprimer."""
    fichiers = _list_recent_files(LISTE_MAX_FILES)

    if not fichiers:
        await update.message.reply_text("📂 Aucun fichier trouvé dans le dossier.")
        return

    # En-tête
    await update.message.reply_text(
        f"📂 *{LISTE_MAX_FILES} derniers fichiers — `{SAVE_DIR}`*\n\n"
        "Choisissez une action par fichier :",
        parse_mode="Markdown",
    )

    # Un message inline par fichier avec 2 boutons
    for nom in fichiers:
        chemin = os.path.join(SAVE_DIR, nom)
        try:
            mtime  = os.path.getmtime(chemin)
            date   = datetime.datetime.fromtimestamp(mtime).strftime("%d/%m %H:%M")
            taille = _format_size(os.path.getsize(chemin))
            ext    = os.path.splitext(nom)[1].lower()
            icone  = "🎬" if ext in (".mp4", ".h264") else "📷"
            label  = f"{icone} `{nom}`\n📅 {date}  💾 {taille}"
        except Exception:
            label = f"• `{nom}`"

        keyboard = [[
            InlineKeyboardButton("📤 Envoyer",    callback_data=f"cam_send:{nom}"),
            InlineKeyboardButton("🗑 Supprimer", callback_data=f"cam_del:{nom}"),
        ]]
        await update.message.reply_text(
            label,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

# ------------------------------------------------------------
# Callbacks inline (/liste : envoi et suppression)
# ------------------------------------------------------------
async def callback_dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # --- Envoi d'un fichier ---
    if data.startswith("cam_send:"):
        nom       = data[len("cam_send:"):]
        chemin    = os.path.join(SAVE_DIR, nom)

        if not os.path.exists(chemin):
            await query.answer("❌ Fichier introuvable (déjà supprimé ?).", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return

        taille_mo = os.path.getsize(chemin) / 1024**2
        ext = os.path.splitext(nom)[1].lower()
        mtime = os.path.getmtime(chemin)
        legende = (
            f"📁 `{nom}`\n"
            f"📅 {datetime.datetime.fromtimestamp(mtime).strftime('%d/%m/%Y %H:%M:%S')}\n"
            f"💾 {_format_size(os.path.getsize(chemin))}"
        )

        try:
            with open(chemin, "rb") as f:
                if ext in (".jpg", ".jpeg", ".png"):
                    await query.message.reply_photo(photo=f, caption=legende, parse_mode="Markdown")
                elif taille_mo > 50:
                    await query.message.reply_text(
                        f"⚠️ Fichier trop lourd pour Telegram ({taille_mo:.1f} Mo > 50 Mo).\n"
                        f"Accessible sur le Pi : `{chemin}`",
                        parse_mode="Markdown",
                    )
                else:
                    await query.message.reply_video(video=f, caption=legende, parse_mode="Markdown")
            logger.info(f"Fichier envoyé via /liste : {nom}")
        except Exception as exc:
            logger.warning(f"Erreur envoi {nom} : {exc}")
            await query.message.reply_text(
                f"❌ Impossible d'envoyer `{nom}` : {exc}", parse_mode="Markdown"
            )
        return

    # --- Suppression d'un fichier ---
    if data.startswith("cam_del:"):
        nom    = data[len("cam_del:"):]
        chemin = os.path.join(SAVE_DIR, nom)

        if not os.path.exists(chemin):
            await query.answer("❌ Fichier déjà supprimé.", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return

        # Demande de confirmation
        keyboard = [[
            InlineKeyboardButton("✅ Suppression", callback_data=f"cam_del_ok:{nom}"),
            InlineKeyboardButton("❌ Annuler",               callback_data=f"cam_del_cancel:{nom}"),
        ]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- Confirmation suppression ---
    if data.startswith("cam_del_ok:"):
        nom    = data[len("cam_del_ok:"):]
        chemin = os.path.join(SAVE_DIR, nom)
        try:
            os.unlink(chemin)
            await query.edit_message_text(f"🗑 `{nom}` supprimé.", parse_mode="Markdown")
            logger.info(f"Fichier supprimé via /liste : {nom}")
        except FileNotFoundError:
            await query.edit_message_text(f"⚠️ `{nom}` déjà supprimé.", parse_mode="Markdown")
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Impossible de supprimer `{nom}` : {exc}", parse_mode="Markdown"
            )
        return

    # --- Annulation suppression ---
    if data.startswith("cam_del_cancel:"):
        nom = data[len("cam_del_cancel:"):]
        # Rétablit les boutons d'origine
        keyboard = [[
            InlineKeyboardButton("📤 Envoyer",    callback_data=f"cam_send:{nom}"),
            InlineKeyboardButton("🗑 Supprimer", callback_data=f"cam_del:{nom}"),
        ]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- Changement de mode Motion ---
    if data.startswith("motion_mode:"):
        nouveau_mode = data[len("motion_mode:"):]
        activer_enreg = (nouveau_mode == "enregistrement")

        # Écrire dans motion.conf
        ok = await asyncio.to_thread(_set_motion_movie_output, activer_enreg)
        if not ok:
            await query.edit_message_text(
                "❌ Impossible de modifier motion.conf — vérifiez les permissions sudo."
            )
            return

        mode_txt = ("🎥 Enregistrement .mkv"
                    if activer_enreg else "📡 Flux continu (Visio)")

        # Redémarrer Motion si actif
        running = await asyncio.to_thread(MotionGuard.is_running)
        if running:
            await query.edit_message_text(
                f"⏳ Mode → *{mode_txt}*\nRedémarrage de Motion en cours…",
                parse_mode="Markdown",
            )
            _, mode_avant = await asyncio.to_thread(MotionGuard.stop_if_running)
            ok2, _ = await asyncio.to_thread(MotionGuard.start_with_mode, mode_avant)
            etat = "🟢 actif" if ok2 else "🔴 erreur au redémarrage"
        else:
            etat = "🔴 arrêté (le mode sera appliqué au prochain démarrage)"

        await query.edit_message_text(
            f"✅ *Mode Motion mis à jour*\n"
            f"Mode : {mode_txt}\n"
            f"Motion : {etat}",
            parse_mode="Markdown",
        )
        return

    logger.warning(f"callback_dispatch : data inconnu reçu : '{data}'")

# ------------------------------------------------------------
# Attente synchronisation NTP
# ------------------------------------------------------------
async def _attendre_ntp(timeout: int = 60) -> bool:
    """Attend que l'horloge système soit synchronisée NTP.
    Retourne True si synchro obtenue avant timeout (secondes)."""
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
        await asyncio.sleep(1)
    return False

# ------------------------------------------------------------
# Message automatique au démarrage
# ------------------------------------------------------------
async def message_demarrage(application) -> None:
    """Envoyée une seule fois au CHAT_ID dès que le bot est opérationnel."""
    ntp_ok      = await _attendre_ntp(timeout=60)
    heure       = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    running     = MotionGuard.is_running()
    motion_etat = "🟢 actif" if running else "🔴 arrêté"
    motion_mode = get_motion_mode() if running else "—"
    ntp_avert   = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
    await application.bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"🤖 *Bot Caméra IMX708 démarré*\n"
            f"📅 {heure}{ntp_avert}\n\n"
            f"🎥 Motion : {motion_etat}\n"
            f"   ↳ Mode : {motion_mode}\n\n"
            f"Tapez /aide pour les commandes."
        ),
        parse_mode="Markdown",
    )
    logger.info(f"Message de démarrage envoyé (NTP OK : {ntp_ok}).")

# ------------------------------------------------------------
# Point d'entrée
# ------------------------------------------------------------
def main():
    logger.info("Démarrage du bot Telegram IMX708…")
    logger.info(f"CHAT_ID autorisé : {CHAT_ID}")
    logger.info(f"Dossier images   : {SAVE_DIR}")

    app = ApplicationBuilder().token(TOKEN).post_init(message_demarrage).build()

    app.add_handler(CommandHandler("photo",  cmd_photo))
    app.add_handler(CommandHandler("rafale", cmd_rafale))
    app.add_handler(CommandHandler("video",  cmd_video))
    app.add_handler(CommandHandler("liste",  cmd_liste))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("motion", cmd_motion))
    app.add_handler(CommandHandler("aide",   cmd_aide))
    app.add_handler(CommandHandler("start",  cmd_aide))
    app.add_handler(CallbackQueryHandler(callback_dispatch))

    logger.info("Bot en écoute… (Ctrl+C pour arrêter)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
