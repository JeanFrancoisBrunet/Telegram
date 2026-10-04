#!/usr/bin/env python3
# =============================================================================
#  Bot Telegram — Menu Pi5 (Bot Maître)
#  Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe
#
#  Gestion On/Off des 7 bots via boutons inline Telegram :
#    • Caméra IMX500      → telegram-bot-imx500.service
#    • Caméra IMX708      → telegram-bot-cam.service
#    • Motion IMX708      → telegram-bot-motion.service
#    • LLM Llama          → telegram-bot-llm.service
#    • Agent Groq         → telegram-bot-groq.service
#    • Suivi Bourse       → telegram-bot-bourse.service
#    • Ctrl & Sécurité    → telegram-bot-ctrl.service
#
#  Commandes :
#    /aide    Menu des commandes
#    /menu    Tableau de bord On/Off des bots
#    /status  Statut de tous les services
#
#  Configuration — ~/.telegram_config :
#      [telegram]
#      token_menu = VOTRE_TOKEN
#      chat_id    = VOTRE_CHAT_ID
#
#  autorisation chat_id, asyncio non-bloquant, gather parallèle,
#  debounce par verrou, polling d'état post-action...
#
#  Auteur : Jean-François BRUNET – JFBConseils – Juin 2026
# =============================================================================

import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =============================================================================
#  CONFIGURATION
# =============================================================================

CONFIG_FILE = Path.home() / ".telegram_config"
TOKEN_KEY   = "token_menu"

# Délai maximum (s) pour attendre qu'un service change d'état après action
SERVICE_WAIT_TIMEOUT = 5

# Correspondance nom affiché → nom du service systemd
BOTS: dict[str, dict] = {
    "imx500": {
        "label":   "Caméra IMX500  -",
        "service": "telegram-bot-imx500.service",
    },
    "imx708": {
        "label":   "Caméra IMX708  -",
        "service": "telegram-bot-cam.service",
    },
    "motion": {
        "label":   "Motion IMX708  -",
        "service": "telegram-bot-motion.service",
    },
    "llm": {
        "label":   "LLM Llama          -",
        "service": "telegram-bot-llm.service",
    },
    "groq": {
        "label":   "Agent Groq         -",
        "service": "telegram-bot-groq.service",
    },
    "bourse": {
        "label":   "Suivi Bourse       -",
        "service": "telegram-bot-bourse.service",
    },
    "ctrl": {
        "label":   "Ctrl & Sécurité   -",
        "service": "telegram-bot-ctrl.service",
    },
}

# Un verrou asyncio par bot — empêche les double-clics simultanés
_locks: dict[str, asyncio.Lock] = {}   # initialisé dans main()

# chat_id administrateur chargé une seule fois au démarrage
_admin_chat_id: int | None = None

# =============================================================================
#  Logging — sortie console (récupérée par journalctl)
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
#  Sécurité — filtre d'autorisation
# =============================================================================

async def _est_autorise(update: Update) -> bool:
    """Retourne True si le message provient du chat_id administrateur.
    Si aucun chat_id n'est configuré, laisse passer (mode non sécurisé)."""
    if _admin_chat_id is None:
        log.warning("Aucun chat_id configuré — accès non filtré.")
        return True
    uid = update.effective_chat.id
    if uid != _admin_chat_id:
        log.warning(f"Accès refusé — chat_id non autorisé : {uid}")
        return False
    return True

# =============================================================================
#  Helpers systemd — version async (non bloquante)
# =============================================================================

def _run_sync(cmd: list[str], use_sudo: bool = False, timeout: int = 15) -> tuple[str, bool]:
    """Exécution synchrone de commande — à appeler via run_in_executor."""
    try:
        full_cmd = (["sudo", "-n"] + cmd) if use_sudo else cmd
        result = subprocess.run(
            full_cmd,
            capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout + result.stderr).strip()
        return output, result.returncode == 0
    except subprocess.TimeoutExpired:
        return f"⏱ Timeout ({timeout}s).", False
    except Exception as exc:
        return f"Erreur : {exc}", False

