#!/usr/bin/env python3
# ===============================================================================
#  Bot Telegram – Caméra IMX500 (Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe)
#
#  Commandes disponibles :
#    /photo              – Capture une image et l'envoie
#    /rafale <n>         – Capture n images en séquence rapide (maxi 10)
#    /video <sec>        – Capture et envoi d'une vidéo (maxi 120 s)
#    /ia_objets          – Photo unique annotée détection d'objets (MobileNet SSD)
#    /ia_pose            – Photo unique annotée détection de pose (PoseNet)
#    /ia_continu <n> <s> – n photos IA espacées de s secondes, envoyées au fil de l'eau
#    /ia_video <sec>     – Vidéo avec overlay IA MobileNet SSD (maxi 120 s)
#    /ia_stop            – Arrête la séquence /ia_continu en cours
#    /liste              – 5 derniers fichiers cliquables (envoi + suppression)
#    /status             – Température CPU + espace disque + état IA
#    /aide               – Liste des commandes
#
#  Gestion automatique du service Motion (via motion_mode_manager) :
#    Si Motion est actif lors d'une capture, il est arrêté automatiquement
#    avant la prise de vue, puis relancé après avec restauration du mode
#    ("stream" ou "record") géré par motion_mode_manager.py.
#    Le verrou inter-processus garantit qu'aucun autre bot ne touche à
#    motion.conf pendant la séquence.
#
#  Remarque firmware IMX500 :
#    Le premier chargement du réseau neuronal prend ~15-20 secondes.
#    Le bot envoie un message d'attente et reste muet pendant ce temps.
#
#  Prérequis :
#    pip install python-telegram-bot   (v20+)
#    rpicam-still et rpicam-vid doivent être disponibles sur le Pi
#    sudo visudo  :  jfbrunet ALL=(ALL) NOPASSWD: /usr/bin/systemctl * motion,
#                                                 /usr/bin/pkill -15 -x motion
#  Configuration :
#    Créer ou mettre à jour ~/.telegram_config :    (fichier caché)
#      [telegram]
#      token_imx500 = VOTRE_TOKEN_IMX500
#      chat_id      = VOTRE_CHAT_ID
#
#  Assets IA :
#    /usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json
#    /usr/share/rpi-camera-assets/imx500_posenet.json
#
#  Service systemd :
#    /etc/systemd/system/telegram-bot-imx500.service
#
#  Auteur  : Jean-François BRUNET – JFBConseils – Juin 2026
# ===============================================================================

import asyncio
import configparser
import datetime
import functools
import json
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
from telegram.request import HTTPXRequest

import sys
sys.path.insert(0, "/home/jfbrunet/Projects/Telegram")
from motion_mode_manager import set_mode, get_mode

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================
# Configuration (lecture depuis ~/.telegram_config)
# ============================================================
def charger_config() -> tuple[str, int]:
    """Charge TOKEN_IMX500 et CHAT_ID depuis ~/.telegram_config"""
    cfg_path = os.path.expanduser("~/.telegram_config")
    cfg = configparser.ConfigParser()

    if not os.path.exists(cfg_path):
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {cfg_path}\n"
            "Créez-le avec :\n"
            "  [telegram]\n"
            "  token_imx500 = VOTRE_TOKEN_IMX500\n"
            "  chat_id      = VOTRE_CHAT_ID"
        )

    cfg.read(cfg_path)
    token   = cfg["telegram"]["token_imx500"].strip()
    chat_id = int(cfg["telegram"]["chat_id"].strip())
    return token, chat_id

TOKEN, CHAT_ID = charger_config()

# ============================================================
# Timeouts Telegram (évite les "Timed out" sur envois lents)
# ============================================================
TELEGRAM_CONNECT_TIMEOUT = 10    # secondes
TELEGRAM_READ_TIMEOUT    = 60    # secondes — augmenté pour fichiers IA
TELEGRAM_WRITE_TIMEOUT   = 60    # secondes — idem
TELEGRAM_POOL_TIMEOUT    = 10    # secondes

# Nombre de tentatives d'envoi en cas de timeout réseau
SEND_MAX_RETRIES = 3
SEND_RETRY_DELAY = 5   # secondes entre deux tentatives

# ============================================================
# Constantes IMX500
# ============================================================
IMX500_CAMERA_INDEX       = "1"    # port caméra 1 sur le Pi 5
IMX500_CONFIDENCE_DEFAULT = 0.5
IMX500_JSON_OBJETS        = "/usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json"
IMX500_JSON_POSENET       = "/usr/share/rpi-camera-assets/imx500_posenet.json"

# Dossier de sauvegarde des images
SAVE_DIR = os.path.expanduser("~/Projects/Telegram/images_bot")
os.makedirs(SAVE_DIR, exist_ok=True)

# Nombre de fichiers affichés par /liste
LISTE_MAX_FILES = 5

# ============================================================
# État de la séquence IA continue (ia_continu)
# ============================================================
_ia_continu_actif: bool = False
_ia_continu_task: asyncio.Task | None = None

def ia_continu_stop():
    """Demande l'arrêt de la séquence /ia_continu."""
    global _ia_continu_actif
    _ia_continu_actif = False

