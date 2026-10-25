#!/usr/bin/env python3
# =============================================================================
#  Bot Telegram — Motion / IMX708 (Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe)
#
#  Commandes :
#    /aide        Menu des commandes
#    /status      État du service Motion (actif / inactif + diagnostics)
#    /mode        Chgt Mode Motion (enregistrement .mkv ou Flux Continu pour Visio)
#    /demarrer    Démarre le service Motion
#    /arreter     Arrête le service Motion (confirmation)
#    /rafraichir  Redémarre le service Motion (confirmation)
#    /alertes     Active ou désactive les alertes Telegram
#    /lister      Liste les fichiers dans motion_captures (cliquables)
#    /effacer     Vide le dossier motion_captures (confirmation)
#
#  Alertes automatiques :
#    - Dès qu'un nouveau fichier JPEG/MKV apparaît dans ~/motion_captures,
#      il est envoyé sur Telegram avec horodatage.
#    - Anti-spam : 1 fichier max toutes les ALERT_COOLDOWN secondes.
#
#  Prérequis :
#    sudo apt install motion -y
#    pip install python-telegram-bot --break-system-packages
#    sudo visudo  →  jfbrunet ALL=(ALL) NOPASSWD: /usr/bin/systemctl * motion,
#                                                 /usr/bin/pkill -15 -x motion
#  Configuration — ~/.telegram_config :
#      [telegram]
#      token_motion = VOTRE_TOKEN_MOTION_BOT
#      chat_id      = VOTRE_CHAT_ID
#
#  Service Motion avec libcamerify (IMX708) :
#    sudo nano /usr/lib/systemd/system/motion.service
#    ExecStart=/usr/bin/libcamerify /usr/bin/motion
#    sudo systemctl daemon-reload
#
#  Auteur : Jean-François BRUNET – JFBConseils – Juin 2026
# =============================================================================

import asyncio
import os
import subprocess
import time
import logging
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from motion_mode_manager import set_mode, get_mode

# =============================================================================
#  CONFIGURATION
# =============================================================================

CONFIG_FILE  = Path.home() / ".telegram_config"
TOKEN_KEY    = "token_motion"

SYSTEMCTL                = "/usr/bin/systemctl"
LIBCAMERIFY              = "/usr/bin/libcamerify"
MOTION_DEV_IMX708        = "/dev/video8"
PI5_IP                   = "192.168.1.200"
MOTION_WEB_CTRL          = f"http://{PI5_IP}:8080"
MOTION_WEB_STREAM        = f"http://{PI5_IP}:8081"
MOTION_CAPTURES_DIR      = Path.home() / "motion_captures"
MOTION_SYSTEMCTL_TIMEOUT = 25   # secondes
MOTION_STOP_GRACE        = 3    # secondes entre pkill et systemctl stop

# Surveillance du dossier de captures
POLL_INTERVAL  = 2     # secondes entre chaque scan du dossier
ALERT_COOLDOWN = 10    # secondes minimum entre 2 alertes (anti-spam)
IMAGE_EXTS     = {".jpg", ".jpeg", ".mkv", ".mp4", ".avi"}

# Pagination /lister
FILES_PER_PAGE = 8

# Watchdog anti-freeze libcamera
WATCHDOG_INTERVAL   = 300   # vérification toutes les 5 min
WATCHDOG_MAX_FREEZE = 600   # redémarrage auto si aucune capture depuis 10 min

# =============================================================================
#  Logging
# =============================================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# =============================================================================
#  Chargement du token et du chat_id
# =============================================================================

def _parse_config() -> dict:
    config = {}
    if not CONFIG_FILE.exists():
        return config
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            config[key.strip().lower()] = val.strip()
    return config

def load_token() -> str:
    config = _parse_config()
    token = config.get(TOKEN_KEY.lower(), "")
    if not token:
        raise ValueError(
            f"Clé '{TOKEN_KEY}' introuvable ou vide dans {CONFIG_FILE}.\n"
            f"Ajoutez :  {TOKEN_KEY} = <votre_token>"
        )
    return token

def _get_admin_chat_id() -> int | None:
    config = _parse_config()
    val = config.get("chat_id", "")
    if val.lstrip("-").isdigit():
        return int(val)
    return None