async def _run(cmd: list[str], use_sudo: bool = False, timeout: int = 15) -> tuple[str, bool]:
    """Version asynchrone de _run_sync — ne bloque pas la boucle d'événements."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_sync, cmd, use_sudo, timeout)

def _is_active_sync(service: str) -> bool:
    out, _ = _run_sync(["systemctl", "is-active", service])
    return out.strip() == "active"

def _is_enabled_sync(service: str) -> bool:
    out, _ = _run_sync(["systemctl", "is-enabled", service])
    return out.strip() == "enabled"

async def _gather_states() -> dict[str, bool]:
    """Interroge tous les services en parallèle via asyncio.gather().
    Retourne {key: is_active} pour chaque bot."""
    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(None, _is_active_sync, info["service"])
        for info in BOTS.values()
    ]
    results = await asyncio.gather(*tasks)
    return dict(zip(BOTS.keys(), results))

async def _attendre_etat(service: str, etat_cible: bool, timeout: int = SERVICE_WAIT_TIMEOUT) -> bool:
    """Attend (en polling) que le service atteigne l'état cible (active/inactive).
    Plus fiable qu'un sleep fixe."""
    loop = asyncio.get_running_loop()
    for _ in range(timeout * 2):   # check toutes les 0.5s
        actif = await loop.run_in_executor(None, _is_active_sync, service)
        if actif == etat_cible:
            return True
        await asyncio.sleep(0.5)
    return False

# =============================================================================
#  Construction du clavier inline
# =============================================================================

async def _build_keyboard() -> InlineKeyboardMarkup:
    states = await _gather_states()
    keyboard = []
    for key, info in BOTS.items():
        actif  = states[key]
        badge  = "🟢" if actif else "🔴"
        action = "stop" if actif else "start"
        label  = f"{badge} {info['label']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{action}:{key}")])

    keyboard.append([InlineKeyboardButton("🔄 Rafraîchir", callback_data="refresh")])
    return InlineKeyboardMarkup(keyboard)

def _menu_header() -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    return (
        f"🤖 *Menu Pi5 — Gestion des Bots*\n"
        f"──────────────────────\n"
        f"📅 {now}\n\n"
        "Appuyez sur un bouton pour démarrer ou arrêter un Bot :"
    )

# =============================================================================
#  Handlers commandes
# =============================================================================

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        return
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(
        f"🤖 *Bot On/Off - Telegram & Pi5*\n"
        f"──────────────────────\n"
        f"📅 {now}\n\n"
        "/menu   — 🎛 Tableau de bord On/Off\n"
        "/status — 📋 Statut des services\n"
        "/aide   — ❓ Ce menu\n",
        parse_mode="Markdown"
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche le tableau de bord On/Off."""
    if not await _est_autorise(update):
        return
    await update.message.reply_text(
        _menu_header(),
        parse_mode="Markdown",
        reply_markup=await _build_keyboard()
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche le statut détaillé de chaque service (états en parallèle)."""
    if not await _est_autorise(update):
        return

    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    lines = [f"📋 *Statut des services — Pi5*\n📅 {now}\n"]

    loop = asyncio.get_running_loop()

    # Récupération active + enabled en parallèle pour tous les bots
    tasks_active  = [loop.run_in_executor(None, _is_active_sync,  info["service"]) for info in BOTS.values()]
    tasks_enabled = [loop.run_in_executor(None, _is_enabled_sync, info["service"]) for info in BOTS.values()]
    actifs, enableds = await asyncio.gather(
        asyncio.gather(*tasks_active),
        asyncio.gather(*tasks_enabled),
    )

    for (key, info), actif, enabled in zip(BOTS.items(), actifs, enableds):
        ico_run = "🟢" if actif   else "🔴"
        ico_ena = "✅" if enabled else "⛔"
        lines.append(
            f"{ico_run} {info['label']}\n"
            f"   Actif : {'oui' if actif else 'non'}  |  "
            f"Auto-Boot : {ico_ena} {'activé' if enabled else 'désactivé'}"
        )

    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown"
    )

async def cmd_inconnue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        return
    await cmd_aide(update, ctx)

# =============================================================================
#  Callback boutons inline
# =============================================================================