# ============================================================
# Gestion automatique du service Motion (avec motion_mode_manager)
# ============================================================
SYSTEMCTL            = "/usr/bin/systemctl"
MOTION_STOP_GRACE    = 5    # secondes entre pkill et systemctl stop (augmenté)
MOTION_STOP_TIMEOUT  = 15   # secondes max pour attendre l'arrêt effectif

class MotionGuard:
    """Arrête Motion avant une capture et le relance ensuite si nécessaire.
    Intégration motion_mode_manager :
      - stop_if_running()  → mémorise le mode courant ("stream"/"record")
                             avant l'arrêt, via get_mode()
      - start_with_mode()  → relance Motion et restaure le mode mémorisé
                             via set_mode(), pour rester cohérent avec les
                             bots IMX708 qui partagent motion_mode_manager."""

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
    def _wait_until_stopped(timeout: int = MOTION_STOP_TIMEOUT) -> bool:
        """Attend que le service Motion soit réellement inactif.
        Retourne True si l'arrêt est confirmé avant le timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = subprocess.run(
                [SYSTEMCTL, "is-active", "motion"],
                capture_output=True, text=True,
            )
            if r.stdout.strip() != "active":
                return True
            time.sleep(0.5)
        logger.warning("MotionGuard._wait_until_stopped : timeout dépassé, "
                       "libcamera peut ne pas être libéré.")
        return False

    @staticmethod
    def stop_if_running() -> tuple[bool, str | None]:
        """Arrête Motion si actif.

        Retourne (motion_était_actif, mode_avant_arrêt).
        Le mode ("stream" ou "record") doit être restauré après la capture
        via start_with_mode() pour maintenir la cohérence avec motion_mode_manager.
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

        # Attendre l'arrêt effectif (libération libcamera)
        MotionGuard._wait_until_stopped()

        return True, mode_avant

    @staticmethod
    def start_with_mode(mode: str | None) -> tuple[bool, str]:
        """Relance Motion via systemctl start, puis restaure le mode
        dans motion.conf via motion_mode_manager si mode est fourni.
        Retourne (succès, message).
        """
        try:
            r = subprocess.run(
                ["sudo", SYSTEMCTL, "start", "motion"],
                capture_output=True, text=True, timeout=25,
            )
            started = r.returncode == 0
            msg = (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return False, "Timeout lors du redémarrage de Motion."
        except Exception as e:
            return False, str(e)

        # Restaurer le mode dans motion.conf si on le connaît
        if started and mode in ("stream", "record"):
            try:
                set_mode(mode, caller="telegram_bot_imx500")
                logger.info(f"MotionGuard : mode Motion restauré → '{mode}'")
            except Exception as e:
                logger.warning(f"MotionGuard : impossible de restaurer le mode '{mode}' : {e}")

        return started, msg

# ============================================================
# Lecture du mode Motion (pour /status et /aide)
# ============================================================
def get_motion_mode_label() -> str:
    """Retourne le libellé du mode Motion via motion_mode_manager.
    Retourne '🎥 Enregistrement .mkv' ou '📡 Flux continu (Visio seule)'."""
    try:
        return "🎥 Enregistrement .mkv" if get_mode() == "record" else "📡 Flux continu (Visio seule)"
    except Exception:
        return "🎥 Enregistrement .mkv"  # défaut si manager inaccessible

# ============================================================
# Décorateur de sécurité : filtre par CHAT_ID
# ============================================================
def acces_autorise(handler):
    """N'exécute la commande que si l'expéditeur est le CHAT_ID autorisé."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            logger.warning(f"Accès refusé pour chat_id={update.effective_chat.id}")
            await update.message.reply_text("⛔ Accès non autorisé.")
            return
        await handler(update, context)
    return wrapper

# ============================================================
# Utilitaires système
# ============================================================
def get_cpu_temp() -> str:
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

# ============================================================
# Helpers post-process JSON (seuil de confiance)
# ============================================================
def _get_postprocess_with_confidence(base_json: str, confidence: float) -> str:
    """Génère un JSON temporaire avec le seuil de confiance personnalisé."""
    if abs(confidence - 0.5) < 0.01:
        return base_json
    try:
        with open(base_json, "r") as f:
            data = json.load(f)
        for stage in data.get("post_process_stages", []):
            params = stage.setdefault("params", {})
            if "object_detect" in stage.get("name", ""):
                params["confidence_threshold"] = confidence
            elif "confidence_threshold" in params:
                params["confidence_threshold"] = confidence
        tmp = os.path.join(
            "/tmp",
            f"imx500_conf{int(confidence*100):03d}_{os.path.basename(base_json)}"
        )
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        return tmp
    except Exception:
        return base_json

# ============================================================
# Conversion H264 → MP4 (ffmpeg, copie flux sans réencodage)
# ============================================================
def _h264_vers_mp4(src_h264: str, dst_mp4: str) -> bool:
    """Convertit un fichier H264 brut en MP4 conteneur via ffmpeg.
    Retourne True si le fichier MP4 résultant est valide."""
    cmd = [
        "ffmpeg", "-y",
        "-framerate", "30",
        "-i", src_h264,
        "-c:v", "copy",
        dst_mp4,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            logger.error(f"ffmpeg H264→MP4 erreur : {r.stderr}")
            return False
        return os.path.exists(dst_mp4) and os.path.getsize(dst_mp4) > 0
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg H264→MP4 : timeout")
        return False
    except Exception as e:
        logger.error(f"ffmpeg H264→MP4 : {e}")
        return False

# ============================================================
# Fonctions de capture (synchrones — appelées via asyncio.to_thread)
# ============================================================

def capturer_imx500(fichier: str) -> bool:
    """Photo brute IMX500 (rpicam-still, caméra 1). Retourne True si OK."""
    cmd = [
        "rpicam-still",
        "--camera",  IMX500_CAMERA_INDEX,
        "--output",  fichier,
        "--timeout", "3000",
        "--quality", "90",
        "--immediate",
        "--nopreview",
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"
    try:
        result = subprocess.run(cmd, timeout=30, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            logger.error(f"rpicam-still IMX500 erreur : {result.stderr}")
            return False
        return os.path.exists(fichier) and os.path.getsize(fichier) > 1000
    except subprocess.TimeoutExpired:
        logger.error("rpicam-still IMX500 : timeout")
        return False
    except Exception as e:
        logger.error(f"rpicam-still IMX500 : {e}")
        return False

def capturer_imx500_ia(fichier: str, post_process_json: str) -> bool:
    """Photo annotée IA via rpicam-still + --post-process-file (MobileNet SSD).
    Timeout long (90 s) pour absorber le chargement du firmware IMX500.
    Retourne True si OK."""
    cmd = [
        "rpicam-still",
        "--camera",            IMX500_CAMERA_INDEX,
        "--output",            fichier,
        "--timeout",           "3000",
        "--quality",           "90",
        "--immediate",
        "--nopreview",
        "--post-process-file", post_process_json,
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"
    try:
        result = subprocess.run(cmd, timeout=90, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            logger.error(f"rpicam-still IA erreur : {result.stderr}")
            return False
        return os.path.exists(fichier) and os.path.getsize(fichier) > 1000
    except subprocess.TimeoutExpired:
        logger.error("rpicam-still IA : timeout (firmware trop long ?)")
        return False
    except Exception as e:
        logger.error(f"rpicam-still IA : {e}")
        return False

def capturer_imx500_pose(fichier: str) -> bool:
    """Capture une image avec squelette PoseNet incrusted."""
    tmp_h264 = fichier.replace(".jpg", "_pose_tmp.h264")
    cmd_vid = [
        "rpicam-vid",
        "--camera",            IMX500_CAMERA_INDEX,
        "--output",            tmp_h264,
        "--timeout",           "45000",
        "--width",             "1280",
        "--height",            "960",
        "--framerate",         "10",
        "--post-process-file", IMX500_JSON_POSENET,
        "--nopreview",
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"
    try:
        r = subprocess.run(cmd_vid, timeout=120, capture_output=True, text=True, env=env)
        if r.returncode != 0 or not os.path.exists(tmp_h264) or os.path.getsize(tmp_h264) == 0:
            logger.error(f"rpicam-vid PoseNet erreur : {r.stderr}")
            return False
        cmd_probe = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", tmp_h264,
        ]
        nb_frames = 1
        try:
            rp = subprocess.run(cmd_probe, capture_output=True, text=True, timeout=10)
            nb_frames = max(1, int(rp.stdout.strip()))
        except Exception:
            nb_frames = 15

        frame_cible = max(1, int(nb_frames * 0.8))
        cmd_ff = [
            "ffmpeg", "-y",
            "-i", tmp_h264,
            "-vf", f"select=eq(n\\,{frame_cible})",
            "-vframes", "1",
            "-q:v", "2",
            fichier,
        ]
        r2 = subprocess.run(cmd_ff, capture_output=True, text=True, timeout=15)
        if r2.returncode != 0 or not os.path.exists(fichier) or os.path.getsize(fichier) == 0:
            cmd_ff2 = [
                "ffmpeg", "-y",
                "-i", tmp_h264,
                "-vframes", "1",
                "-q:v", "2",
                fichier,
            ]
            r3 = subprocess.run(cmd_ff2, capture_output=True, text=True, timeout=15)
            if r3.returncode != 0 or not os.path.exists(fichier):
                logger.error(f"ffmpeg extraction frame erreur : {r3.stderr}")
                return False
        return os.path.exists(fichier) and os.path.getsize(fichier) > 1000
    except subprocess.TimeoutExpired:
        logger.error("capturer_imx500_pose : timeout")
        return False
    except Exception as e:
        logger.error(f"capturer_imx500_pose : {e}")
        return False
    finally:
        try:
            os.unlink(tmp_h264)
        except Exception:
            pass

def capturer_video_imx500(fichier_mp4: str, duree_sec: int) -> bool:
    """Vidéo normale IMX500 sans post-process.
    rpicam-vid produit du H264 brut → converti en MP4 via ffmpeg.
    Retourne True si le MP4 final est valide."""
    tmp_h264 = fichier_mp4.replace(".mp4", "_raw.h264")
    cmd = [
        "rpicam-vid",
        "--camera",    IMX500_CAMERA_INDEX,
        "--output",    tmp_h264,
        "--timeout",   str(duree_sec * 1000),
        "--width",     "1920",
        "--height",    "1080",
        "--framerate", "30",
        "--nopreview",
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"
    try:
        result = subprocess.run(
            cmd, timeout=duree_sec + 15,
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            logger.error(f"rpicam-vid IMX500 erreur : {result.stderr}")
            return False
        if not os.path.exists(tmp_h264) or os.path.getsize(tmp_h264) == 0:
            logger.error("rpicam-vid IMX500 : fichier H264 vide ou absent")
            return False
        return _h264_vers_mp4(tmp_h264, fichier_mp4)
    except subprocess.TimeoutExpired:
        logger.error("rpicam-vid IMX500 : timeout")
        return False
    except Exception as e:
        logger.error(f"rpicam-vid IMX500 : {e}")
        return False
    finally:
        try:
            os.unlink(tmp_h264)
        except Exception:
            pass

def capturer_video_ia(fichier_mp4: str, duree_sec: int,
                      confidence: float = IMX500_CONFIDENCE_DEFAULT) -> bool:
    """Vidéo avec overlay IA MobileNet SSD.
    rpicam-vid produit du H264 brut → converti en MP4 via ffmpeg.
    Retourne True si le MP4 final est valide."""
    pp = _get_postprocess_with_confidence(IMX500_JSON_OBJETS, confidence)
    tmp_h264 = fichier_mp4.replace(".mp4", "_raw.h264")
    cmd = [
        "rpicam-vid",
        "--camera",            IMX500_CAMERA_INDEX,
        "--output",            tmp_h264,
        "--timeout",           str(duree_sec * 1000),
        "--post-process-file", pp,
        "--nopreview",
    ]
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env["LIBCAMERA_LOG_LEVELS"] = "ERROR"
    try:
        result = subprocess.run(
            cmd, timeout=duree_sec + 60,
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            logger.error(f"rpicam-vid IA erreur : {result.stderr}")
            return False
        if not os.path.exists(tmp_h264) or os.path.getsize(tmp_h264) == 0:
            logger.error("rpicam-vid IA : fichier H264 vide ou absent")
            return False
        return _h264_vers_mp4(tmp_h264, fichier_mp4)
    except subprocess.TimeoutExpired:
        logger.error("rpicam-vid IA : timeout")
        return False
    except Exception as e:
        logger.error(f"rpicam-vid IA : {e}")
        return False
    finally:
        try:
            os.unlink(tmp_h264)
        except Exception:
            pass

# ============================================================
# Helpers Motion — messages d'état pour les handlers
# ============================================================
async def _stop_motion_si_actif(update: Update) -> tuple[bool, str | None]:
    """Arrête Motion si nécessaire (via asyncio.to_thread).
    Retourne (motion_était_actif, mode_avant) pour restauration ultérieure."""
    motion_actif, mode_avant = await asyncio.to_thread(MotionGuard.stop_if_running)
    if motion_actif:
        await update.message.reply_text(
            "⏸ Motion détecté — arrêt temporaire pour libérer la caméra…",
        )
    return motion_actif, mode_avant

async def _relancer_motion(update: Update, mode_avant: str | None):
    """Relance Motion et restaure le mode Motion via motion_mode_manager."""
    ok, msg = await asyncio.to_thread(MotionGuard.start_with_mode, mode_avant)
    if ok:
        mode_label = get_motion_mode_label() if mode_avant else "—"
        await update.message.reply_text(
            f"▶️ Motion relancé automatiquement.\n   ↳ Mode : {mode_label}",
        )
    else:
        await update.message.reply_text(
            f"⚠️ Impossible de relancer Motion : `{msg}`\n"
            "Relancez-le manuellement via le bot Motion.",
        )

# ============================================================
# Helper envoi robuste (retry sur timeout réseau)
# ============================================================
async def _envoyer_photo_robuste(bot, chat_id: int,
                                  fichier: str, legende: str) -> bool:
    """Envoie une photo avec retry en cas de timeout réseau Telegram.
    Retourne True si l'envoi a réussi."""
    for tentative in range(1, SEND_MAX_RETRIES + 1):
        try:
            with open(fichier, "rb") as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=legende)
            return True
        except Exception as e:
            err = str(e)
            if tentative < SEND_MAX_RETRIES and (
                "timed out" in err.lower() or "timeout" in err.lower()
                or "network" in err.lower()
            ):
                logger.warning(
                    f"Envoi photo tentative {tentative}/{SEND_MAX_RETRIES} échouée "
                    f"({err}), nouvel essai dans {SEND_RETRY_DELAY}s…"
                )
                await asyncio.sleep(SEND_RETRY_DELAY)
            else:
                logger.error(f"Envoi photo échoué après {tentative} tentative(s) : {e}")
                raise
    return False