# =============================================================================
#  MotionController
# =============================================================================

class MotionController:

    @staticmethod
    def _systemctl(action: str) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["sudo", SYSTEMCTL, action, "motion"],
                capture_output=True, text=True,
                timeout=MOTION_SYSTEMCTL_TIMEOUT,
            )
            ok  = result.returncode == 0
            msg = result.stdout.strip() or result.stderr.strip()
            return ok, msg
        except subprocess.TimeoutExpired:
            return False, (
                f"⏱ Délai dépassé ({MOTION_SYSTEMCTL_TIMEOUT} s). "
                "Motion est peut-être en cours de démarrage — "
                "tapez /status dans quelques secondes."
            )
        except Exception as e:
            return False, str(e)

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
    def check_libcamerify() -> bool:
        return os.path.isfile(LIBCAMERIFY)

    @staticmethod
    def check_service_patched() -> bool:
        service_file = "/usr/lib/systemd/system/motion.service"
        try:
            with open(service_file) as f:
                return "libcamerify" in f.read()
        except Exception:
            return False

    @staticmethod
    def check_device_imx708() -> bool:
        return os.path.exists(MOTION_DEV_IMX708)

    @staticmethod
    def demarrer() -> tuple[bool, str]:
        return MotionController._systemctl("start")

    @staticmethod
    def arreter_proprement() -> tuple[bool, str]:
        try:
            subprocess.run(
                ["sudo", "pkill", "-15", "-x", "motion"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        time.sleep(MOTION_STOP_GRACE)
        return MotionController._systemctl("stop")

    @staticmethod
    def redemarrer() -> tuple[bool, str]:
        MotionController.arreter_proprement()
        time.sleep(1)
        return MotionController._systemctl("start")

    @staticmethod
    def get_service_status() -> str:
        try:
            r = subprocess.run(
                [SYSTEMCTL, "status", "motion", "--no-pager", "-l"],
                capture_output=True, text=True,
            )
            return (r.stdout + r.stderr).strip()
        except Exception as e:
            return str(e)

    @staticmethod
    def get_movie_output() -> bool:
        """Retourne True si le mode courant est 'record' (movie_output on)."""
        try:
            return get_mode() == "record"
        except Exception:
            return True  # défaut conservateur

    @staticmethod
    def set_movie_output(activer: bool) -> bool:
        """Définit le mode Motion via motion_mode_manager (verrou inter-processus).
        activer=True  → mode 'record'  (movie_output on)
        activer=False → mode 'stream'  (movie_output off)"""
        try:
            mode = "record" if activer else "stream"
            return set_mode(mode, caller="telegram_bot_motion_imx708")
        except Exception as e:
            log.error(f"[MotionController.set_movie_output] Erreur : {e}")
            return False

    @staticmethod
    def get_capture_dir() -> Path:
        """Lit target_dir dans motion.conf ; retourne le défaut si absent."""
        for conf in ["/etc/motion/motion.conf",
                     Path.home() / ".motion/motion.conf"]:
            try:
                with open(conf) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("target_dir") and not line.startswith("#"):
                            parts = line.split(None, 1)
                            if len(parts) == 2:
                                return Path(parts[1].strip())
            except Exception:
                pass
        return MOTION_CAPTURES_DIR

# =============================================================================
#  CaptureWatcher — surveillance du dossier de captures
# =============================================================================

class CaptureWatcher:
    """Scrute le dossier Motion toutes les POLL_INTERVAL secondes.
    Envoie sur Telegram chaque nouveau JPEG/MKV détecté,
    avec anti-spam de ALERT_COOLDOWN secondes entre 2 envois."""

    def __init__(self):
        self._known_files: set[str] = set()
        self._last_sent: float      = 0.0
        self._enabled: bool         = True
        self._initialized: bool     = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def toggle(self) -> bool:
        self._enabled = not self._enabled
        return self._enabled

    def _scan(self, capture_dir: Path) -> list[Path]:
        try:
            current = {
                f for f in os.listdir(capture_dir)
                if Path(f).suffix.lower() in IMAGE_EXTS
            }
        except FileNotFoundError:
            return []

        if not self._initialized:
            self._known_files = current
            self._initialized = True
            log.info(
                f"CaptureWatcher initialisé — "
                f"{len(current)} fichier(s) existant(s) ignoré(s)."
            )
            return []

        new_files = current - self._known_files
        self._known_files = current

        result = []
        for f in new_files:
            p = capture_dir / f
            try:
                result.append((p, p.stat().st_mtime))
            except FileNotFoundError:
                pass

        return [p for p, _ in sorted(result, key=lambda x: x[1])]

    async def watch_loop(self, bot, chat_id: int, capture_dir: Path):
        log.info(f"CaptureWatcher démarré — dossier : {capture_dir}")
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            if not self._enabled:
                continue

            new_files = await asyncio.to_thread(self._scan, capture_dir)

            for img_path in new_files:
                elapsed = time.monotonic() - self._last_sent

                if elapsed < ALERT_COOLDOWN:
                    remaining = int(ALERT_COOLDOWN - elapsed)
                    log.info(
                        f"Anti-spam : {img_path.name} ignoré "
                        f"(prochain envoi dans {remaining} s)."
                    )
                    continue

                ts = datetime.fromtimestamp(
                    img_path.stat().st_mtime
                ).strftime("%d/%m/%Y %H:%M:%S")

                caption = (
                    f"🚨 *Détection Motion – IMX708*\n"
                    f"📅 {ts}\n"
                    f"📁 `{img_path.name}`"
                )

                try:
                    suffix = img_path.suffix.lower()
                    if suffix in {".jpg", ".jpeg"}:
                        with open(img_path, "rb") as f:
                            await bot.send_photo(
                                chat_id=chat_id,
                                photo=f,
                                caption=caption,
                                parse_mode="Markdown",
                            )
                    else:  # .mkv, .mp4, .avi — notification + bouton de téléchargement
                        dl_keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                f"📥 Télécharger {img_path.name}",
                                callback_data=f"dl:{img_path.name}",
                            )
                        ]])
                        await bot.send_message(
                            chat_id=chat_id,
                            text=caption,
                            parse_mode="Markdown",
                            reply_markup=dl_keyboard,
                        )
                    self._last_sent = time.monotonic()
                    log.info(f"Fichier envoyé : {img_path.name}")
                except Exception as exc:
                    log.warning(f"Impossible d'envoyer {img_path.name} : {exc}")

    def refresh_known(self, capture_dir: Path):
        """Resynchronise _known_files après un effacement du dossier."""
        self._known_files = set()

