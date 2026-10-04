#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
#  Bot Telegram – Agent IA Groq  (Raspberry Pi 5 - 16 Go RAM - 256 Go SSD NVMe)
#
#  Commandes disponibles :
#    /start | /aide     – Message d'accueil & liste des commandes
#    /status            – Modèle actif, température, tokens max, nb skills
#    /doctor            – Diagnotic le système
#    /model             – Affiche les modèles disponibles
#    /model <n>         – Change de modèle Groq (n = 1..5)
#    /clear             – Vide l'historique conversation (reset mémoire courte)
#    /mem               – Affiche la mémoire longue (faits mémorisés)
#    /skills            – Liste les skills disponibles
#    /reflect           – Basculer le mode Self-Reflection (On/Off)
#    /temp <val>        – Change la qualité du modèle (0.0–1.0)
#    /tool <nom> [args] – Exécute un outil (date, calc, shell, read, search,
#                         mem, remember, write, write_skill, add_theme_keyword,
#                         audit_autonomy, reindex, forget, net, notify, cron)
#                         write/notify/cron/forget demandent une confirmation
#                         par boutons inline avant exécution réelle.
#                         write_skill/add_theme_keyword restent autonomes
#                         (sans confirmation), encadrés par des garde-fous
#                         automatiques (dédoublonnage sémantique, score
#                         qualité, anti-emballement) partagés avec l'auto-save
#                         de skill déclenché par une réponse de l'agent.
#    📷 photo/image     – Analyse l'image envoyée (qwen/qwen3.8-27b, vision)
#                         La légende de la photo sert de question optionnelle
#    /scanmails [--live] [--since-days N]
#                       – Lance emails_scan.py (scan/classement Gmail+Outlook).
#                         Sans --live : dry-run (aucune action réelle).
#                         Avec --live : actions réelles (déplacement, suppression,
#                         brouillons, envois auto cadrés). Non bloquant pour le bot,
#                         timeout de sécurité à 5 min.
#    <texte libre>      – Dialogue avec l'agent Groq
#
#  Architecture :
#    Ce bot Telegram est une interface → agent_groq_ng.py, il importe directement les 
#    fonctions de ce programme. La mémoire (historique, mémoire longue, vectorielle)
#    est partagée avec les sessions sur le terminal (Pi5).
#    Côté Telegram, la sauvegarde de skill détectée dans une réponse se fait sans
#    confirmation humaine, mais passe par les mêmes garde-fous automatiques que
#    /tool write_skill (dédoublonnage sémantique, score qualité, anti-emballement).
#
#  Prérequis :
#    pip install python-telegram-bot openai pyyaml sentence-transformers numpy --break-system-packages
#    ~/.telegram_config                      : [telegram] / token_groq + chat_id
#    ~/Projects/Groq_agent/.groq_config      : [groq] / api_key
#
#  Fichier de config Telegram à compléter :
#    [telegram]
#    token_groq = VOTRE_TOKEN_BOT_GROQ
#    chat_id    = VOTRE_CHAT_ID
#
#  Auteur  : Jean-François BRUNET – JFBConseils – Aout 2026
# ===============================================================================

import asyncio
import configparser
import datetime
import logging
import os
import sys
import tempfile
import threading
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------
# Import de agent_groq_ng.py
# ------------------------------------------------------------
sys.path.insert(0, "/home/jfbrunet/Projects/Groq_agent")
import agent_groq_ng as ag

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Configuration Telegram (lecture depuis ~/.telegram_config)
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
            "  token_groq = VOTRE_TOKEN\n"
            "  chat_id    = VOTRE_CHAT_ID"
        )

    cfg.read(cfg_path)
    token   = cfg["telegram"]["token_groq"].strip()
    chat_id = int(cfg["telegram"]["chat_id"].strip())

    # Même réflexe que ~/Projects/Groq_agent/.groq_config : ce fichier 
    # contient le token du bot, on s'assure qu'il n'est lisible que par
    # le propriétaire — ne doit jamais bloquer le démarrage.
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass

    return token, chat_id

TOKEN, CHAT_ID = charger_config()

# ------------------------------------------------------------
# Initialisation de l'agent (chemins, config, clé API, embedding)
# ------------------------------------------------------------
def _init_agent():
    """Charge la clé Groq, init les répertoires et lance l'embedding en background."""
    try:
        ag.GROQ_API_KEY = ag.load_groq_api_key()
    except (FileNotFoundError, KeyError, ValueError) as e:
        logger.error(f"Clé API Groq invalide : {e}")
        sys.exit(1)

    ag.init()                      # répertoires + config.yaml + readline
    ag.load_config()               # recharge GROQ_MODEL, MAX_TOKENS, etc.

    # Modèle d'embedding en arrière-plan
    threading.Thread(target=ag._load_embed_model, daemon=True).start()

    # Vectorisation initiale des skills en arrière-plan
    skills_index = ag.load_skills_index()
    for skill in skills_index:
        threading.Thread(
            target=ag._vectorize_text,
            args=(f"{skill['name']}: {skill['description']} {skill['body'][:300]}",
                  f"skill:{skill['name']}"),
            daemon=True,
        ).start()

    logger.info(f"Agent Groq initialisé — modèle : {ag.GROQ_MODEL} — "
                f"{len(skills_index)} skill(s) chargé(s).")
    return skills_index