# ============================================================
# Helper envoi vidéo
# ============================================================
async def _envoyer_video(update: Update, fichier: str, label: str, duree: int):
    """Envoie une vidéo MP4 dans le chat, avec vérification taille Telegram."""
    if not os.path.exists(fichier) or os.path.getsize(fichier) == 0:
        await update.message.reply_text(
            f"❌ La capture vidéo ({label}) a échoué ou le fichier est vide."
        )
        return
    taille_mo = os.path.getsize(fichier) / 1024**2
    legende = (
        f"🎬 Video {label} {duree}s - IMX500\n"
        f"📅 {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"💾 {taille_mo:.1f} Mo"
    )
    if taille_mo > 50:
        await update.message.reply_text(
            f"⚠️ Vidéo trop lourde pour Telegram ({taille_mo:.1f} Mo > 50 Mo).\n"
            f"Fichier conservé sur le Pi : `{fichier}`",
        )
        return
    with open(fichier, "rb") as vid:
        await update.message.reply_video(
            video=vid,
            caption=legende,
            supports_streaming=True,
        )
    logger.info(f"Vidéo envoyée : {fichier}  ({taille_mo:.1f} Mo)")

# ============================================================
# Handlers des commandes
# ============================================================

# ------------------------------------------------------------
# /photo
# ------------------------------------------------------------
@acces_autorise
async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/photo – Capture une image IMX500 et l'envoie."""
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)
    await update.message.reply_text("📷 Capture IMX500 en cours...")

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx500_{horodatage}.jpg")

    succes = await asyncio.to_thread(capturer_imx500, fichier)

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "❌ Capture échouée. Vérifiez que l'IMX500 est connectée (port 1)."
        )
        return

    taille = _format_size(os.path.getsize(fichier))
    legende = f"📸 IMX500 \n📅 {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')} - {taille}"
    with open(fichier, "rb") as photo:
        await update.message.reply_photo(photo=photo, caption=legende)
    logger.info(f"Photo envoyée : {fichier}")