# Instance globale partagée entre tous les handlers
watcher = CaptureWatcher()

# =============================================================================
#  MotionWatchdog — redémarrage automatique si libcamera se fige
# =============================================================================

class MotionWatchdog:
    """Vérifie périodiquement que le pipeline libcamera répond vraiment,
    en interrogeant le webcontrol HTTP de Motion (port 8080).
    Principe : systemd peut voir Motion comme 'actif' alors que libcamera
    est figé. Le webcontrol ne répond que si le pipeline est opérationnel.
    Comportement silencieux : aucun message Telegram."""

    WEBCONTROL_URL     = f"http://localhost:8080"
    HTTP_TIMEOUT       = 5      # secondes
    MAX_FAILURES       = 3      # échecs consécutifs avant redémarrage

    def _check_webcontrol(self) -> bool:
        """Retourne True si le webcontrol Motion répond (pipeline vivant)."""
        try:
            import urllib.request
            urllib.request.urlopen(self.WEBCONTROL_URL, timeout=self.HTTP_TIMEOUT)
            return True
        except Exception:
            return False

    async def watch_loop(self, bot, chat_id: int):
        """Tâche asyncio indépendante — tourne en permanence en arrière-plan.
        Silencieuse tant que tout va bien."""
        log.info(
            f"MotionWatchdog démarré — vérification toutes les "
            f"{WATCHDOG_INTERVAL} s ({self.MAX_FAILURES} échecs → redémarrage)."
        )
        await asyncio.sleep(WATCHDOG_MAX_FREEZE)  # grace period au démarrage

        failures = 0

        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)

            # Motion arrêté volontairement → on ne touche à rien
            if not MotionController.is_running():
                failures = 0
                continue

            alive = await asyncio.to_thread(self._check_webcontrol)

            if alive:
                if failures > 0:
                    log.info("MotionWatchdog : webcontrol de nouveau accessible.")
                failures = 0
                continue

            # Échec webcontrol
            failures += 1
            log.warning(
                f"MotionWatchdog : webcontrol inaccessible "
                f"({failures}/{self.MAX_FAILURES})."
            )

            if failures < self.MAX_FAILURES:
                continue  # on attend encore avant d'agir

            # Seuil atteint → redémarrage silencieux (aucun message Telegram)
            log.warning("MotionWatchdog : seuil atteint — redémarrage silencieux de Motion.")
            ok, _ = await asyncio.to_thread(MotionController.redemarrer)
            failures = 0
            if ok:
                log.info("MotionWatchdog : Motion redémarré avec succès.")
            else:
                log.error("MotionWatchdog : échec du redémarrage — vérifier manuellement.")