async def callback_bouton(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Vérification d'autorisation sur le callback (avant query.answer)
    if not await _est_autorise(update):
        await query.answer("⛔ Accès refusé.", show_alert=True)
        return

    await query.answer()

    data = query.data

    # --- Rafraîchissement simple ---
    if data == "refresh":
        try:
            await query.edit_message_text(
                _menu_header(),
                parse_mode="Markdown",
                reply_markup=await _build_keyboard()
            )
        except Exception:
            pass  # Message identique → Telegram lève une exception ignorée
        return

    # --- Action start / stop ---
    if ":" not in data:
        log.warning(f"callback inattendu : '{data}'")
        return

    action, key = data.split(":", 1)
    if key not in BOTS:
        log.warning(f"clé de bot inconnue : '{key}'")
        return

    info    = BOTS[key]
    service = info["service"]
    label   = info["label"]

    # --- Verrou anti-double-clic ---
    if _locks[key].locked():
        log.info(f"Action ignorée — verrou actif pour '{key}'")
        await query.answer("⏳ Action déjà en cours…", show_alert=False)
        return

    async with _locks[key]:
        if action == "start":
            out, ok = await _run(["systemctl", "start", service], use_sudo=True)
            etat_cible = True
            verb       = "démarré"
            emoji      = "🟢" if ok else "❌"
        elif action == "stop":
            out, ok = await _run(["systemctl", "stop", service], use_sudo=True)
            etat_cible = False
            verb       = "arrêté"
            emoji      = "🔴" if ok else "❌"
        else:
            log.warning(f"action inconnue : '{action}'")
            return

        if ok:
            # Polling jusqu'à ce que l'état soit effectif (max SERVICE_WAIT_TIMEOUT s)
            stable = await _attendre_etat(service, etat_cible)
            if not stable:
                log.warning(f"Service {service} n'a pas atteint l'état cible dans le délai imparti.")
            log.info(f"Service {service} {verb}.")
            notification = f"{emoji} *{label}* {verb}."
        else:
            log.error(f"Échec {action} {service} : {out}")
            notification = f"❌ Échec {action} *{label}* :\n`{out}`"

        # Mise à jour du clavier avec le nouvel état
        try:
            await query.edit_message_text(
                _menu_header() + f"\n\n{notification}",
                parse_mode="Markdown",
                reply_markup=await _build_keyboard()
            )
        except Exception as exc:
            log.warning(f"edit_message_text : {exc}")

# =============================================================================
#  Attente synchronisation NTP
# =============================================================================

async def _attendre_ntp(timeout: int = 60) -> bool:
    """Attend que l'horloge système soit synchronisée NTP."""
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
    global _admin_chat_id

    token          = load_token()
    _admin_chat_id = _get_admin_chat_id()

    # Initialisation des verrous (doit se faire dans le thread principal)
    for key in BOTS:
        _locks[key] = asyncio.Lock()

    if _admin_chat_id:
        log.info(f"Accès restreint au chat_id : {_admin_chat_id}")
    else:
        log.warning(
            "Aucun chat_id configuré dans ~/.telegram_config — "
            "le bot répond à n'importe quel utilisateur."
        )

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("aide",   cmd_aide))
    app.add_handler(CommandHandler("start",  cmd_menu))   # /start → menu directement
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_inconnue))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_inconnue))
    app.add_handler(CallbackQueryHandler(callback_bouton))

    async def _on_start(app_ref):
        ntp_ok    = await _attendre_ntp(timeout=60)
        now       = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ntp_avert = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
        if _admin_chat_id:
            try:
                await app_ref.bot.send_message(
                    _admin_chat_id,
                    f"🤖 *Bot Menu Pi5 démarré*\n"
                    f"📅 {now}{ntp_avert}\n\n"
                    f"Activation/Désactivation des Bots.\n"
                    f"Tapez /menu pour gérer les Bots.\n"
                    f"           /aide pour les options.",
                    parse_mode="Markdown"
                )
                # Envoi automatique du menu On/Off après le message de démarrage
                await app_ref.bot.send_message(
                    _admin_chat_id,
                    _menu_header(),
                    parse_mode="Markdown",
                    reply_markup=await _build_keyboard()
                )
                log.info(f"Message de démarrage + menu envoyés — chat_id {_admin_chat_id}")
            except Exception as exc:
                log.warning(f"Message de démarrage impossible : {exc}")
        else:
            log.warning(
                "chat_id absent de ~/.telegram_config — "
                "message de démarrage non envoyé. "
                "Ajoutez : chat_id = <votre_chat_id>"
            )
    app.post_init = _on_start
    log.info("Bot Telegram Menu Pi5 démarré")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