# ------------------------------------------------------------
# /rafale
# ------------------------------------------------------------
@acces_autorise
async def cmd_rafale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rafale <n> – Capture n images en séquence rapide (maxi 10)."""
    try:
        n = int(context.args[0]) if context.args else 3
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Usage : /rafale <nombre>  (ex. /rafale 5)")
        return

    n = max(1, min(n, 10))
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)
    await update.message.reply_text(f"🔁 Rafale de {n} image(s) en cours...")

    horodatage_base = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichiers_ok = []

    for i in range(1, n + 1):
        fichier = os.path.join(SAVE_DIR, f"imx500_rafale_{horodatage_base}_{i:02d}.jpg")
        succes  = await asyncio.to_thread(capturer_imx500, fichier)
        if succes:
            fichiers_ok.append(fichier)
            logger.info(f"Rafale {i}/{n} : OK")
        else:
            logger.warning(f"Rafale {i}/{n} : échec")

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
        legende = (
            f"🔁 Rafale {idx + 1}/{len(fichiers_ok)} - {os.path.basename(f)}"
            if idx == 0 else ""
        )
        media.append(InputMediaPhoto(media=data, caption=legende))

    await update.message.reply_media_group(media=media)
    logger.info(f"Rafale envoyée : {len(fichiers_ok)}/{n} images")

# ------------------------------------------------------------
# /video
# ------------------------------------------------------------
@acces_autorise
async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/video <sec> – Capture une vidéo normale IMX500 (maxi 120 s)."""
    try:
        duree = int(context.args[0]) if context.args else 10
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Usage : /video <secondes>  (ex. /video 20)")
        return

    duree = max(1, min(duree, 120))
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)
    msg_attente = await update.message.reply_text(
        f"🎬 Enregistrement vidéo de {duree}s\n  ⏳ merci de patienter..."
    )

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx500_video_{horodatage}.mp4")

    succes = await asyncio.to_thread(capturer_video_imx500, fichier, duree)

    # Confirmer la fin de l'enregistrement en éditant le message d'attente
    try:
        if succes:
            await msg_attente.edit_text(
                f"✅ Enregistrement terminé ({duree}s) — envoi en cours…"
            )
        else:
            await msg_attente.edit_text(
                f"❌ Enregistrement échoué ({duree}s)"
            )
    except Exception:
        pass  # edit_text peut échouer si le message a été supprimé

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "❌ Capture vidéo échouée. Vérifiez que l'IMX500 est connectée (port 1)."
        )
        return

    await _envoyer_video(update, fichier, "", duree)