watchdog = MotionWatchdog()

# =============================================================================
#  Helpers
# =============================================================================

def _wait_file_stable(path: Path, checks: int = 3, interval: float = 1.0) -> bool:
    """Attend que le fichier ait une taille stable (écriture terminée).
    Retourne True si stable, False si toujours vide après les vérifications."""
    prev_size = -1
    for _ in range(checks):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == 0:
            time.sleep(interval)
            continue
        if size == prev_size:
            return True
        prev_size = size
        time.sleep(interval)
    return prev_size > 0

def _truncate(text: str, limit: int = 3800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[tronqué]"

def _etat_emoji() -> str:
    return "🟢" if MotionController.is_running() else "🔴"

def _mode_emoji() -> str:
    if not MotionController.is_running():
        return "—"
    return "🎥 Enregistrement MKV" if MotionController.get_movie_output() else "📡 Flux continu"

def _alertes_emoji() -> str:
    return "🔔" if watcher.enabled else "🔕"

def _build_diagnostics() -> str:
    lc_ok   = MotionController.check_libcamerify()
    svc_ok  = MotionController.check_service_patched()
    dev_ok  = MotionController.check_device_imx708()
    cap_dir = MotionController.get_capture_dir()
    lines = [
        f"{'✅' if lc_ok  else '❌'} libcamerify : `{LIBCAMERIFY}`",
        f"{'✅' if svc_ok else '❌'} Service patché (libcamerify dans .service)",
        f"{'✅' if dev_ok else '❌'} Périphérique IMX708 : `{MOTION_DEV_IMX708}`\n",
        f"📁 Dossier  : `{cap_dir}`",
        f"🌐 Web Ctrl : `{MOTION_WEB_CTRL}`",
        f"📹 Stream   : `{MOTION_WEB_STREAM}`",
    ]
    return "\n".join(lines)

def _list_captures(capture_dir: Path) -> list[Path]:
    """Retourne les fichiers JPEG/MKV triés du plus récent au plus ancien."""
    try:
        files = [
            capture_dir / f
            for f in os.listdir(capture_dir)
            if Path(f).suffix.lower() in IMAGE_EXTS
        ]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        return []

def _list_all_captures(capture_dir: Path) -> list[Path]:
    """Tous les fichiers (sans filtre extension) — pour /effacer."""
    try:
        return [
            capture_dir / f
            for f in os.listdir(capture_dir)
            if (capture_dir / f).is_file()
        ]
    except FileNotFoundError:
        return []
        
def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} o"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} Ko"
    else:
        return f"{size_bytes / 1024**2:.1f} Mo"

# =============================================================================
#  Handlers des commandes
# =============================================================================

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(
        f"🎥 *Motion Bot – IMX708*\n"
        f"──────────────────────\n"
        f"📅 {now}\n"
        f"  {_etat_emoji()} Motion  {_alertes_emoji()} Alertes\n"
        f"  ↳ {_mode_emoji()}\n\n"
        "/status — État du service Motion\n"
        "/demarrer — ▶️ Démarrer Motion\n"
        "/arreter — ⏹ Arrêter Motion\n"
        "/rafraichir — 🔄 Redémarrer Motion\n"
        "/mode — 🎛 Changer le mode Motion\n"
        "/alertes — 🔔/🔕 On/Off des alertes\n"
        "/lister — 📂 Fichiers motion_captures\n"
        "/effacer — 🗑 Vider motion_captures\n"
        "/aide — Ce menu",
        parse_mode="Markdown",
    )

