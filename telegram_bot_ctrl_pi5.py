#!/usr/bin/env python3
# =============================================================================
#  Bot Telegram — Ctrl & Sécurité (Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe)
#
#  Commandes :
#    /aide         Menu des commandes
#    /status       Température, CPU, RAM, uptime
#    /reseau       Interfaces et adresses IP (format lisible)
#    /scan_reseau  Appareils présents sur le réseau local (nmap)
#    /disque       Espace disque (partitions réelles uniquement)
#    /fail2ban     Bans actifs + tentatives récentes
#    /ufw          Statut et règles du pare-feu
#    /ufw_toggle   Active ou désactive UFW (confirmation requise)
#    /scan         Scan ClamAV (base maintenue par clamav-freshclam)
#    /stopscan     Interrompt un scan ClamAV en cours
#    /wireguard    Statut du VPN WireGuard
#    /vpn          Active ou désactive WireGuard wg0 (confirmation requise)
#    /nettoyage    apt autoremove/clean + /tmp
#    /terminal     Exécute une commande shell (sans sudo, avec garde-fous)
#    /reboot       Redémarrage du Pi5 (confirmation requise)
#    /shutdown     Arrêt propre du Pi5 (confirmation requise)
#
#  Alertes automatiques :
#    - Température CPU > 75 °C
#    - IP bannie par Fail2Ban
#    - Espace disque < 10 %
#    - Connexion SSH suspecte (via journald)
#    - scan ClamAV non-bloquant via asyncio.create_task
#
#  Prérequis (à installer) :
#    sudo apt install clamav clamav-daemon fail2ban ufw nmap -y
#    pip install python-telegram-bot psutil --break-system-packages
#
#  Configuration — ~/.telegram_config :
#      [telegram]
#      token_ctrl = VOTRE_TOKEN
#      chat_id    = VOTRE_CHAT_ID
#
#  Auteur : Jean-François BRUNET – JFBConseils – Mai 2026
# =============================================================================

import asyncio
import os
import re
import time
import logging
import subprocess
import ipaddress
from pathlib import Path
from datetime import datetime

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# =============================================================================
#  CONFIGURATION
# =============================================================================

CONFIG_FILE    = Path.home() / ".telegram_config"
TOKEN_KEY      = "token_ctrl"
SCAN_PATH      = "/home/jfbrunet"
TEMP_FILE      = Path("/sys/class/thermal/thermal_zone0/temp")

TEMP_ALERT_C   = 75.0
DISK_ALERT_PCT = 10
ALERT_INTERVAL = 300    # anti-spam : 5 min entre deux alertes identiques
WATCH_INTERVAL = 60     # fréquence de la boucle de surveillance (secondes)
SCAN_TIMEOUT   = 7200   # 120 min — marge pour clamscan