# ------------------------------------------------------------
# /ia_objets  – photo unique annotée MobileNet SSD
# ------------------------------------------------------------
@acces_autorise
async def cmd_ia_objets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ia_objets – Photo annotée détection d'objets (MobileNet SSD)."""
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)

    await update.message.reply_text(
        "🤖 Détection objets IA\n      ...capture en cours...\n"
        "⏳ Chargement firmware ~15-20s",
    )

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx500_objets_{horodatage}.jpg")
    pp         = _get_postprocess_with_confidence(
        IMX500_JSON_OBJETS, IMX500_CONFIDENCE_DEFAULT
    )

    succes = await asyncio.to_thread(capturer_imx500_ia, fichier, pp)

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "❌ Capture IA échouée. Vérifiez que l'IMX500 est connectée (port 1)."
        )
        return

    legende = (
        f"🔍 Objets IMX500\n📅 {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n      confiance >= {IMX500_CONFIDENCE_DEFAULT:.0%}"
    )
    with open(fichier, "rb") as photo:
        await update.message.reply_photo(photo=photo, caption=legende)
    logger.info(f"Photo IA objets envoyée : {fichier}")

# ------------------------------------------------------------
# /ia_pose  – photo unique annotée PoseNet
# ------------------------------------------------------------
@acces_autorise
async def cmd_ia_pose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ia_pose - Photo avec squelette PoseNet incrusted."""
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)

    await update.message.reply_text(
        "🦴 PoseNet - capture en cours...\n⏳ Chargement firmware ~30-45s\n ...  merci de patienter..."
    )

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx500_pose_{horodatage}.jpg")

    succes = await asyncio.to_thread(capturer_imx500_pose, fichier)

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "Capture PoseNet échouée. Verifiez IMX500 (port 1) et ffmpeg installé."
        )
        return

    legende = f"🦴 PoseNet IMX500\n📅 {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    with open(fichier, "rb") as photo:
        await update.message.reply_photo(photo=photo, caption=legende)
    logger.info(f"Photo PoseNet envoyée : {fichier}")

# ------------------------------------------------------------
# Tâche de fond : boucle ia_continu (indépendante du handler)
# ------------------------------------------------------------
async def _ia_continu_loop(bot, chat_id: int, n: int, s: int,
                           pp: str, motion_was_running: bool,
                           mode_avant: str | None):
    """Boucle ia_continu dans une Task asyncio séparée."""
    global _ia_continu_actif, _ia_continu_task

    ok_count  = 0
    err_count = 0

    try:
        for i in range(1, n + 1):

            if not _ia_continu_actif:
                await bot.send_message(chat_id,
                    f"⏹ Séquence interrompue après {i - 1}/{n} photo(s).")
                break

            horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fichier    = os.path.join(SAVE_DIR,
                                      f"imx500_continu_{horodatage}_{i:02d}.jpg")

            succes = await asyncio.to_thread(capturer_imx500_ia, fichier, pp)

            # Vérifier l'arrêt aussi après la capture (longue)
            if not _ia_continu_actif:
                await bot.send_message(chat_id,
                    f"⏹ Séquence interrompue après {i - 1}/{n} photo(s).")
                break

            if succes:
                ok_count += 1
                taille_c = _format_size(os.path.getsize(fichier))
                legende  = (f"IA {i}/{n} — "
                            f"{datetime.datetime.now().strftime('%H:%M:%S')} — "
                            f"{taille_c}")
                try:
                    await _envoyer_photo_robuste(bot, chat_id, fichier, legende)
                    logger.info(f"ia_continu {i}/{n} envoyée : {fichier}")
                except Exception as e:
                    err_count += 1
                    logger.error(f"ia_continu {i}/{n} envoi échoué : {e}")
                    await bot.send_message(
                        chat_id,
                        f"⚠️ Photo {i}/{n} capturée mais envoi échoué : {e}\n"
                        f"Fichier conservé : {os.path.basename(fichier)}"
                    )
            else:
                err_count += 1
                await bot.send_message(chat_id, f"⚠️ Capture {i}/{n} échouée.")
                logger.warning(f"ia_continu {i}/{n} : échec")

            # Attente inter-captures (sauf après la dernière)
            if i < n and _ia_continu_actif:
                await asyncio.sleep(s)

        # Fin naturelle
        if _ia_continu_actif:
            await bot.send_message(
                chat_id,
                f"✅ Séquence terminée : {ok_count} photo(s) ok"
                + (f", {err_count} échec(s)." if err_count else ".")
            )

    except asyncio.CancelledError:
        await bot.send_message(chat_id, "⏹ Séquence IA annulée.")
    except Exception as e:
        logger.error(f"_ia_continu_loop : erreur inattendue : {e}")
        await bot.send_message(chat_id,
                               f"❌ Erreur inattendue dans la séquence IA : {e}")
    finally:
        _ia_continu_actif = False
        _ia_continu_task  = None
        if motion_was_running:
            ok, msg = await asyncio.to_thread(MotionGuard.start_with_mode, mode_avant)
            if ok:
                mode_label = get_motion_mode_label() if mode_avant else "—"
                await bot.send_message(
                    chat_id,
                    f"▶️ Motion relancé automatiquement.\n   ↳ Mode : {mode_label}"
                )
            else:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Impossible de relancer Motion : `{msg}`\n"
                    "Relancez-le manuellement via le bot Motion."
                )

# ------------------------------------------------------------
# /ia_continu <n> <s>  – lance la tâche de fond
# ------------------------------------------------------------
@acces_autorise
async def cmd_ia_continu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ia_continu <n> <s> – n photos IA espacées de s secondes (défaut : 5 photos, 10 s)."""
    global _ia_continu_actif, _ia_continu_task

    if _ia_continu_actif:
        await update.message.reply_text(
            "ℹ️ Une séquence IA est déjà en cours. Utilisez /ia_stop pour l'arrêter."
        )
        return

    try:
        n = int(context.args[0]) if len(context.args) >= 1 else 5
        s = int(context.args[1]) if len(context.args) >= 2 else 10
    except (ValueError, IndexError):
        await update.message.reply_text(
            "⚠️ Usage : /ia_continu <nb_photos> <intervalle_s>\n"
            "Exemple : /ia_continu 6 15  (6 photos toutes les 15 s)"
        )
        return

    n = max(1, min(n, 20))
    s = max(3, min(s, 300))

    motion_was_running, mode_avant = await _stop_motion_si_actif(update)
    pp = _get_postprocess_with_confidence(IMX500_JSON_OBJETS, IMX500_CONFIDENCE_DEFAULT)

    _ia_continu_actif = True

    _ia_continu_task = asyncio.create_task(
        _ia_continu_loop(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            n=n, s=s, pp=pp,
            motion_was_running=motion_was_running,
            mode_avant=mode_avant,
        )
    )

    await update.message.reply_text(
        f"🤖 Séquence IA lancée :\n"
        f"     {n} photos, intervalle {s}s\n"
        f"⏳ ~25s par capture\n"
        f"     /ia_stop pour interrompre."
    )