async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/mode – Affiche et permet de changer le mode Motion."""
    running    = MotionController.is_running()
    etat_txt   = "🟢 actif" if running else "🔴 arrêté"
    mode_actuel = _mode_emoji()

    keyboard = [[
        InlineKeyboardButton("🎥  Fichier.mkv",
                             callback_data="motion_mode:enregistrement"),
        InlineKeyboardButton("📡  Flux continu",
                             callback_data="motion_mode:flux"),
    ]]
    await update.message.reply_text(
        f"🎛 *Mode Motion*\n"
        f"──────────────────────\n"
        f"Statut : {etat_txt}\n"
        f"Mode actuel : {mode_actuel}\n\n"
        f"Choisissez le nouveau mode :\n"
        f"_(Motion redémarrera automatiquement s'il est actif)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    running    = MotionController.is_running()
    etat_txt   = "🟢 *Motion est actif*" if running else "🔴 *Motion est arrêté*"
    mode_txt   = f"↳ Mode : {_mode_emoji()}"
    alerte_txt = (
        f"🔔 Alertes *activées* (anti-spam : {ALERT_COOLDOWN} s)"
        if watcher.enabled else
        "🔕 Alertes *désactivées*"
    )
    diag = _build_diagnostics()

    await update.message.reply_text(
        f"📊 *Statut Motion – IMX708*\n\n"
        f"{etat_txt}\n"
        f"{mode_txt}\n"
        f"{alerte_txt}\n\n"
        f"*Diagnostics :*\n{diag}",
        parse_mode="Markdown",
    )

async def cmd_alertes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now_enabled = watcher.toggle()
    if now_enabled:
        cap_dir = MotionController.get_capture_dir()
        await update.message.reply_text(
            f"🔔 *Alertes activées.*\n"
            f"Dossier   : `{cap_dir}`\n"
            f"Anti-spam : {ALERT_COOLDOWN} s entre 2 détections.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "🔕 *Alertes désactivées.*\n"
            "Motion continue de tourner, mais aucun fichier ne sera envoyé.",
            parse_mode="Markdown",
        )

async def cmd_demarrer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if MotionController.is_running():
        await update.message.reply_text(
            "ℹ️ Motion est *déjà actif*.\n"
            "Tapez /status pour l'état détaillé.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("⏳ Démarrage de Motion en cours…")
    ok, msg = await asyncio.to_thread(MotionController.demarrer)

    if ok:
        log.info("Motion démarré via /demarrer")
        await update.message.reply_text(
            "🟢 *Motion démarré avec succès.*\n\n"
            f"📹 Stream : `{MOTION_WEB_STREAM}`\n"
            f"🌐 Ctrl : `{MOTION_WEB_CTRL}`\n\n"
            f"{_alertes_emoji()} Alertes : "
            f"{'activées' if watcher.enabled else 'désactivées'}",
            parse_mode="Markdown",
        )
    else:
        log.warning(f"Échec démarrage Motion : {msg}")
        await update.message.reply_text(
            f"❌ *Échec du démarrage.*\n\n`{_truncate(msg)}`\n\n"
            "Vérifiez les diagnostics avec /status.",
            parse_mode="Markdown",
        )

async def cmd_arreter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not MotionController.is_running():
        await update.message.reply_text(
            "ℹ️ Motion est *déjà arrêté*.",
            parse_mode="Markdown",
        )
        return

    keyboard = [[
        InlineKeyboardButton("⏹ Arrêt", callback_data="motion_stop_ok"),
        InlineKeyboardButton("❌ Annuler",           callback_data="motion_stop_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ *Arrêt de Motion*\n\n"
        "Cela coupera la détection de mouvement et le stream vidéo.\n\n"
        "👇 *Cliquez le bouton ci-dessous pour confirmer.*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def cmd_rafraichir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("🔄 Redémarrage", callback_data="motion_restart_ok"),
        InlineKeyboardButton("❌ Annuler",                  callback_data="motion_restart_cancel"),
    ]]
    await update.message.reply_text(
        f"🔄 *Redémarrage de Motion*\n\n"
        f"État actuel : {_etat_emoji()}\n\n"
        "Le service sera arrêté puis relancé.\n\n"
        "👇 *Cliquez le bouton ci-dessous pour confirmer.*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# =============================================================================
#  /lister — liste paginée des fichiers avec boutons de téléchargement
# =============================================================================

async def cmd_lister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cap_dir = MotionController.get_capture_dir()
    files   = _list_captures(cap_dir)

    if not files:
        await update.message.reply_text(
            f"📂 *motion_captures est vide.*\n`{cap_dir}`",
            parse_mode="Markdown",
        )
        return

    # Calcul taille totale
    total_size = sum(f.stat().st_size for f in files if f.exists())

    # En-tête
    await update.message.reply_text(
        f"📂 *motion_captures*\n"
        f"`{cap_dir}`\n\n"
        f"📄 {len(files)} fichier(s) — {_format_size(total_size)}\n\n"
        f"Cliquez sur un fichier pour le recevoir :",
        parse_mode="Markdown",
    )

    # Un bouton inline par fichier (par pages de FILES_PER_PAGE)
    page_files = files[:FILES_PER_PAGE]
    keyboard   = []
    for p in page_files:
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m %H:%M")
            size  = _format_size(p.stat().st_size)
            emoji = "🎥" if p.suffix.lower() == ".mkv" else "📷"
            label = f"{emoji} {p.name}  ({size}, {mtime})"
        except FileNotFoundError:
            continue
        keyboard.append([InlineKeyboardButton(label, callback_data=f"dl:{p.name}")])

    if len(files) > FILES_PER_PAGE:
        keyboard.append([
            InlineKeyboardButton(
                f"… {len(files) - FILES_PER_PAGE} fichier(s) de plus (non listés)",
                callback_data="dl_more"
            )
        ])

    await update.message.reply_text(
        "📋 *Sélectionnez un fichier :*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# =============================================================================
#  /effacer — vide le dossier motion_captures avec confirmation
# =============================================================================

async def cmd_effacer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cap_dir = MotionController.get_capture_dir()
    files   = _list_all_captures(cap_dir)
    nb      = len(files)

    if nb == 0:
        await update.message.reply_text(
            "📂 *Le dossier est déjà vide.*",
            parse_mode="Markdown",
        )
        return

    total_size = sum(f.stat().st_size for f in files if f.exists())
    keyboard   = [[
        InlineKeyboardButton(f"🗑 Effacer", callback_data="captures_clear_ok"),
        InlineKeyboardButton("❌ Annuler",                   callback_data="captures_clear_cancel"),
    ]]
    await update.message.reply_text(
        f"🗑 *Effacer motion_captures ?*\n\n"
        f"📄 {nb} fichier(s) — {_format_size(total_size)}\n\n"
        "⚠️ Cette action est *irréversible*.\n\n"
        "👇 *Cliquez le bouton ci-dessous pour confirmer.*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# =============================================================================
#  Callbacks inline
# =============================================================================

async def callback_dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # --- Téléchargement d'un fichier ---
    if data.startswith("dl:"):
        filename  = data[3:]
        cap_dir   = MotionController.get_capture_dir()
        file_path = cap_dir / filename

        if not file_path.exists():
            await query.answer("❌ Fichier introuvable.", show_alert=True)
            return

        await query.answer()

        # Attendre que Motion ait fini d'écrire le fichier (taille stable)
        stable = await asyncio.to_thread(_wait_file_stable, file_path)
        if not stable:
            await query.message.reply_text(
                f"⚠️ `{filename}` est encore en cours d'écriture par Motion.\n"
                "Patientez quelques secondes et réessayez.",
                parse_mode="Markdown",
            )
            return

        caption = (
            f"📁 `{filename}`\n"
            f"📅 {datetime.fromtimestamp(file_path.stat().st_mtime).strftime('%d/%m/%Y %H:%M:%S')}\n"
            f"💾 {_format_size(file_path.stat().st_size)}"
        )
        try:
            with open(file_path, "rb") as f:
                if file_path.suffix.lower() in {".jpg", ".jpeg"}:
                    await query.message.reply_photo(photo=f, caption=caption, parse_mode="Markdown")
                else:
                    await query.message.reply_video(video=f, caption=caption, parse_mode="Markdown")
            log.info(f"Fichier envoyé via /lister : {filename}")
        except Exception as exc:
            log.warning(f"Erreur envoi {filename} : {exc}")
            await query.message.reply_text(f"❌ Impossible d'envoyer `{filename}` : {exc}", parse_mode="Markdown")
        return

    if data == "dl_more":
        await query.answer(
            f"Seuls les {FILES_PER_PAGE} fichiers les plus récents sont listés.\n"
            "Utilisez /effacer pour libérer de l'espace.",
            show_alert=True,
        )
        return

    # --- Effacement du dossier ---
    if data == "captures_clear_cancel":
        await query.edit_message_text("❌ Effacement annulé.")
        return

    if data == "captures_clear_ok":
        cap_dir = MotionController.get_capture_dir()
        files   = _list_all_captures(cap_dir)   # ← tous les fichiers

        if not files:
            # Dossier déjà vide (effacement double-clic ou concurrent)
            await query.edit_message_text(
                "📂 *Le dossier est déjà vide* — rien à supprimer.",
                parse_mode="Markdown",
            )
            return

        nb_ok, nb_err = 0, 0
        for p in files:
            try:
                p.unlink()
                nb_ok += 1
            except Exception as exc:
                log.warning(f"Impossible de supprimer {p.name} : {exc}")
                nb_err += 1

        watcher.refresh_known(cap_dir)

        if nb_err == 0:
            await query.edit_message_text(
                f"🗑 *{nb_ok} fichier(s) supprimé(s).\n* Dossier vidé.\n\n"
                "Les prochaines détections seront de nouveau envoyées.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"⚠️ *{nb_ok} supprimé(s)*, {nb_err} erreur(s).\n"
                "Vérifiez les permissions du dossier.",
                parse_mode="Markdown",
            )
        log.info(f"Effacement captures : {nb_ok} ok, {nb_err} erreurs.")
        return

    # --- Arrêt ---
    if data == "motion_stop_cancel":
        await query.edit_message_text("❌ Arrêt annulé.")
        return

    if data == "motion_stop_ok":
        await query.edit_message_text(
            f"⏳ Arrêt en cours (délai : {MOTION_STOP_GRACE} s)…"
        )
        ok, msg = await asyncio.to_thread(MotionController.arreter_proprement)
        if ok:
            log.info("Motion arrêté via /arreter")
            await query.edit_message_text("🔴 *Motion arrêté.*", parse_mode="Markdown")
        else:
            log.warning(f"Échec arrêt Motion : {msg}")
            await query.edit_message_text(
                f"❌ *Échec de l'arrêt.*\n\n`{_truncate(msg)}`",
                parse_mode="Markdown",
            )
        return

    # --- Redémarrage ---
    if data == "motion_restart_cancel":
        await query.edit_message_text("❌ Redémarrage annulé.")
        return

    if data == "motion_restart_ok":
        await query.edit_message_text(
            f"⏳ Redémarrage en cours (arrêt {MOTION_STOP_GRACE} s + relance)…"
        )
        ok, msg = await asyncio.to_thread(MotionController.redemarrer)
        if ok:
            log.info("Motion redémarré via /rafraichir")
            await query.edit_message_text(
                "🟢 *Motion redémarré avec succès.*\n\n"
                f"📹 Stream : `{MOTION_WEB_STREAM}`\n"
                f"🌐 Ctrl : `{MOTION_WEB_CTRL}`",
                parse_mode="Markdown",
            )
        else:
            log.warning(f"Échec redémarrage Motion : {msg}")
            await query.edit_message_text(
                f"❌ *Échec du redémarrage.*\n\n`{_truncate(msg)}`\n\n"
                "Vérifiez les diagnostics avec /status.",
                parse_mode="Markdown",
            )
        return

    # --- Changement de mode Motion ---
    if data.startswith("motion_mode:"):
        nouveau_mode  = data[len("motion_mode:"):]
        activer_enreg = (nouveau_mode == "enregistrement")
        ok = await asyncio.to_thread(MotionController.set_movie_output, activer_enreg)
        if not ok:
            await query.edit_message_text(
                "❌ Impossible de modifier motion.conf — vérifiez les permissions sudo."
            )
            return
        mode_txt = ("🎥 Enregistrement .mkv"
                    if activer_enreg else "📡 Flux continu")
        running = await asyncio.to_thread(MotionController.is_running)
        if running:
            await query.edit_message_text(
                f"⏳ Mode → *{mode_txt}*\nRedémarrage de Motion en cours…",
                parse_mode="Markdown",
            )
            ok2, _ = await asyncio.to_thread(MotionController.redemarrer)
            etat = "🟢 actif" if ok2 else "🔴 erreur au redémarrage"
        else:
            etat = "🔴 arrêté (mode appliqué au prochain démarrage)"
        await query.edit_message_text(
            f"✅ *Mode Motion mis à jour*\n"
            f"Mode : {mode_txt}\n"
            f"Motion : {etat}",
            parse_mode="Markdown",
        )
        return

    log.warning(f"callback_dispatch : data inconnu reçu → '{data}'")
    await query.edit_message_text("⚠️ Action inconnue.")

async def cmd_inconnue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_aide(update, ctx)

# =============================================================================
#  Attente synchronisation NTP
# =============================================================================

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

# =============================================================================
#  Démarrage
# =============================================================================

def main():
    token = load_token()
    app   = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("aide",       cmd_aide))
    app.add_handler(CommandHandler("start",      cmd_aide))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("alertes",    cmd_alertes))
    app.add_handler(CommandHandler("demarrer",   cmd_demarrer))
    app.add_handler(CommandHandler("arreter",    cmd_arreter))
    app.add_handler(CommandHandler("rafraichir", cmd_rafraichir))
    app.add_handler(CommandHandler("mode",       cmd_mode))
    app.add_handler(CommandHandler("lister",     cmd_lister))
    app.add_handler(CommandHandler("effacer",    cmd_effacer))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_inconnue))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_inconnue))
    app.add_handler(CallbackQueryHandler(callback_dispatch))

    async def _on_start(app_ref):
        ntp_ok   = await _attendre_ntp(timeout=60)
        now      = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ntp_avert = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
        admin_id = _get_admin_chat_id()
        cap_dir  = MotionController.get_capture_dir()

        if admin_id:
            try:
                await app_ref.bot.send_message(
                    admin_id,
                    f"🎥 *Bot Motion démarré*\n"
                    f"📅 {now}{ntp_avert}\n\n"
                    f"État Motion : {_etat_emoji()}\n"
                    f"↳ Mode : {_mode_emoji()}\n"
                    f"Alertes : {_alertes_emoji()} activées\n"
                    f"Dossier : `{cap_dir}`\n\n"
                    "Tapez /aide pour les commandes.",
                    parse_mode="Markdown",
                )
                log.info(f"Message de démarrage envoyé — chat_id {admin_id}")
            except Exception as exc:
                log.warning(f"Message de démarrage impossible : {exc}")

            asyncio.create_task(
                watcher.watch_loop(app_ref.bot, admin_id, cap_dir)
            )
            log.info(f"CaptureWatcher lancé sur {cap_dir}")

            asyncio.create_task(
                watchdog.watch_loop(app_ref.bot, admin_id)
            )
            log.info(
                f"MotionWatchdog lancé "
                f"(max freeze : {WATCHDOG_MAX_FREEZE} s, "
                f"intervalle : {WATCHDOG_INTERVAL} s)"
            )
        else:
            log.warning(
                "chat_id absent de ~/.telegram_config — "
                "le CaptureWatcher NE sera PAS lancé et aucune alerte ne sera envoyée. "
                "Ajoutez : chat_id = <votre_chat_id>"
            )

    app.post_init = _on_start
    log.info("Motion Bot IMX708 démarré.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