# Mots-clés bloqués dans /terminal (commandes dangereuses)
TERMINAL_BLOCKLIST = [
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{",      # fork bomb
    r"\bchmod\s+777\b",
    r"\bchown\b.*root",
    r"\bsudo\b",        # pas de sudo via /terminal
    r"\bsu\b\s",
    r"\bpasswd\b",
    r"\bvisudo\b",
    r"\bcrontab\b",
    r">\s*/dev/sd",     # écriture directe sur disque
    r"\bwget\b.*\|\s*(bash|sh)",  # téléchargement+exec
    r"\bcurl\b.*\|\s*(bash|sh)",
]

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
#  Helpers système
# =============================================================================

def _run(cmd: list[str], use_sudo: bool = False, timeout: int = 60) -> tuple[str, bool]:
    try:
        full_cmd = (["sudo", "-n"] + cmd) if use_sudo else cmd
        result = subprocess.run(
            full_cmd,
            capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout + result.stderr).strip()
        return output, result.returncode == 0
    except subprocess.TimeoutExpired:
        return f"⏱ Commande interrompue (timeout {timeout} s).", False
    except Exception as exc:
        return f"Erreur : {exc}", False

def _temperature() -> float | None:
    try:
        return int(TEMP_FILE.read_text().strip()) / 1000
    except Exception:
        return None

def _disk_min_free_pct() -> float:
    try:
        result = subprocess.run(
            ["df", "--output=pcent,target", "--exclude-type=tmpfs",
             "--exclude-type=devtmpfs", "--exclude-type=squashfs"],
            capture_output=True, text=True
        )
        min_free = 100.0
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if parts:
                pct_used = float(parts[0].replace("%", ""))
                min_free = min(min_free, 100 - pct_used)
        return min_free
    except Exception:
        return 100.0

def _format_uptime() -> str:
    if HAS_PSUTIL:
        uptime_s = int(time.time() - psutil.boot_time())
        d, r = divmod(uptime_s, 86400)
        h, r = divmod(r, 3600)
        m, _ = divmod(r, 60)
        parts = []
        if d:
            parts.append(f"{d}j")
        parts.append(f"{h}h {m}m")
        return " ".join(parts)
    else:
        out, _ = _run(["uptime", "-p"])
        return out

def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[tronqué]"

def _terminal_is_safe(cmd: str) -> tuple[bool, str]:
    """Vérifie qu'une commande /terminal ne contient pas de motif dangereux.
    Retourne (True, "") si OK, (False, raison) si bloqué."""
    for pattern in TERMINAL_BLOCKLIST:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False, pattern
    return True, ""

# =============================================================================
#  Surveillance SSH via journald
# =============================================================================

class AuthLogWatcher:
    """Surveille les tentatives SSH suspectes via journald."""

    SUSPICIOUS_PATTERNS = [
        re.compile(r"Failed password for"),
        re.compile(r"Invalid user"),
        re.compile(r"authentication failure"),
        re.compile(r"POSSIBLE BREAK-IN ATTEMPT"),
    ]

    def new_suspicious_lines(self) -> list[str]:
        try:
            result = subprocess.run(
                ["journalctl", "-u", "ssh", "--since", "1 minute ago",
                 "--no-pager", "--output", "short"],
                capture_output=True, text=True, timeout=10
            )
            suspicious = []
            for line in result.stdout.splitlines():
                for pattern in self.SUSPICIOUS_PATTERNS:
                    if pattern.search(line):
                        suspicious.append(line.strip())
                        break
            return suspicious
        except Exception:
            return []

# =============================================================================
#  Gestionnaire d'alertes (anti-spam)
# =============================================================================

class AlertManager:
    def __init__(self):
        self._last_sent: dict[str, float] = {}

    def should_send(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._last_sent.get(key, 0) >= ALERT_INTERVAL:
            self._last_sent[key] = now
            return True
        return False

# =============================================================================
#  Handlers des commandes
# =============================================================================

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(
        f"🤖 *Bot Contrôle Pi5*\n"
        f"──────────────────────\n"
        f"📅 {now}\n\n"
        "/status — 🌡 CPU, RAM, uptime\n"
        "/reseau — Interfaces réseau\n"
        "/scan\\_reseau — Appareils sur réseau\n"
        "/disque — Espace disque\n"
        "/fail2ban — Bans actifs & intrusions\n"
        "/ufw — Statut pare-feu UFW\n"
        "/ufw\\_toggle — ⚠️ On/Off UFW\n"
        "/scan — Scan ClamAV (~1 H)\n"
        "/stopscan — Arrêt Scan en cours\n"
        "/wireguard — Statut VPN\n"
        "/vpn — ⚠️ On/Off WireGuard\n"
        "/nettoyage — Nettoyage système\n"
        "/terminal — Cde shell (sans sudo)\n"
        "/reboot — ⚠️ Redémarrage du Pi5\n"
        "/shutdown — ⚠️ Arrêt du Pi5\n",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Collecte des informations système…")

    lines = ["📊 *Statut Système – Pi5*\n"]

    temp = _temperature()
    if temp is not None:
        emoji = "🔴" if temp > 75 else "🟠" if temp > 65 else "🟢"
        lines.append(f"{emoji} 🌡 : *{temp:.1f} °C*")
    else:
        lines.append("🌡 Température : N/A")

    if HAS_PSUTIL:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        la  = psutil.getloadavg()
        lines.append(
            f"⚙️ CPU : *{cpu:.1f}%*\n"
            f"Charge 1/5/15 min : {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}\n"
        )
        lines.append(
            f"🧠 RAM : *{mem.used // (1024**2)} Mo / {mem.total // (1024**2)} Mo* ({mem.percent:.0f}%)"
        )
    else:
        out, _ = _run(["free", "-m"])
        lines.append(f"🧠 RAM :\n`{out}`")

    lines.append(f"⏱ Uptime : *{_format_uptime()}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_reseau(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche les interfaces réseau de façon lisible (IPv4 puis IPv6)."""
    addr4, _ = _run(["ip", "-o", "-f", "inet",  "addr", "show"])
    addr6, _ = _run(["ip", "-o", "-f", "inet6", "addr", "show"])
    # État des interfaces
    link_out, _ = _run(["ip", "-o", "link", "show"])

    iface_state: dict[str, str] = {}
    for line in link_out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[1].rstrip(":").split("@")[0]
            state = "UP" if "state UP" in line else "DOWN"
            iface_state[name] = state

    # --- Bloc IPv4 ---
    lines = ["🌐 *Interfaces réseau — IPv4*"]
    found = False
    for line in addr4.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            iface = parts[1]
            addr  = parts[3]
            state = iface_state.get(iface, "?")
            icon  = "🟢" if state == "UP" else "🔴"
            lines.append(f"{icon} `{iface}` → `{addr}`")
            found = True
    if not found:
        lines.append("❌ Aucune adresse IPv4 détectée.")

    # --- Bloc IPv6 ---
    lines.append("\n🌐 *Interfaces réseau — IPv6*")
    found6 = False
    for line in addr6.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            iface = parts[1]
            addr  = parts[3]
            # Ignorer les adresses de loopback
            if addr.startswith("::1"):
                continue
            state = iface_state.get(iface, "?")
            icon  = "🟢" if state == "UP" else "🔴"
            lines.append(f"{icon} `{iface}` → `{addr}`")
            found6 = True
    if not found6:
        lines.append("❌ Aucune adresse IPv6 détectée.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_disque(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche l'espace disque — partitions réelles uniquement."""
    out, ok = _run([
        "df", "-h",
        "--exclude-type=tmpfs",
        "--exclude-type=devtmpfs",
        "--exclude-type=squashfs",
    ])
    free_pct = _disk_min_free_pct()
    emoji = "💾" if free_pct > 10 else "⚠️"
    await update.message.reply_text(
        f"{emoji} *Espace disque* (partitions principales)\n\n`{_truncate(out)}`",
        parse_mode="Markdown"
    )

async def cmd_fail2ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["🛡 *Fail2Ban*\n"]

    svc, _ = _run(["systemctl", "is-active", "fail2ban"])
    if svc.strip() != "active":
        lines.append(f"❌ Service fail2ban : {svc.strip()}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    lines.append("✅ Service : actif\n")

    jails_out, ok = _run(["fail2ban-client", "status"], use_sudo=True)
    jail_names = []
    for line in jails_out.splitlines():
        if "Jail list:" in line:
            jail_names = [j.strip() for j in line.split(":", 1)[1].split(",") if j.strip()]

    if not jail_names:
        lines.append("⚠️ Aucune jail active détectée.")
    else:
        for jail in jail_names:
            jail_status, ok = _run(["fail2ban-client", "status", jail], use_sudo=True)
            if ok:
                total_banned = ""
                banned_ips   = ""
                for l in jail_status.splitlines():
                    if "Total banned:" in l:
                        total_banned = l.split(":", 1)[1].strip()
                    if "Banned IP list:" in l:
                        banned_ips = l.split(":", 1)[1].strip()
                ips_display = banned_ips if banned_ips else "aucune"
                lines.append(
                    f"🔒 Jail *{jail}* — {total_banned} ban(s) total\n"
                    f"   IPs actives : `{ips_display}`"
                )

    journal_out, _ = _run(
        ["journalctl", "-u", "ssh", "--since", "24 hours ago",
         "--no-pager", "--output", "short"]
    )
    recent = [
        l for l in journal_out.splitlines()
        if re.search(r"Failed|Invalid user|break-in", l, re.IGNORECASE)
    ][-15:]

    if recent:
        recent_joined = "\n".join(recent)
        lines.append(f"\n🔍 *15 dernières tentatives SSH (24h)* :\n`{_truncate(recent_joined, 1500)}`")
    else:
        lines.append("\n✅ Aucune tentative suspecte SSH dans les dernières 24h.")

    await update.message.reply_text(
        _truncate("\n".join(lines), 4000),
        parse_mode="Markdown"
    )

async def cmd_ufw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    out, ok = _run(["ufw", "status", "verbose"], use_sudo=True)
    emoji = "🔒" if ok else "❌"
    await update.message.reply_text(
        f"{emoji} *UFW – Pare-feu*\n\n`{_truncate(out)}`",
        parse_mode="Markdown"
    )

async def cmd_ufw_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche le statut UFW actuel et propose d'activer ou désactiver."""
    out, _ = _run(["ufw", "status"], use_sudo=True)
    actif = "Status: active" in out

    if actif:
        statut_txt  = "🔒 UFW est actuellement *actif*."
        action_btn  = "🔓 Désactiver UFW"
        callback    = "ufw_disable"
        warning_txt = "⚠️ Désactiver UFW expose le Pi sur le réseau."
    else:
        statut_txt  = "🔓 UFW est actuellement *inactif*."
        action_btn  = "🔒 Activer UFW"
        callback    = "ufw_enable"
        warning_txt = "ℹ️ L'activation UFW appliquera les règles existantes."

    keyboard = [[
        InlineKeyboardButton(action_btn,   callback_data=callback),
        InlineKeyboardButton("❌ Annuler", callback_data="ufw_cancel"),
    ]]
    await update.message.reply_text(
        f"🛡 *Pare-feu UFW*\n\n"
        f"{statut_txt}\n\n"
        f"{warning_txt}\n\n"
        f"Confirmez l'action ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_wireguard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    out, ok = _run(["wg", "show"], use_sudo=True)
    if not out:
        out = "(aucune interface WireGuard active)"
    svc, _ = _run(["systemctl", "is-active", "wg-quick@wg0"])
    emoji = "🟢" if svc.strip() == "active" else "🔴"
    await update.message.reply_text(
        f"{emoji} *WireGuard VPN* (wg0 : {svc.strip()})\n\n`{_truncate(out)}`",
        parse_mode="Markdown"
    )

async def cmd_vpn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche le statut WireGuard et propose d'activer ou désactiver."""
    svc, _ = _run(["systemctl", "is-active", "wg-quick@wg0"])
    actif = svc.strip() == "active"

    if actif:
        statut_txt = "🟢 WireGuard est actuellement *actif*."
        action_btn = "🔴 Désactiver VPN"
        callback   = "vpn_stop"
    else:
        statut_txt = "🔴 WireGuard est actuellement *inactif*."
        action_btn = "🟢 Activer VPN"
        callback   = "vpn_start"

    keyboard = [[
        InlineKeyboardButton(action_btn,   callback_data=callback),
        InlineKeyboardButton("❌ Annuler", callback_data="vpn_cancel"),
    ]]
    await update.message.reply_text(
        f"🔐 *WireGuard VPN*\n\n"
        f"{statut_txt}\n\n"
        f"⚠️ Désactiver le VPN coupe les connexions distantes via wg0.\n"
        f"Confirmez l'action ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =============================================================================
#  Scan ClamAV — tâche asyncio non-bloquante
# =============================================================================

async def _scan_task(bot, chat_id: int, bot_data: dict):
    """Tâche asyncio indépendante — le bot reste disponible pendant toute la durée du scan."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "clamscan", "-r", "--infected", SCAN_PATH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        bot_data["scan_proc"] = proc
        log.info(f"clamscan lancé — PID {proc.pid}")

        await bot.send_message(
            chat_id,
            f"✅ *clamscan démarré* (PID {proc.pid})\n"
            "Le résultat arrivera automatiquement ici.",
            parse_mode="Markdown"
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SCAN_TIMEOUT
            )
            out = (stdout.decode() + stderr.decode()).strip()
            rc  = proc.returncode

            if rc in (-15, -9, None):
                # Process tué par /stopscan (SIGTERM = -15)
                await bot.send_message(chat_id, "🛑 Scan interrompu par /stopscan.")
            elif rc == 1:
                await bot.send_message(
                    chat_id,
                    f"🚨 *ClamAV — VIRUS DÉTECTÉ !*\n\n`{_truncate(out, 3500)}`",
                    parse_mode="Markdown"
                )
            elif rc == 0:
                await bot.send_message(
                    chat_id,
                    f"✅ *ClamAV — Aucune menace détectée.*\n\n`{_truncate(out, 3500)}`",
                    parse_mode="Markdown"
                )
            else:
                await bot.send_message(
                    chat_id,
                    f"⚠️ *ClamAV — Scan terminé (code {rc}).*\n\n`{_truncate(out, 3500)}`",
                    parse_mode="Markdown"
                )

        except asyncio.TimeoutError:
            proc.terminate()
            await bot.send_message(
                chat_id,
                f"⏱ Scan interrompu — timeout {SCAN_TIMEOUT // 60} min dépassé."
            )

    except Exception as exc:
        log.error(f"Erreur dans _scan_task : {exc}")
        try:
            await bot.send_message(chat_id, f"❌ Erreur scan : {exc}")
        except Exception:
            pass
    finally:
        bot_data["scan_en_cours"] = False
        bot_data["scan_proc"]     = None
        bot_data["scan_task"]     = None
        log.info("Tâche clamscan terminée, état réinitialisé.")

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Lance le scan ClamAV dans une tâche asyncio séparée."""
    if ctx.bot_data.get("scan_en_cours"):
        await update.message.reply_text(
            "⚠️ Un scan est déjà en cours.\n"
            "Tapez /stopscan pour l'interrompre."
        )
        return

    ctx.bot_data["scan_en_cours"] = True
    ctx.bot_data["scan_proc"]     = None
    ctx.bot_data["scan_task"]     = None

    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    await update.message.reply_text(
        f"🔍 *Scan ClamAV de* `{SCAN_PATH}`\n"
        f"📅 {now}\n\n"
        "⏳ Mise à jour de la base virale en cours…",
        parse_mode="Markdown"
    )

    # Vérifier si clamav-freshclam tourne (il monopolise le verrou du log)
    svc_status, _ = _run(["systemctl", "is-active", "clamav-freshclam"])
    service_was_active = svc_status.strip() == "active"

    if service_was_active:
        # Arrêt temporaire pour libérer le verrou
        _run(["systemctl", "stop", "clamav-freshclam"], use_sudo=True)

    # Mise à jour de la base virale
    fresh_out, fresh_ok = _run(["freshclam"], use_sudo=True, timeout=120)

    if service_was_active:
        # Redémarrage du service après la mise à jour
        _run(["systemctl", "start", "clamav-freshclam"], use_sudo=True)

    # Interprétation du résultat
    if fresh_ok:
        fresh_status = "✅ Base virale mise à jour avec succès."
    else:
        out_lower = fresh_out.lower()
        if "up to date" in out_lower or "à jour" in out_lower:
            fresh_status = "✅ Base virale déjà à jour."
        else:
            fresh_status = f"⚠️ Mise à jour partielle ou échec :\n`{_truncate(fresh_out, 300)}`"

    await update.message.reply_text(
        f"{fresh_status}\n\n"
        "🔍 Lancement du scan…\n"
        "Résultat envoyé en fin de scan — comptez 45 à 120 minutes.\n"
        "Le bot reste disponible. Tapez /stopscan pour interrompre.",
        parse_mode="Markdown"
    )

    task = asyncio.create_task(
        _scan_task(ctx.bot, update.effective_chat.id, ctx.bot_data)
    )
    ctx.bot_data["scan_task"] = task

async def cmd_stopscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Interrompt un scan ClamAV en cours."""
    proc = ctx.bot_data.get("scan_proc")
    if not ctx.bot_data.get("scan_en_cours") or proc is None:
        await update.message.reply_text("ℹ️ Aucun scan en cours.")
        return
    try:
        proc.terminate()
        await update.message.reply_text(
            "🛑 *Signal d'arrêt envoyé à clamscan.*\n"
            "Le résultat partiel sera affiché dans quelques secondes.",
            parse_mode="Markdown"
        )
        log.info("clamscan interrompu via /stopscan")
    except Exception as exc:
        await update.message.reply_text(f"❌ Impossible d'arrêter le scan : {exc}")

# =============================================================================
#  Scan réseau
# =============================================================================

async def cmd_scan_reseau(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Découverte des appareils actifs sur le réseau local via nmap -sn."""
    await update.message.reply_text("📡 Scan du réseau local en cours (attendre quelques secondes)…")

    iface_out, _ = _run(["ip", "-o", "-f", "inet", "addr", "show"])
    subnet = None
    for line in iface_out.splitlines():
        if any(iface in line for iface in ("eth0", "wlan0", "end0")):
            for p in line.split():
                if "/" in p and not p.startswith("127"):
                    try:
                        subnet = str(ipaddress.ip_interface(p).network)
                    except ValueError:
                        pass
                    break
        if subnet:
            break

    if not subnet:
        await update.message.reply_text(
            "❌ Impossible de détecter le sous-réseau automatiquement.\n"
            "Vérifiez que eth0 ou wlan0 est actif (`/reseau`)."
        )
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "nmap", "-sn", subnet,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.terminate()
        await update.message.reply_text("⏱ Scan interrompu — timeout 60s dépassé.")
        return
    except Exception as exc:
        await update.message.reply_text(f"❌ Erreur lors du scan : {exc}")
        return

    out = (stdout.decode() + stderr.decode()).strip()

    appareils = []
    bloc = []
    for line in out.splitlines():
        if line.startswith("Nmap scan report"):
            if bloc:
                appareils.append("\n".join(bloc))
            bloc = [line]
        elif bloc and line.strip():
            bloc.append(line.strip())
    if bloc:
        appareils.append("\n".join(bloc))

    nb = len(appareils)
    header = f"📡 *Réseau {subnet}* — {nb} appareil(s) détecté(s)\n\n"
    body = "\n\n".join(appareils) if appareils else out

    await update.message.reply_text(
        header + f"`{_truncate(body, 3800)}`",
        parse_mode="Markdown"
    )

# =============================================================================
#  Nettoyage, terminal, shutdown, reboot
# =============================================================================

async def cmd_nettoyage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("✅ Confirmer", callback_data="nettoyage_ok"),
        InlineKeyboardButton("❌ Annuler",   callback_data="nettoyage_cancel"),
    ]]
    await update.message.reply_text(
        "🗑 *Nettoyage système*\n\n"
        "Opérations :\n"
        "• `apt autoremove --purge`\n"
        "• `apt autoclean`\n"
        "• `apt clean`\n"
        "• Suppression de `/tmp/*` et `/var/tmp/*`\n\n"
        "Confirmer ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Exécute une commande shell (sans sudo) avec liste de blocage."""
    args = " ".join(ctx.args) if ctx.args else ""

    if not args:
        await update.message.reply_text(
            "💻 *Terminal distant*\n\n"
            "Usage : `/terminal <commande>`\n\n"
            "Exemples :\n"
            "• `/terminal ls -lh /home/jfbrunet`\n"
            "• `/terminal df -h`\n"
            "• `/terminal journalctl -u ssh --since '10 minutes ago' --no-pager`\n"
            "• `/terminal journalctl -n 20 --no-pager`\n"
            "• `/terminal systemctl status ssh`\n"
            "• `/terminal systemctl status clamav-freshclam`\n"
            "• `/terminal ip -o -f inet addr show`\n"
            "• `/terminal ps aux --sort=-%cpu | head -10`\n"
            "• `/terminal top -bn1 | head -20`\n"
            "• `/terminal find /home/jfbrunet -name '*.py' | head -20`\n\n"
            "ℹ️ `sudo` et les commandes destructrices sont bloqués.\n"
            "Utilisez `/shutdown` pour éteindre le Pi.",
            parse_mode="Markdown"
        )
        return

    safe, pattern = _terminal_is_safe(args)
    if not safe:
        log.warning(f"Commande /terminal bloquée : {args!r} (motif : {pattern})")
        await update.message.reply_text(
            f"🚫 *Commande refusée*\n\n"
            f"`{args}`\n\n"
            f"Cette commande contient un motif bloqué pour des raisons de sécurité.\n"
            f"Pour éteindre le Pi, utilisez `/shutdown`.",
            parse_mode="Markdown"
        )
        return

    log.info(f"Commande /terminal : {args!r}")
    await update.message.reply_text(f"⚙️ Exécution : `{args}`", parse_mode="Markdown")

    out, ok = _run(["sh", "-c", args], use_sudo=False, timeout=30)
    emoji = "✅" if ok else "❌"

    await update.message.reply_text(
        f"{emoji} `{args}`\n\n`{_truncate(out, 3500)}`",
        parse_mode="Markdown"
    )

async def cmd_shutdown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("⚡ Arrêt Pi", callback_data="shutdown_ok"),
        InlineKeyboardButton("❌ Annuler",  callback_data="shutdown_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ *Arrêt du Pi5*\n\n"
        "Cette action va éteindre le Raspberry.\n"
        "Confirmez-vous ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_reboot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("🔄 Reboot",  callback_data="reboot_ok"),
        InlineKeyboardButton("❌ Annuler", callback_data="reboot_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ *Redémarrage du Pi5*\n\n"
        "Cette action va redémarrer le Raspberry Pi.\n"
        "Confirmez-vous ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =============================================================================
#  Callbacks boutons inline
# =============================================================================

async def callback_ufw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "ufw_cancel":
        await query.edit_message_text("❌ Action UFW annulée.")
        return

    if query.data == "ufw_enable":
        out, ok = _run(["ufw", "--force", "enable"], use_sudo=True)
        emoji = "🔒" if ok else "❌"
        await query.edit_message_text(
            f"{emoji} *UFW activé*\n\n`{_truncate(out)}`",
            parse_mode="Markdown"
        )
    elif query.data == "ufw_disable":
        out, ok = _run(["ufw", "disable"], use_sudo=True)
        emoji = "🔓" if ok else "❌"
        await query.edit_message_text(
            f"{emoji} *UFW désactivé*\n\n`{_truncate(out)}`",
            parse_mode="Markdown"
        )
    else:
        log.warning(f"callback_ufw : data inconnu → '{query.data}'")
        await query.edit_message_text("⚠️ Action UFW inconnue.")

async def callback_vpn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "vpn_cancel":
        await query.edit_message_text("❌ Action VPN annulée.")
        return

    if query.data == "vpn_start":
        out, ok = _run(["systemctl", "start", "wg-quick@wg0"], use_sudo=True)
        emoji = "🟢" if ok else "❌"
        label = "WireGuard activé" if ok else "Échec activation WireGuard"
    elif query.data == "vpn_stop":
        out, ok = _run(["systemctl", "stop", "wg-quick@wg0"], use_sudo=True)
        emoji = "🔴" if ok else "❌"
        label = "WireGuard désactivé" if ok else "Échec désactivation WireGuard"
    else:
        return

    svc, _ = _run(["systemctl", "is-active", "wg-quick@wg0"])
    await query.edit_message_text(
        f"{emoji} *{label}*\n"
        f"Statut actuel : `{svc.strip()}`",
        parse_mode="Markdown"
    )

async def callback_nettoyage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "nettoyage_cancel":
        await query.edit_message_text("❌ Nettoyage annulé.")
        return

    await query.edit_message_text("⏳ Nettoyage en cours…")

    cmds = [
        (["apt", "autoremove", "--purge", "-y"], "apt autoremove --purge"),
        (["apt", "autoclean"],                    "apt autoclean"),
        (["apt", "clean"],                        "apt clean"),
        (["sh", "-c", "rm -rf /tmp/*"],           "rm /tmp/*"),
        (["sh", "-c", "rm -rf /var/tmp/*"],       "rm /var/tmp/*"),
    ]
    results = []
    for cmd, label in cmds:
        _, ok = _run(cmd, use_sudo=True)
        results.append(f"{'✅' if ok else '❌'} {label}")

    await query.edit_message_text(
        "🗑 *Nettoyage terminé*\n\n" + "\n".join(results),
        parse_mode="Markdown"
    )

async def callback_shutdown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "shutdown_cancel":
        await query.edit_message_text("❌ Arrêt annulé.")
        return

    if query.data == "shutdown_ok":
        await query.edit_message_text("🔴 Arrêt du Pi5 en cours…")
        log.info("Arrêt demandé via Telegram.")
        _run(["shutdown", "-h", "now"], use_sudo=True)

async def callback_reboot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "reboot_cancel":
        await query.edit_message_text("❌ Redémarrage annulé.")
        return

    if query.data == "reboot_ok":
        await query.edit_message_text("🔄 Redémarrage du Pi5 en cours…")
        log.info("Redémarrage demandé via Telegram.")
        _run(["reboot"], use_sudo=True)

async def callback_dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("ufw"):
        await callback_ufw(update, ctx)
    elif data.startswith("vpn"):
        await callback_vpn(update, ctx)
    elif data.startswith("nettoyage"):
        await callback_nettoyage(update, ctx)
    elif data.startswith("reboot"):
        await callback_reboot(update, ctx)
    elif data.startswith("shutdown"):
        await callback_shutdown(update, ctx)
    else:
        log.warning(f"callback_dispatch : data inconnu reçu → '{data}'")

# =============================================================================
#  Boucle de surveillance (alertes automatiques)
# =============================================================================

auth_watcher  = AuthLogWatcher()
alert_manager = AlertManager()
_f2b_last_bans: set[str] = set()

async def _surveillance(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id

    # 1. Température
    temp = _temperature()
    if temp is not None and temp > TEMP_ALERT_C:
        if alert_manager.should_send("temp"):
            await ctx.bot.send_message(
                chat_id,
                f"🔴 *ALERTE Température* : {temp:.1f} °C (seuil {TEMP_ALERT_C} °C)",
                parse_mode="Markdown"
            )

    # 2. Espace disque
    free_pct = _disk_min_free_pct()
    if free_pct < DISK_ALERT_PCT:
        if alert_manager.should_send("disk"):
            await ctx.bot.send_message(
                chat_id,
                f"⚠️ *ALERTE Disque* : seulement {free_pct:.0f}% d'espace libre restant !",
                parse_mode="Markdown"
            )

    # 3. Nouvelles IPs bannies par Fail2Ban
    global _f2b_last_bans
    svc, _ = _run(["systemctl", "is-active", "fail2ban"])
    if svc.strip() == "active":
        jails_out, _ = _run(["fail2ban-client", "status"], use_sudo=True, timeout=15)
        jail_names = []
        for line in jails_out.splitlines():
            if "Jail list:" in line:
                jail_names = [j.strip() for j in line.split(":", 1)[1].split(",") if j.strip()]
        current_bans: set[str] = set()
        for jail in jail_names:
            jail_out, ok = _run(["fail2ban-client", "status", jail], use_sudo=True, timeout=15)
            if ok:
                for line in jail_out.splitlines():
                    if "Banned IP list:" in line:
                        ips = line.split(":", 1)[1].strip()
                        current_bans.update(ip.strip() for ip in ips.split() if ip.strip())
        for ip in current_bans - _f2b_last_bans:
            await ctx.bot.send_message(
                chat_id,
                f"🛡 *Fail2Ban* : nouvelle IP bannie → `{ip}`",
                parse_mode="Markdown"
            )
        _f2b_last_bans = current_bans

    # 4. Connexions SSH suspectes via journald
    suspicious = auth_watcher.new_suspicious_lines()
    if suspicious and alert_manager.should_send("ssh"):
        excerpt = "\n".join(suspicious[-5:])
        await ctx.bot.send_message(
            chat_id,
            f"🚨 *Connexion SSH suspecte*\n\n`{_truncate(excerpt, 1000)}`",
            parse_mode="Markdown"
        )

async def cmd_inconnue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Répond à toute commande non reconnue en affichant l'aide."""
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
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("aide",        cmd_aide))
    app.add_handler(CommandHandler("start",       cmd_aide))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("reseau",      cmd_reseau))
    app.add_handler(CommandHandler("scan_reseau", cmd_scan_reseau))
    app.add_handler(CommandHandler("disque",      cmd_disque))
    app.add_handler(CommandHandler("fail2ban",    cmd_fail2ban))
    app.add_handler(CommandHandler("ufw",         cmd_ufw))
    app.add_handler(CommandHandler("ufw_toggle",  cmd_ufw_toggle))
    app.add_handler(CommandHandler("scan",        cmd_scan))
    app.add_handler(CommandHandler("stopscan",    cmd_stopscan))
    app.add_handler(CommandHandler("wireguard",   cmd_wireguard))
    app.add_handler(CommandHandler("vpn",         cmd_vpn))
    app.add_handler(CommandHandler("nettoyage",   cmd_nettoyage))
    app.add_handler(CommandHandler("terminal",    cmd_terminal))
    app.add_handler(CommandHandler("reboot",      cmd_reboot))
    app.add_handler(CommandHandler("shutdown",    cmd_shutdown))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_inconnue))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_inconnue))
    app.add_handler(CallbackQueryHandler(callback_dispatch))

    async def _on_start(app_ref):
        ntp_ok   = await _attendre_ntp(timeout=60)
        now      = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ntp_avert = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
        admin_id = _get_admin_chat_id()
        if admin_id:
            try:
                await app_ref.bot.send_message(
                    admin_id,
                    f"🤖 *Bot Contrôle Pi5 démarré*\n"
                    f"📅 {now}{ntp_avert}\n\n"
                    f"Tapez /aide pour les commandes.",
                    parse_mode="Markdown"
                )
                app_ref.job_queue.run_repeating(
                    _surveillance,
                    interval=WATCH_INTERVAL,
                    first=30,
                    chat_id=admin_id,
                    name="surveillance"
                )
                log.info(f"Surveillance activée — chat_id {admin_id}")
            except Exception as exc:
                log.warning(f"Message de démarrage impossible : {exc}")
        else:
            log.warning(
                "chat_id absent de ~/.telegram_config — "
                "la boucle de surveillance NE sera PAS lancée. "
                "Ajoutez : chat_id = <votre_chat_id>"
            )

    app.post_init = _on_start
    log.info("Bot Telegram Contrôle Pi5 démarré.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