# ------------------------------------------------------------
# /ia_stop
# ------------------------------------------------------------
@acces_autorise
async def cmd_ia_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ia_stop – Arrête la séquence /ia_continu en cours."""
    global _ia_continu_actif, _ia_continu_task
    if not _ia_continu_actif:
        await update.message.reply_text("ℹ️ Aucune séquence IA en cours.")
        return
    ia_continu_stop()
    if _ia_continu_task:
        _ia_continu_task.cancel()
    await update.message.reply_text(
        "⏹ Séquence IA : arrêt demandé.\n"
        "L'interruption sera effective à la fin de la capture en cours.",
    )
    logger.info("Séquence ia_continu interrompue via /ia_stop")

# ------------------------------------------------------------
# /ia_video <sec>
# ------------------------------------------------------------
@acces_autorise
async def cmd_ia_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ia_video <sec> – Vidéo avec overlay IA MobileNet SSD (maxi 120 s)."""
    try:
        duree = int(context.args[0]) if context.args else 10
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Usage : /ia_video <secondes>  (ex. /ia_video 20)")
        return

    duree = max(1, min(duree, 120))
    motion_was_running, mode_avant = await _stop_motion_si_actif(update)

    msg_attente = await update.message.reply_text(
        f"🎬 Video IA {duree}s - démarrage...\n"
        "⏳ Chargement firmware ~15-20s",
    )

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier    = os.path.join(SAVE_DIR, f"imx500_ia_{horodatage}.mp4")

    succes = await asyncio.to_thread(
        capturer_video_ia, fichier, duree, IMX500_CONFIDENCE_DEFAULT
    )

    # Confirmer la fin de l'enregistrement IA
    try:
        if succes:
            await msg_attente.edit_text(
                f"✅ Enregistrement IA terminé ({duree}s) — envoi en cours…"
            )
        else:
            await msg_attente.edit_text(
                f"❌ Enregistrement IA échoué ({duree}s)"
            )
    except Exception:
        pass

    if motion_was_running:
        await _relancer_motion(update, mode_avant)

    if not succes:
        await update.message.reply_text(
            "❌ Capture vidéo IA échouée. Vérifiez que l'IMX500 est connectée (port 1)."
        )
        return

    await _envoyer_video(update, fichier, "IA", duree)