_skills_index = _init_agent()

# _exchange_idx initialisé depuis ag.EXCHANGE_IDX chargé en config.yaml
_exchange_idx: int = ag.EXCHANGE_IDX

# ------------------------------------------------------------
# Sécurité — vérification du chat_id autorisé
# ------------------------------------------------------------
async def _est_autorise(update: Update) -> bool:
    uid = (update.effective_user.id if update.effective_user else None)
    cid = (update.effective_chat.id  if update.effective_chat  else None)
    return uid == CHAT_ID or cid == CHAT_ID

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _trunc(text: str, maxlen: int = 4000) -> str:
    """Telegram limite les messages à 4096 caractères."""
    return text if len(text) <= maxlen else text[:maxlen - 3] + "…"

def _safe_exc_text(exc: BaseException, maxlen: int = 200) -> str:
    """Version tronquée d'une exception à afficher dans le chat.

    Le détail complet (avec traceback) part toujours dans les logs via
    logger.error(..., exc_info=True) juste avant l'appel — ceci ne concerne
    que ce qui remonte jusqu'à Telegram. Certaines exceptions (SDK réseau,
    erreurs HTTP) peuvent occasionnellement embarquer des fragments de
    requête dans leur message ; on tronque par prudence plutôt que de tout
    répercuter tel quel dans la conversation."""
    text = str(exc)
    return text if len(text) <= maxlen else text[:maxlen - 1] + "…"

async def _reply(update: Update, text: str, reply_markup=None):
    """Envoie un message en gérant la limite Telegram et le Markdown.

    Tentative en Markdown ; si le texte contient une entité mal formée
    (astérisque, underscore ou backtick non apparié — fréquent avec du
    contenu dynamique : noms d'outils, messages d'exception, texte généré
    par le LLM…), on bascule automatiquement en texte brut plutôt que de
    planter la commande."""
    try:
        await update.message.reply_text(
            _trunc(text), parse_mode="Markdown", reply_markup=reply_markup
        )
    except Exception:
        await update.message.reply_text(_trunc(text), reply_markup=reply_markup)

async def _safe_edit(query, text: str, reply_markup=None):
    """Équivalent de _reply() pour l'édition de message (boutons inline).
    Repli en texte brut si le Markdown est mal formé."""
    try:
        await query.edit_message_text(
            _trunc(text), parse_mode="Markdown", reply_markup=reply_markup
        )
    except Exception:
        await query.edit_message_text(_trunc(text), reply_markup=reply_markup)