# ------------------------------------------------------------
# /status
# ------------------------------------------------------------
@acces_autorise
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status – Température CPU, espace disque, état Motion et IA."""
    temp        = get_cpu_temp()
    disque      = get_espace_disque()
    heure       = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    running     = MotionGuard.is_running()
    motion_etat = "🟢 actif"  if running             else "🔴 arrêté"
    motion_mode = get_motion_mode_label() if running else "—"
    ia_etat     = "🟢 active" if _ia_continu_actif   else "🔴 arrêtée"

    texte = (
        f"🖥 Statut Pi5 - IMX500\n"
        f"────────────────────\n"
        f"🕐 {heure}\n\n"
        f"🌡 CPU : {temp}\n"
        f"💾 : {disque}\n"
        f"📁 : {SAVE_DIR}\n"
        f"🎥 Motion : {motion_etat}\n"
        f"   ↳ Mode : {motion_mode}\n"
        f"🤖 Séquence IA : {ia_etat}"
    )
    await update.message.reply_text(texte)

# ------------------------------------------------------------
# /aide
# ------------------------------------------------------------
@acces_autorise
async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/aide - Liste des commandes disponibles."""
    running     = MotionGuard.is_running()
    motion_etat = "🟢" if running           else "🔴"
    motion_mode = get_motion_mode_label()   if running else "—"
    ia_etat     = "ON" if _ia_continu_actif else "OFF"
    texte = (
        f"🤖 Bot IA IMX500 — Cdes\n"
        f"─────────────────\n"
        f"🎥 Motion : {motion_etat}  ↳ {motion_mode}\n"
        f"🤖 Séquence IA : {ia_etat}\n\n"
        "/photo - 1 photo\n"
        "/rafale n - jusqu'a 10 photos\n"
        "/video sec - video (max 120s)\n\n"
        "/ia_objets - photo objets annotés\n"
        "/ia_pose - photo pose annotée\n"
        "/ia_continu n s - n photos / s sec\n"
        "/ia_video sec - vidéo IA (max 120s)\n"
        "/ia_stop - stopper ia_continu\n\n"
        "/liste - 5 derniers fichiers\n"
        "/status - température & disque...\n"
        "/aide - ce menu"
    )
    await update.message.reply_text(texte)