async def _safe_send(bot, chat_id, text: str, reply_markup=None):
    """Équivalent de _reply() pour un envoi direct via bot.send_message
    (hors contexte d'un update, ex. message de démarrage)."""
    try:
        await bot.send_message(
            chat_id=chat_id, text=_trunc(text), parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    except Exception:
        await bot.send_message(chat_id=chat_id, text=_trunc(text), reply_markup=reply_markup)

def _clean_reflect_response(original: str, reflected: str) -> str:
    """Nettoie la sortie de reflect_on_response pour le mode Telegram mobile.

    Cas 1 : le LLM améliore → on extrait ce qui suit "Version améliorée :"
            pour ne pas afficher le bloc d'évaluation.
    Cas 2 : reflect retourne la réponse originale inchangée (LLM a dit OK)
            → on la retourne telle quelle.
    """
    import re as _re

    # Identique à l'original → le LLM avait dit OK, rien à nettoyer
    if reflected == original:
        return original

    # Chercher le marqueur "version améliorée" et n'extraire que ce qui suit
    marker_pattern = _re.compile(
        r'version\s+améliorée\s*:?\s*\n?', _re.IGNORECASE
    )
    match = marker_pattern.search(reflected)
    if match:
        after = reflected[match.end():].strip()
        if after:
            return after

    # Fallback : supprimer ligne à ligne les lignes d'évaluation en tête
    score_line = _re.compile(
        r'^[-–]?\s*(précision|complétude|utilité|exactitude|pertinence)'
        r'.*?(\d+/\d+|:\s*\d)',
        _re.IGNORECASE
    )
    intro_line = _re.compile(
        r'^(je vais évaluer|voici (une?|la)|la réponse est|évaluation\s*:)',
        _re.IGNORECASE
    )
    lines = reflected.splitlines()
    while lines and (score_line.match(lines[0].strip())
                     or intro_line.match(lines[0].strip())
                     or not lines[0].strip()):
        lines.pop(0)
    cleaned = "\n".join(lines).strip()
    return cleaned if cleaned else original

# ------------------------------------------------------------
# /aide  /start
# ------------------------------------------------------------
async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    reflect = "🟢 ON" if ag.REFLECT_MODE else "⚪ off"
    texte = (
        f"🤖 *Bot Telegram – Agent Groq NG*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Modèle  : `{ag.GROQ_MODEL}`\n"
        f"🌡 Qualité : `{ag.TEMPERATURE}`\n"
        f"🔄 Self-Reflection : {reflect}\n\n"
        f"*Commandes :*\n"
        f"/status — État de l'agent\n"
        f"/model — Liste des modèles\n"
        f"/model <n> — Chgt modèle (1–5)\n"
        f"/clear — Vide mémoire courte\n"
        f"/mem — Affiche mémoire longue\n"
        f"/compact — Optimise mémoire lg\n"
        f"/skills — Liste des skills\n"
        f"/load <nom ou n°> — Affiche skill\n"
        f"/reflect — On/Off Self-Reflection\n"
        f"/temp <val> — Qualité (0.0–1.0)\n"
        f"/tools — Liste des outils\n"
        f"/tool <nom> — ex: date, calc, shell…\n"
        f"/doctor — Diagnostic système\n"
        f"/scanmails — Scan emails (dry-run)\n"
        f"/scanmails --live — Scan emails (réel)\n"
        f"/aide — Ce menu\n\n"
        f"📷 Envoie une photo\n      (légende = question).\n"
        f"💬 Envoie un texte pour dialoguer\n      avec l'agent."
    )
    await _reply(update, texte)

# ------------------------------------------------------------
# /status
# ------------------------------------------------------------
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    ag.maybe_reload_config()  # reflète un éventuel changement fait depuis le terminal
    skills_count  = len(ag.load_skills_index())
    history       = ag.load_history()
    long_mem      = ag.load_long_memory()
    reflect       = "🟢 ON" if ag.REFLECT_MODE else "⚪ off"

    texte = (
        f"📊 *Statut Agent Groq NG*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Modèle : `{ag.GROQ_MODEL}`\n"
        f"🌡 Qualité : `{ag.TEMPERATURE}`\n"
        f"🔢 Max tokens : `{ag.MAX_TOKENS}`\n"
        f"💬 Historique : `{len(history)} message(s)`\n"
        f"🧠 Mémoire lg : `{len(long_mem)} fait(s)`\n"
        f"📚 Skills : `{skills_count}`\n"
        f"🔄 Self-Reflection : {reflect}\n"
        f"👤 Utilisateur : `{ag.USER_LABEL}`\n"
    )
    await _reply(update, texte)

# ------------------------------------------------------------
# /model  (afficher ou changer)
# ------------------------------------------------------------
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    ag.maybe_reload_config()  # reflète un éventuel changement fait depuis le terminal
    args = ctx.args

    if not args:
        # Afficher la liste avec boutons inline
        keyboard = []
        for key, (model_id, label, desc, ctx_win, tpm) in ag.GROQ_MODELS.items():
            marker = "✅ " if model_id == ag.GROQ_MODEL else ""
            btn_label = f"{marker}{key}. {label} — {desc}"
            keyboard.append([InlineKeyboardButton(btn_label,
                                                   callback_data=f"groq_model:{key}")])
        await _reply(
            update,
            "📡 *Choisir un modèle Groq :*",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # /model <n> en argument direct
    n = args[0]
    await _changer_model(update, n)

async def _changer_model(update_or_query, n: str):
    """Change le modèle et répond à l'update ou au query."""
    if n not in ag.GROQ_MODELS:
        msg = f"❌ Numéro invalide : `{n}` — valeurs 1 à 3."
        if hasattr(update_or_query, "message"):
            await _reply(update_or_query, msg)
        else:
            await _safe_edit(update_or_query, msg)
        return

    model_id, label, desc, ctx_win, tpm = ag.GROQ_MODELS[n]
    ag.GROQ_MODEL = model_id
    ag.client = None

    reflect_avert = ""
    if not ag.REFLECT_MODE:
        ag.REFLECT_MODE = True
        reflect_avert = "\n✅ _Self-Reflection activé automatiquement_"

    ag.save_config()

    msg = (f"✅ *Modèle changé*\n"
           f"📡 `{label}`\n"
           f"   Fenêtre : {ctx_win} | TPM : {tpm}/min"
           f"{reflect_avert}")
    if hasattr(update_or_query, "message"):
        await _reply(update_or_query, msg)
    else:
        await _safe_edit(update_or_query, msg)

# ------------------------------------------------------------
# /clear
# ------------------------------------------------------------
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    ag.clear_history()
    await _reply(update, "🧹 *Mémoire courte effacée.* Nouvelle conversation.")

# ------------------------------------------------------------
# /mem
# ------------------------------------------------------------
async def cmd_mem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    mem = ag.load_long_memory()
    if not mem:
        await update.message.reply_text("🧠 Mémoire longue vide.")
        return

    nb_affiches = len(mem[-20:])
    lignes = [f"🧠 *Mémoire longue* ({nb_affiches} dernier{'s' if nb_affiches > 1 else ''} fait{'s' if nb_affiches > 1 else ''}) :\n"]
    for e in mem[-20:]:
        theme = e.get("theme", "")
        label = ag.MEMORY_THEMES.get(theme, {}).get("label", "") if theme else ""
        prefix = f"[{label}]" if label else f"[{e['date']}] _{e['source']}_"
        lignes.append(f"• {prefix} — {e['fact']}")

    await _reply(update, "\n".join(lignes))

# ------------------------------------------------------------
# /compact
# ------------------------------------------------------------
async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    mem_size = len(ag.load_long_memory())

    if mem_size < 10:
        # Peu de faits : simple dédoublonnage Jaccard
        removed = await asyncio.get_running_loop().run_in_executor(
            None, ag.compact_long_memory
        )
        total = len(ag.load_long_memory())
        await _reply(
            update,
            f"🧹 *Mémoire longue compactée*\n"
            f"• {removed} doublon(s) supprimé(s)\n"
            f"• {total} fait(s) conservé(s)",
        )
    else:
        # Consolidation thématique complète via LLM
        await _reply(update, "🔄 _Consolidation thématique en cours…_")
        result = await asyncio.get_running_loop().run_in_executor(
            None, ag.consolidate_long_memory
        )
        if "erreur" in result:
            await _reply(update, f"❌ *Erreur consolidation :*\n{result['erreur']}")
        else:
            lignes = [
                f"✅ *Mémoire consolidée*\n"
                f"• {result['avant']} faits → {result['apres']} thème(s)\n"
            ]
            for theme_key, apercu in result.get("themes", {}).items():
                label = ag.MEMORY_THEMES.get(theme_key, {}).get("label", theme_key)
                lignes.append(f"• _{label}_ : {apercu}")
            await _reply(update, "\n".join(lignes))

# ------------------------------------------------------------
# /skills
# ------------------------------------------------------------
async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    global _skills_index
    _skills_index = ag.load_skills_index()

    if not _skills_index:
        await update.message.reply_text("📚 Aucun skill disponible.")
        return

    # Trié par nom (comme /skills en CLI) : load_skills_index() renvoie l'ordre
    # du système de fichiers (nom de FICHIER), qui peut différer du champ
    # 'name' du frontmatter -- sans ce tri, la numérotation affichée ici ne
    # correspond pas à celle du terminal, rendant /load <n°> 
    # ambigu d'une interface à l'autre.
    sorted_index = sorted(_skills_index, key=lambda s: s["name"].lower())

    lignes = ["📚 *Skills disponibles :*\n"]
    for i, s in enumerate(sorted_index, 1):
        triggers = ", ".join(s.get("triggers", [])[:3])
        lignes.append(f"*{i}.* `{s['name']}` — {s['description']}\n"
                      f"   ↳ Triggers : _{triggers}_")

    await _reply(update, "\n".join(lignes))

# ------------------------------------------------------------
# /load
# ------------------------------------------------------------
async def cmd_load(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    global _skills_index
    if not ctx.args:
        await update.message.reply_text(
            "Usage : /load <nom>  ou  /load <n°>\n"
            "Utilise /skills pour voir la liste des noms et numéros."
        )
        return

    arg = ctx.args[0]
    if arg.isdigit():
        idx = int(arg) - 1
        sorted_index = sorted(_skills_index, key=lambda s: s["name"].lower())
        if 0 <= idx < len(sorted_index):
            arg = sorted_index[idx]["name"]
        else:
            await update.message.reply_text(f"❌ Numéro {arg} invalide.")
            return

    content = ag.load_skill_content(arg)
    if not content:
        await update.message.reply_text(f"❌ Skill '{arg}' introuvable.")
        return

    await _reply(update, f"📚 *{arg}*\n━━━━━━━━━━━━━━━━━━━━━━━━\n{_trunc(content, 3800)}")
async def cmd_reflect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    ag.REFLECT_MODE = not ag.REFLECT_MODE
    ag.save_config()
    etat = "🟢 *activé*" if ag.REFLECT_MODE else "⚪ *désactivé*"
    note = " _(+~200 tokens/réponse)_" if ag.REFLECT_MODE else ""
    await _reply(update, f"🔄 Self-Reflection {etat}{note}")

# ------------------------------------------------------------
# /temp (0.1 factuel & 1 augmente la créativité et le risque d'hallucination)
# ------------------------------------------------------------
async def cmd_temp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    if not ctx.args:
        await _reply(
            update,
            f"🌡 Qualité actuelle : `{ag.TEMPERATURE}`\n"
            f"Usage : `/temp 0.7`  (valeur entre 0.0 et 1.0)",
        )
        return

    try:
        val = float(ctx.args[0])
        if not (0.0 <= val <= 1.0):
            raise ValueError
    except ValueError:
        await _reply(update, "❌ Valeur entre 0.0 et 1.0  ex : `/temp 0.7`")
        return

    ag.TEMPERATURE = val
    ag.save_config()
    await _reply(update, f"✅ Qualité mise à jour : `{ag.TEMPERATURE}`")

# ------------------------------------------------------------
# Pont confirmation Génération NG : run_agentic_turn() est bloquant (exécuté
# via run_in_executor, hors thread asyncio) et appelle confirm_callback(tool,
# preview) -> bool de façon SYNCHRONE. Ce pont fait attendre ce thread avec un
# threading.Event pendant qu'on programme l'envoi des boutons inline sur la
# boucle asyncio via run_coroutine_threadsafe -- même principe que
# _pending_tool_actions plus bas, mais bidirectionnel et avec timeout (une
# action jamais confirmée ne doit pas bloquer le thread indéfiniment).
# ------------------------------------------------------------
_agentic_pending: dict[str, dict] = {}
_AGENTIC_CONFIRM_TIMEOUT = 120  # secondes avant annulation automatique

def _make_agentic_confirm(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, loop):
    def _confirm(tool: str, preview: str) -> bool:
        token = uuid.uuid4().hex[:8]
        state = {"event": threading.Event(), "result": False}
        _agentic_pending[token] = state

        async def _send():
            keyboard = [[
                InlineKeyboardButton("✅ Confirmer", callback_data=f"agentic_confirm:{token}"),
                InlineKeyboardButton("❌ Annuler",   callback_data=f"agentic_cancel:{token}"),
            ]]
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=_trunc(f"🤖 *L'agent veut exécuter :*\n\n{preview}"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        asyncio.run_coroutine_threadsafe(_send(), loop)

        confirmed_in_time = state["event"].wait(timeout=_AGENTIC_CONFIRM_TIMEOUT)
        _agentic_pending.pop(token, None)
        return state["result"] if confirmed_in_time else False   # timeout = prudence = annulé
    return _confirm

# ------------------------------------------------------------
# Callback boutons inline (/model)
# ------------------------------------------------------------
async def callback_dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await _est_autorise(update):
        await query.answer("⛔ Accès refusé.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("groq_model:"):
        n = data[len("groq_model:"):]
        await _changer_model(query, n)
        return

    if data.startswith("agentic_confirm:") or data.startswith("agentic_cancel:"):
        token = data.split(":", 1)[1]
        state = _agentic_pending.get(token)
        if state is None:
            await query.edit_message_text("⏱ Action expirée (délai dépassé ou session redémarrée).")
            return
        state["result"] = data.startswith("agentic_confirm:")
        await _safe_edit(query, "✅ Confirmé — l'agent continue…" if state["result"] else "❌ Annulé.")
        state["event"].set()
        return

    if data.startswith("tool_confirm:") or data.startswith("tool_cancel:"):
        token   = data.split(":", 1)[1]
        pending = _pending_tool_actions.pop(token, None)
        if pending is None:
            await query.edit_message_text("⏱ Action expirée ou déjà traitée.")
            return
        tool_name, tool_args = pending

        if data.startswith("tool_cancel:"):
            await _safe_edit(query, f"❌ Annulé : `{tool_name}`")
            return

        await _safe_edit(query, f"⏳ Exécution de `{tool_name}`…")
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, ag.execute_tool, tool_name, tool_args
            )
            await _reply(query, _trunc(f"🛠 *Outil : {tool_name}*\n\n{result}"))
        except Exception as exc:
            logger.error(f"callback_dispatch (tool_confirm) exception : {exc}", exc_info=True)
            await _reply(query, f"❌ Erreur outil `{tool_name}` : {_safe_exc_text(exc)}")
        return

    logger.warning(f"callback_dispatch : data inconnu : '{data}'")

# ------------------------------------------------------------
# /tool <nom> [args] 
# ------------------------------------------------------------
TOOLS_AIDE = (
    "🛠 *Outils disponibles :* /tool\n\n"
    "`date` - date & heure\n"
    "`\ncalc <expr>` - `/tool calc 2**10`\n"
    "`\nshell <cmd>` - `/tool shell df -h`\n"
    "`\nread <chemin>` - `~/notes.txt`\n"
    "`\nsearch <mots>` - Recherche\n"
    "`\nmem` - Affiche mémoire longue\n"
    "`\nremember <fait>` - Mémorise un fait\n"
    "`\nreindex` - Resynchronise les ids\n"
    "`\nwrite_skill <nom> :: <md>`\n     - Crée/màj un skill (⚡ sans confirm)\n"
    "`\nadd_theme_keyword <thème> :: <mot>`\n     - Ajoute mot-clé (⚡ sans confirm)\n"
    "`\naudit_autonomy [n]`\n     - Ecritures journalisées\n\n"
    "*Outils avec confirmation :*\n"
    "`\nwrite <fichier> :: <contenu>`\n     - Écrit dans le workspace.\n"
    "`\nnet <hôte>`\n     - Test connexion (ping)\n       `/tool net 1.1.1.1`\n"
    "`\nnotify <message>`\n     - Envoie notification Telegram\n"
    "`\nforget <id>`\n     - Suppr. un fait `long_mem:N` ou `exchange:N` (id avec `/tool search`)\n"
    "`\ncron list|add|remove`\n     - Gère les tâches planifiées"
)

_pending_tool_actions: dict[str, tuple[str, str]] = {}
_PENDING_MAX = 50

def _pending_tool_add(tool: str, args: str) -> str:
    if len(_pending_tool_actions) >= _PENDING_MAX:
        oldest = next(iter(_pending_tool_actions))
        _pending_tool_actions.pop(oldest, None)
    token = uuid.uuid4().hex[:8]
    _pending_tool_actions[token] = (tool, args)
    return token

async def cmd_tool(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    if not ctx.args:
        await _reply(update, TOOLS_AIDE)
        return

    tool_name = ctx.args[0].lower().strip()
    tool_args = " ".join(ctx.args[1:]).strip() if len(ctx.args) > 1 else ""

    if tool_name not in ag.TOOLS:
        liste = ", ".join(f"`{t}`" for t in ag.TOOLS)
        await _reply(update, f"❌ Outil inconnu : `{tool_name}`\nOutils disponibles : {liste}")
        return

    if ag.tool_call_needs_confirmation(tool_name, tool_args):
        apercu = ag.preview_tool_action(tool_name, tool_args)
        token  = _pending_tool_add(tool_name, tool_args)
        keyboard = [[
            InlineKeyboardButton("✅ Confirmer", callback_data=f"tool_confirm:{token}"),
            InlineKeyboardButton("❌ Annuler",   callback_data=f"tool_cancel:{token}"),
        ]]
        await _reply(
            update,
            f"⚠ *Confirmation requise*\n\n{apercu}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, ag.execute_tool, tool_name, tool_args
        )
        await _reply(update, f"🛠 *Outil : {tool_name}*\n\n{result}")
    except Exception as exc:
        logger.error(f"cmd_tool exception : {exc}", exc_info=True)
        await _reply(update, f"❌ Erreur outil `{tool_name}` : {_safe_exc_text(exc)}")

# ------------------------------------------------------------
# /tools  (liste des outils dispo., équivalent à /tool sans argument)
# ------------------------------------------------------------
async def cmd_tools(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    lignes = [f"🛠 *Outils disponibles*", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for nom, desc in ag.TOOLS.items():
        lignes.append(f"• `{nom}` — {desc}")
    lignes.append("\nUsage : `/tool <nom> [arguments]`")
    await _reply(update, "\n".join(lignes))

# ------------------------------------------------------------
# /doctor  (diagnostic système, réutilise ag.get_doctor_checks)
# ------------------------------------------------------------
async def cmd_doctor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        checks = await asyncio.get_running_loop().run_in_executor(
            None, ag.get_doctor_checks, _skills_index
        )
    except Exception as exc:
        logger.error(f"cmd_doctor exception : {exc}", exc_info=True)
        await update.message.reply_text(f"❌ Erreur diagnostic : {_safe_exc_text(exc)}")
        return

    nb_ok = nb_warn = nb_fail = 0
    problemes = []
    lignes = ["🩺 *Diagnostic Agent Groq*", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for label, (icone, detail) in checks:
        lignes.append(f"{icone} *{label}* — {detail}")
        if icone == "✅":
            nb_ok += 1
        elif icone == "❌":
            nb_fail += 1
            problemes.append(label)
        elif icone == "🟡":
            nb_warn += 1
            problemes.append(label)

    lignes.append("")
    if nb_fail:
        lignes.append(f"❌ {nb_fail} problème(s) critique(s) — {nb_warn} avertissement(s), {nb_ok} OK")
    elif nb_warn:
        lignes.append(f"⚠ {nb_warn} avertissement(s) à surveiller — {nb_ok} OK")
    else:
        lignes.append(f"✅ Tout est vert — {nb_ok}/{nb_ok} vérifications passées")

    resume = f"ok={nb_ok} warn={nb_warn} fail={nb_fail}"
    if problemes:
        resume += " | " + " ; ".join(problemes)
    ag.log_event("doctor_run", resume)

    await _reply(update, "\n".join(lignes))

# ------------------------------------------------------------
# Photos → analyse d'image via qwen/qwen3.8-27b (vision)
# ------------------------------------------------------------
async def handler_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reçoit une photo Telegram, la télécharge temporairement et l'analyse
    avec ag.analyze_image(). La légende (caption) sert de question optionnelle."""
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    msg_id = update.message.message_id
    if msg_id in _en_cours:
        return
    _en_cours.add(msg_id)

    try:
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="upload_photo")

        # Telegram fournit plusieurs résolutions ; on prend la plus grande.
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()

        question = (update.message.caption or "").strip()

        # Téléchargement dans un fichier temporaire (nettoyé après analyse)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="typing")

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, ag.analyze_image, tmp_path, question
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        await _reply(update, result)

    except Exception as exc:
        logger.error(f"handler_photo exception : {exc}", exc_info=True)
        await _reply(update, f"❌ Erreur analyse image : {_safe_exc_text(exc)}")
    finally:
        _en_cours.discard(msg_id)

# ------------------------------------------------------------
# Documents (fichiers image envoyés sans compression, .png notamment)
# ------------------------------------------------------------
DOC_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

async def handler_document_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gère les images envoyées en tant que document (sans compression Telegram),
    utile pour les .png ou les photos en pleine résolution."""
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    doc = update.message.document
    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in DOC_IMAGE_EXTS:
        return  # pas une image : on laisse passer (aucun handler pour le reste)

    msg_id = update.message.message_id
    if msg_id in _en_cours:
        return
    _en_cours.add(msg_id)

    try:
        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="upload_photo")

        tg_file  = await doc.get_file()
        question = (update.message.caption or "").strip()

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="typing")

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, ag.analyze_image, tmp_path, question
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        await _reply(update, result)

    except Exception as exc:
        logger.error(f"handler_document_image exception : {exc}", exc_info=True)
        await _reply(update, f"❌ Erreur analyse image : {_safe_exc_text(exc)}")
    finally:
        _en_cours.discard(msg_id)

# ------------------------------------------------------------
# Handler principal — messages texte libres → agent Groq
# ------------------------------------------------------------
# Anti-rebond : évite le double traitement 
_en_cours: set[int] = set()

async def handler_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _exchange_idx, _skills_index

    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    msg_id = update.message.message_id
    if msg_id in _en_cours:
        return
    _en_cours.add(msg_id)

    user_input = (update.message.text or "").strip()
    if not user_input:
        _en_cours.discard(msg_id)
        return

    # Un autre processus (session terminal) a pu changer le modèle/température
    # entre-temps -- resynchronise avant de traiter ce message. Même logique
    # que côté CLI (maybe_reload_config), les deux interfaces étant deux
    # processus indépendants qui ne partagent leur état qu'au travers de
    # config.yaml.
    _model_avant = ag.GROQ_MODEL
    if ag.maybe_reload_config() and ag.GROQ_MODEL != _model_avant:
        await _reply(update, f"📡 _Modèle synchronisé depuis une autre session : `{ag.GROQ_MODEL}`_")

    # Indicateur "en train de taper…"
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                   action="typing")

    try:
        # ── Skill Router ──────────────────────────────────────────────────────
        skill_name, route_method = ag.route_skill(user_input, _skills_index)
        skill_content = ag.load_skill_content(skill_name) if skill_name else None

        # ── Contexte vectoriel ────────────────────────────────────────────────
        vector_ctx = None
        if ag._embed_model is not None:
            results = ag.vector_search(user_input, top_k=3)
            if results:
                lines = [f"- [{doc_id}] {text[:120]}"
                         for text, doc_id, score in results if score > 0.4]
                if lines:
                    vector_ctx = "\n".join(lines)

        # ── Context Builder + appel Groq ──────────────────────────────────────
        history       = ag.load_history()
        system_prompt = ag.build_system_prompt(_skills_index, skill_content, vector_ctx)

        loop = asyncio.get_running_loop()
        confirm_cb = _make_agentic_confirm(ctx, update.effective_chat.id, loop)
        response, action_log = await loop.run_in_executor(
            None, ag.run_agentic_turn, system_prompt, history, user_input, confirm_cb
        )
        if action_log:
            await _reply(update, "\n".join(f"_{l}_" for l in action_log))

        # ── Self-Reflection ───────────────────────────────────────────────────
        if ag.REFLECT_MODE and not response.startswith("⚠"):
            await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action="typing")
            original_response = response
            reflected = await asyncio.get_running_loop().run_in_executor(
                None, ag.reflect_on_response, user_input, response
            )
            response = _clean_reflect_response(original_response, reflected)

        # ── Détection skill à sauvegarder ─────────────────────────────────────
        skill_data = ag.parse_skill_from_response(response)
        if skill_data:
            response = ag.response_without_skill_block(response)
            # validation minimale avant sauvegarde automatique
            skill_name_val  = str(skill_data.get("name", "")).strip()
            skill_content_val = str(skill_data.get("content", "")).strip()
            skill_valid = (
                skill_name_val                          # nom non vide
                and len(skill_name_val) <= 80           # nom raisonnable
                and not any(c in skill_name_val for c in r"/\:*?\"<>|")  # pas de chemin
                and skill_content_val                   # contenu non vide
                and len(skill_content_val) >= 10        # contenu non trivial
            )
            if not skill_valid:
                logger.warning(f"Skill proposé ignoré (validation échouée) : "
                               f"name={skill_name_val!r} content_len={len(skill_content_val)}")
                await _reply(
                    update,
                    "⚠ *Skill proposé invalide — sauvegarde ignorée.*\n"
                    "_(nom vide, trop long, ou contenu insuffisant)_",
                )
            else:
                # guarded_save_skill applique les mêmes garde-fous d'autonomie que
                # le tool write_skill (dédoublonnage sémantique, score qualité,
                # anti-emballement) -- avant ce correctif, ce chemin appelait
                # ag.save_skill() directement et n'en bénéficiait pas du tout,
                # alors que c'est justement le seul des trois chemins de sauvegarde
                # sans confirmation humaine.
                f, msg = await asyncio.get_running_loop().run_in_executor(
                    None, ag.guarded_save_skill,
                    skill_data["name"], skill_data.get("description", ""),
                    skill_data.get("triggers", []), skill_data["content"],
                )
                if f is None:
                    logger.info(f"Skill proposé refusé par les garde-fous : "
                                f"name={skill_name_val!r} — {msg}")
                    await _reply(
                        update,
                        f"ℹ️ *Skill non sauvegardé :* {msg}",
                    )
                else:
                    _skills_index = ag.load_skills_index()
                    await _reply(
                        update,
                        f"💾 *Nouveau skill sauvegardé :* `{skill_data['name']}`\n"
                        f"_{skill_data.get('description', '')}_ → `{f.name}`",
                    )

        # ── Envoi de la réponse ───────────────────────────────────────────────
        if skill_name:
            method_str = "🔑 mot-clé" if route_method == "keyword" else "🔍 vectoriel"
            await _reply(update, f"_📎 Skill : {skill_name}  ({method_str})_")

        await _reply(update, response)

        # ── Mise à jour des mémoires ──────────────────────────────────────────
        history = ag.append_exchange_to_history(user_input, response)   # écriture atomique — invalide le cache _history_cache dans ag

        ag.vectorize_exchange(user_input, response, _exchange_idx)
        _exchange_idx += 1
        # synchronise ag.EXCHANGE_IDX et persiste dans config.yaml
        ag.EXCHANGE_IDX = _exchange_idx
        ag.save_config()

        ag._BACKGROUND_EXECUTOR.submit(ag.extract_and_store_facts, user_input, response)

    except Exception as exc:
        logger.error(f"handler_message exception : {exc}", exc_info=True)
        await _reply(update, f"❌ Erreur interne : {_safe_exc_text(exc)}")
    finally:
        _en_cours.discard(msg_id)

# ------------------------------------------------------------
# /scanmails  (déclenchement à la demande de emails_scan.py)
# ------------------------------------------------------------
EMAILS_SCAN_SCRIPT = os.path.expanduser(
    "~/Projects/Groq_agent/Scan_emails/emails_scan.py"
)

async def cmd_scanmails(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _est_autorise(update):
        await update.message.reply_text("⛔ Accès refusé.")
        return

    args = ctx.args if ctx.args else []
    dry_run = "--live" not in args
    mode_txt = "DRY-RUN (aucune action réelle)" if dry_run else "LIVE"
    await _reply(update, f"📧 Scan emails lancé ({mode_txt})… patiente, ça peut prendre 1-2 min.")

    cmd = [sys.executable, EMAILS_SCAN_SCRIPT] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await _reply(update, "⏱ Scan interrompu après 5 min (timeout).")
            return

        sortie = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            await _reply(update, f"❌ emails_scan.py a échoué (code {proc.returncode}) :\n{_trunc(sortie, 3500)}")
        else:
            await _reply(update, _trunc(sortie, 3800) or "✅ Scan terminé, aucune sortie.")
    except Exception as exc:
        await _reply(update, f"❌ Erreur au lancement du scan : {_safe_exc_text(exc)}")

# ------------------------------------------------------------
# Attente synchronisation NTP
# ------------------------------------------------------------
async def _attendre_ntp(timeout: int = 60) -> bool:
    import subprocess
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
    # Enregistre un menu natif de commandes Telegram 
    # (bouton "Menu" à côté de la zone de saisie). 
    try:
        await application.bot.set_my_commands([
            BotCommand("aide",    "Affiche l'aide"),
            BotCommand("status",  "État de l'agent"),
            BotCommand("model",   "Change de modèle Groq"),
            BotCommand("tool",    "Utilise un outil (date, calc, shell, write, net, notify, cron...)"),
            BotCommand("tools",   "Liste les outils disponibles"),
            BotCommand("doctor",  "Diagnostic système"),
            BotCommand("mem",     "Affiche la mémoire longue"),
            BotCommand("compact", "Consolide la mémoire longue"),
            BotCommand("skills",  "Liste les skills"),
            BotCommand("reflect", "Active/désactive l'auto-évaluation"),
            BotCommand("temp",    "Change la température"),
            BotCommand("clear",   "Efface la mémoire courte"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands a échoué (non bloquant) : {e}")

    ntp_ok    = await _attendre_ntp(timeout=60)
    heure     = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    ntp_avert = "" if ntp_ok else "\n⚠ Heure non synchronisée NTP"
    reflect   = "🟢 ON" if ag.REFLECT_MODE else "⚪ off"
    nb_skills = len(ag.load_skills_index())

    await _safe_send(
        application.bot,
        CHAT_ID,
        f"🤖 *Bot Agent Groq NG démarré*\n"
        f"📅 {heure}{ntp_avert}\n\n"
        f"📡 Modèle : `{ag.GROQ_MODEL}`\n"
        f"🌡 Qualité : `{ag.TEMPERATURE}`\n"
        f"🔄 Self-Reflection : {reflect}\n"
        f"📚 Skills chargés : `{nb_skills}`\n\n"
        f"Tapez /aide pour les commandes.",
    )
    logger.info(f"Message de démarrage envoyé (NTP OK : {ntp_ok}).")

# ------------------------------------------------------------
# Gestionnaire d'erreur global
# ------------------------------------------------------------
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Filet de sécurité pour toute exception non rattrapée par les handlers
    (erreurs réseau pendant le polling, exceptions PTB internes, etc.).
    Sans ce handler, ces erreurs ne sont visibles que dans les logs et
    peuvent passer inaperçues si le bot tourne en tâche de fond."""
    logger.error("Exception non gérée :", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠ Erreur interne inattendue : {_safe_exc_text(ctx.error)}",
            )
    except Exception:
        # Jamais laisser error_handler planter lui-même le bot
        pass

# ------------------------------------------------------------
# Point d'entrée
# ------------------------------------------------------------
def main():
    logger.info("Démarrage du bot Telegram Groq…")
    logger.info(f"CHAT_ID autorisé : {CHAT_ID}")
    logger.info(f"Modèle initial   : {ag.GROQ_MODEL}")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30)    # 30 sec
        .read_timeout(60)       # 60 sec
        .write_timeout(60)
        .pool_timeout(30)
        .get_updates_read_timeout(60)
        .post_init(message_demarrage)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_aide))
    app.add_handler(CommandHandler("aide",    cmd_aide))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("model",   cmd_model))
    app.add_handler(CommandHandler("clear",   cmd_clear))
    app.add_handler(CommandHandler("mem",     cmd_mem))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("skills",  cmd_skills))
    app.add_handler(CommandHandler("load",    cmd_load))
    app.add_handler(CommandHandler("reflect", cmd_reflect))
    app.add_handler(CommandHandler("temp",    cmd_temp))
    app.add_handler(CommandHandler("tool",    cmd_tool))
    app.add_handler(CommandHandler("tools",   cmd_tools))
    app.add_handler(CommandHandler("doctor",  cmd_doctor))
    app.add_handler(CommandHandler("scanmails", cmd_scanmails))
    app.add_handler(CallbackQueryHandler(callback_dispatch))
    app.add_handler(MessageHandler(filters.PHOTO, handler_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handler_document_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler_message))
    app.add_error_handler(error_handler)

    logger.info("Bot en écoute… (Ctrl+C pour arrêter)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