# ============================================================
# /liste — boutons cliquables : envoi ou suppression
# ============================================================
@acces_autorise
async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/liste – Affiche les 5 derniers fichiers avec boutons Envoyer / Supprimer."""
    fichiers = _list_recent_files(LISTE_MAX_FILES)

    if not fichiers:
        await update.message.reply_text("📂 Aucun fichier trouvé dans le dossier.")
        return

    await update.message.reply_text(
        f"📂 {LISTE_MAX_FILES} derniers fichiers\n{SAVE_DIR}\n\nChoisissez une action :",
    )

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
            label = f"- {nom}"

        keyboard = [[
            InlineKeyboardButton("📤 Envoyer",    callback_data=f"cam_send:{nom}"),
            InlineKeyboardButton("🗑 Supprimer", callback_data=f"cam_del:{nom}"),
        ]]
        await update.message.reply_text(
            label,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

# ============================================================
# Callbacks inline (/liste : envoi et suppression)
# ============================================================
async def callback_dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # --- Envoi d'un fichier ---
    if data.startswith("cam_send:"):
        nom    = data[len("cam_send:"):]
        chemin = os.path.join(SAVE_DIR, nom)

        if not os.path.exists(chemin):
            await query.answer("❌ Fichier introuvable (déjà supprimé ?).", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=None)
            return

        taille_mo = os.path.getsize(chemin) / 1024**2
        ext       = os.path.splitext(nom)[1].lower()
        mtime     = os.path.getmtime(chemin)
        legende   = (
            f"📁 `{nom}`\n"
            f"📅 {datetime.datetime.fromtimestamp(mtime).strftime('%d/%m/%Y %H:%M:%S')}\n"
            f"💾 {_format_size(os.path.getsize(chemin))}"
        )

        try:
            with open(chemin, "rb") as f:
                if ext in (".jpg", ".jpeg", ".png"):
                    await query.message.reply_photo(photo=f, caption=legende)
                elif taille_mo > 50:
                    await query.message.reply_text(
                        f"⚠️ Fichier trop lourd pour Telegram ({taille_mo:.1f} Mo > 50 Mo).\n"
                        f"Accessible sur le Pi : `{chemin}`",
                    )
                else:
                    await query.message.reply_video(video=f, caption=legende)
            logger.info(f"Fichier envoyé via /liste : {nom}")
        except Exception as exc:
            logger.warning(f"Erreur envoi {nom} : {exc}")
            await query.message.reply_text(
                f"❌ Impossible d'envoyer `{nom}` : {exc}"
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

        keyboard = [[
            InlineKeyboardButton("✅ Suppression", callback_data=f"cam_del_ok:{nom}"),
            InlineKeyboardButton("❌ Annuler",     callback_data=f"cam_del_cancel:{nom}"),
        ]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- Confirmation suppression ---
    if data.startswith("cam_del_ok:"):
        nom    = data[len("cam_del_ok:"):]
        chemin = os.path.join(SAVE_DIR, nom)
        try:
            os.unlink(chemin)
            await query.edit_message_text(f"🗑 `{nom}` supprimé.")
            logger.info(f"Fichier supprimé via /liste : {nom}")
        except FileNotFoundError:
            await query.edit_message_text(f"⚠️ `{nom}` déjà supprimé.")
        except Exception as exc:
            await query.edit_message_text(
                f"❌ Impossible de supprimer `{nom}` : {exc}"
            )
        return

    # --- Annulation suppression ---
    if data.startswith("cam_del_cancel:"):
        nom = data[len("cam_del_cancel:"):]
        keyboard = [[
            InlineKeyboardButton("📤 Envoyer",    callback_data=f"cam_send:{nom}"),
            InlineKeyboardButton("🗑 Supprimer", callback_data=f"cam_del:{nom}"),
        ]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    logger.warning(f"callback_dispatch : data inconnu : '{data}'")

# ============================================================
# Attente NTP & Message automatique au démarrage
# ============================================================
async def _attendre_ntp(timeout: int = 60) -> bool:
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

async def message_demarrage(application) -> None:
    ntp_ok      = await _attendre_ntp(timeout=60)
    heure       = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    running     = MotionGuard.is_running()
    motion_etat = "🟢 actif" if running else "🔴 arrêté"
    motion_mode = get_motion_mode_label() if running else "—"
    ntp_avert   = "" if ntp_ok else "\n⚠️ Heure non synchro NTP"
    await application.bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"🤖 Bot IMX500 démarré\n"
            f"Date : {heure}{ntp_avert}\n\n"
            f"🎥 Motion : {motion_etat}\n"
            f"   ↳ Mode : {motion_mode}\n"
            f"🤖 IA : attente 1ère capture\n\n"
            f"Tapez /aide"
        ),
    )
    logger.info(f"Message démarrage envoyé (NTP OK : {ntp_ok}).")

# ============================================================
# Point d'entrée
# ============================================================
def main():
    logger.info("Démarrage du bot Telegram IMX500…")
    logger.info(f"CHAT_ID autorisé : {CHAT_ID}")
    logger.info(f"Dossier images   : {SAVE_DIR}")

    request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
    )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request)
        .post_init(message_demarrage)
        .build()
    )

    app.add_handler(CommandHandler("photo",      cmd_photo))
    app.add_handler(CommandHandler("rafale",     cmd_rafale))
    app.add_handler(CommandHandler("video",      cmd_video))
    app.add_handler(CommandHandler("ia_objets",  cmd_ia_objets))
    app.add_handler(CommandHandler("ia_pose",    cmd_ia_pose))
    app.add_handler(CommandHandler("ia_continu", cmd_ia_continu))
    app.add_handler(CommandHandler("ia_video",   cmd_ia_video))
    app.add_handler(CommandHandler("ia_stop",    cmd_ia_stop))
    app.add_handler(CommandHandler("liste",      cmd_liste))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("aide",       cmd_aide))
    app.add_handler(CommandHandler("start",      cmd_aide))
    app.add_handler(CallbackQueryHandler(callback_dispatch))

    logger.info("Bot IMX500 en écoute… (Ctrl+C pour arrêter)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
