#!/usr/bin/env python3
# =============================================================================
#  Bot Telegram — LLM Ollama (Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe)
#
#  Modèles :
#    - Texte / PDF / TXT  → llama3.2 (ou mistral, qwen…)
#    - Vision / Images    → moondream (en anglais uniquement)
#
#  Commandes :
#    /aide         Menu + boutons questions vision
#    /llm <texte>  Message texte au LLM
#    /modele       Changer de modèle texte
#    /qualite      Régler la qualité des réponses
#    /stop         Interrompre la génération
#    /effacer      Effacer l'historique (modèle actuel ou tous)
#    /status       Vérifier Ollama + température CPU
#
#  Envoi direct :
#    📷 Photo (+ légende optionnelle)  → moondream vision
#    📄 Fichier .txt / .pdf            → modèle texte
#    💬 Texte sans commande            → modèle texte
#
#  Configuration — ~/.telegram_config :
#      [telegram]
#      token_llm  = VOTRE_TOKEN_LLM
#      chat_id    = VOTRE_CHAT_ID
#
#  Auteur : Jean-François BRUNET – JFBConseils – Mai 2026
# =============================================================================

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ChatAction

# =============================================================================
#  CONFIGURATION
# =============================================================================

CONFIG_FILE           = Path.home() / ".telegram_config"
TOKEN_KEY             = "token_llm"

# Modèle vision (fixe — moondream uniquement sur Pi5)
MODEL_VISION          = "moondream"
TEMP_VISION_DEFAULT   = 0.2   # Vision = analyse factuelle → faible variabilité

# Modèles texte disponibles (le premier est le défaut)
MODELS_TEXT = [
    ("llama3.2:3b",         "🦙 Llama 3.2 3B (texte)"),
    ("llama3.2-vision:11b", "🦙 Llama 3.2 11B (vision)"),
    ("mistral:7b",          "🌬️ Mistral 7B"),
    ("qwen2.5:7b",          "🀄 Qwen 2.5 7B"),
    ("deepseek-coder:6.7b", "💻 DeepSeek Coder"),
    ("gemma2:9b",           "💎 Gemma 2 9B"),
]
DEFAULT_TEXT_MODEL    = MODELS_TEXT[0][0]
TEMP_TEXT_DEFAULT     = 0.7   # Texte → équilibre créativité / précision

# Niveaux de qualité des réponses (LLM) proposés à l'utilisateur
QUALITY_LEVELS = [
    (0.0, "🧊 Précis",       "\nDéterministe, idéal pour : faits & code."),
    (0.3, "🎯 Factuel",      "\nPeu de variabilité, privilégie la précision."),
    (0.7, "⚖️ Équilibré",    "\nPar défaut — bon compromis."),
    (1.0, "🎨 Créatif",      "\nVariété et Originalité dans les réponses."),
    (1.5, "🌪️ Imaginatif",   "\nTrès créatif, peut être imprévisible."),
]

# Surveillance température CPU
TEMP_CPU_PATH         = Path("/sys/class/thermal/thermal_zone0/temp")
TEMP_WARN_C           = 78.0   # °C — avertissement avant génération
TEMP_CRIT_C           = 85.0   # °C — indicateur critique dans /status

OLLAMA_URL            = "http://localhost:11434"
REQUESTS_TIMEOUT      = 300
MAX_HISTORY_PAIRS     = 10
MAX_FILE_CHARS        = 8000
STREAM_EDIT_INTERVAL  = 1.5
VISION_NUM_PREDICT    = 512
TEXT_NUM_PREDICT      = 1024
KEEP_ALIVE            = "10m"
HISTORY_DIR           = Path.home() / ".llm_bot_histories"
HISTORY_DIR.mkdir(exist_ok=True)

# Questions vision prédéfinies (envoyées en anglais à moondream)
VISION_QUESTIONS = [
    ("🔍 Décrire",        "Describe this image in detail."),
    ("👤 Qui ?",          "Who or what is the main subject of this image? Describe in detail."),
    ("📅 Époque ?",       "What time period or era does this photo appear to be from? Explain why."),
    ("📝 Extraire texte", "Extract and list all text visible in this image."),
    ("✨ Une phrase",     "Write one single sentence that best captures the essence of this image."),
    ("🎨 Style/Couleurs", "Describe the colors, style, and artistic or photographic technique used."),
]

# =============================================================================
#  LOGGING
# =============================================================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# =============================================================================
#  TEMPÉRATURE CPU
# =============================================================================

def get_cpu_temp() -> float | None:
    """Lit la température CPU depuis le sysfs du Pi 5. Retourne None si indisponible."""
    try:
        raw = TEMP_CPU_PATH.read_text().strip()
        return int(raw) / 1000.0
    except Exception:
        return None

def cpu_temp_str(temp: float | None) -> str:
    """Retourne une chaîne formatée avec indicateur coloré."""
    if temp is None:
        return "🌡️ N/A"
    if temp >= TEMP_CRIT_C:
        icon = "🔴"
    elif temp >= TEMP_WARN_C:
        icon = "🟠"
    elif temp >= 65:
        icon = "🟡"
    else:
        icon = "🟢"
    return f"🌡️ {icon} {temp:.1f}°C"

async def _check_temp_warning(update: Update) -> bool:
    """Vérifie la température avant une génération.
    Envoie un avertissement si CPU > TEMP_WARN_C.
    Retourne True si la génération peut continuer, False si bloquée.
    Note : on avertit mais on ne bloque pas — l'utilisateur décide."""
    temp = get_cpu_temp()
    if temp is not None and temp >= TEMP_WARN_C:
        icon = "🔴" if temp >= TEMP_CRIT_C else "🟠"
        await update.effective_message.reply_text(
            f"⚠️ *Température CPU élevée* : {icon} {temp:.1f}°C\n"
            f"La génération va démarrer mais surveillez la chaleur.\n"
            f"_(Seuil d'avertissement : {TEMP_WARN_C}°C)_",
            parse_mode="Markdown",
        )
    return True  # On continue dans tous les cas

# =============================================================================
#  CONFIG
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
    token = _parse_config().get(TOKEN_KEY.lower(), "")
    if not token:
        raise ValueError(f"Clé '{TOKEN_KEY}' introuvable dans {CONFIG_FILE}.")
    return token

def _get_admin_chat_id() -> int | None:
    val = _parse_config().get("chat_id", "")
    return int(val) if val.lstrip("-").isdigit() else None

# =============================================================================
#  HISTORIQUE — séparé par modèle (clé = chat_id + model)
# =============================================================================

def _history_path(chat_id: int, model: str) -> Path:
    """Fichier d'historique spécifique à chaque (chat_id, modèle)."""
    safe_model = model.replace(":", "-").replace("/", "_")
    return HISTORY_DIR / f"history_{chat_id}__{safe_model}.json"

def load_history(chat_id: int, model: str) -> list:
    path = _history_path(chat_id, model)
    if not path.exists():
        return []
    try:
        sessions = json.loads(path.read_text(encoding="utf-8"))
        return [
            {"role": m["role"], "content": m["content"]}
            for s in sessions for m in s.get("messages", [])
        ]
    except Exception as e:
        log.warning(f"[HISTORY] Erreur chargement {chat_id}/{model}: {e}")
        return []

def save_history(chat_id: int, model: str, history: list):
    path = _history_path(chat_id, model)
    try:
        sessions = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if history:
            sessions.append({
                "date":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "model":    model,
                "messages": list(history),
            })
        path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"[HISTORY] Erreur sauvegarde {chat_id}/{model}: {e}")

ERROR_MARKERS = ("[⏱", "[❌", "⚠️ Aucune réponse", "[Timeout", "[Erreur")

def trim_history(history: list) -> list:
    history = [m for m in history if not any(
        marker in str(m.get("content", "")) for marker in ERROR_MARKERS
    )]
    pairs, i = [], 0
    while i < len(history) - 1:
        if history[i]["role"] == "user" and history[i+1]["role"] == "assistant":
            pairs.append((i, i+1))
            i += 2
        else:
            i += 1
    if len(pairs) > MAX_HISTORY_PAIRS:
        drop = set()
        for p in pairs[:len(pairs)-MAX_HISTORY_PAIRS]:
            drop.add(p[0]); drop.add(p[1])
        history = [m for idx, m in enumerate(history) if idx not in drop]
    return history

# =============================================================================
#  SESSIONS
# =============================================================================

_sessions: dict[int, dict] = {}

def get_session(chat_id: int) -> dict:
    if chat_id not in _sessions:
        _sessions[chat_id] = {
            # Historiques par modèle : { model_id: [messages] }
            "histories":       {},
            "stop":            False,
            "generating":      False,
            "text_model":      DEFAULT_TEXT_MODEL,
            "quality":         TEMP_TEXT_DEFAULT,   # LLM texte
            "last_images_b64": [],
            "last_prompt_en":  "",
            "buffer":          "",
        }
    return _sessions[chat_id]

def get_model_history(session: dict, chat_id: int, model: str) -> list:
    """Retourne (et initialise si besoin) l'historique pour un modèle donné."""
    if model not in session["histories"]:
        session["histories"][model] = load_history(chat_id, model)
    return session["histories"][model]

def set_model_history(session: dict, model: str, history: list):
    session["histories"][model] = history

# =============================================================================
#  UTILITAIRES
# =============================================================================

def encode_image_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()

def read_text_file(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="ignore")
        return content[:MAX_FILE_CHARS] + "\n\n[Tronqué…]" if len(content) > MAX_FILE_CHARS else content
    except Exception as e:
        return f"[Erreur lecture : {e}]"

def read_pdf_file(path: str) -> str:
    try:
        from pdfminer.high_level import extract_text
        content = extract_text(path) or ""
        if not content.strip():
            return "[PDF vide ou scanné sans OCR]"
        return content[:MAX_FILE_CHARS] + "\n\n[Tronqué…]" if len(content) > MAX_FILE_CHARS else content
    except ImportError:
        return "[Installez pdfminer.six pour lire les PDF]"
    except Exception as e:
        return f"[Erreur PDF : {e}]"

def _truncate(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[tronqué]"

def _quality_label(temp: float) -> str:
    """Retourne le label correspondant à une valeur de Qualité."""
    for val, label, _ in QUALITY_LEVELS:
        if abs(val - temp) < 0.01:
            return label
    return f"⚙️ {temp}"

def _vision_questions_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(VISION_QUESTIONS), 2):
        row = [InlineKeyboardButton(VISION_QUESTIONS[i][0], callback_data=f"vq_{i}")]
        if i+1 < len(VISION_QUESTIONS):
            row.append(InlineKeyboardButton(VISION_QUESTIONS[i+1][0], callback_data=f"vq_{i+1}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("▶️ Relancer",       callback_data="relancer"),
        InlineKeyboardButton("🔄 Changer modèle", callback_data="changer_modele_texte"),
    ])
    return InlineKeyboardMarkup(rows)

def _text_models_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for model_id, label in MODELS_TEXT:
        check = "✅ " if model_id == current else ""
        rows.append([InlineKeyboardButton(f"{check}{label}", callback_data=f"tm_{model_id}")])
    return InlineKeyboardMarkup(rows)

def _quality_keyboard(current: float) -> InlineKeyboardMarkup:
    rows = []
    for val, label, desc in QUALITY_LEVELS:
        check = "✅ " if abs(val - current) < 0.01 else ""
        rows.append([InlineKeyboardButton(
            f"{check}{label} ({val})",
            callback_data=f"ql_{val}",
        )])
    return InlineKeyboardMarkup(rows)

def _effacer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Modèle actuel", callback_data="eff_current"),
        InlineKeyboardButton("🗑🗑 Tous",         callback_data="eff_all"),
    ]])

# =============================================================================
#  OLLAMA — VISION (moondream, température fixe TEMP_VISION_DEFAULT)
# =============================================================================

def ollama_vision_stream(prompt_en: str, images_b64: list, stop_flag: dict) -> str:
    payload = {
        "model":      MODEL_VISION,
        "prompt":     prompt_en,
        "images":     images_b64,
        "stream":     True,
        "keep_alive": KEEP_ALIVE,
        "options":    {
            "num_predict": VISION_NUM_PREDICT,
            "temperature": TEMP_VISION_DEFAULT,
        },
    }
    log.info(f"[VISION] Appel — images={len(images_b64)} temp={TEMP_VISION_DEFAULT} prompt={prompt_en!r}")
    full_text = ""
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload, stream=True, timeout=REQUESTS_TIMEOUT,
        ) as r:
            log.info(f"[VISION] HTTP {r.status_code}")
            r.raise_for_status()
            nb = 0
            for line in r.iter_lines():
                if stop_flag.get("stop"):
                    break
                if not line:
                    continue
                nb += 1
                try:
                    data = json.loads(line.decode())
                    delta = data.get("response", "")
                    if delta:
                        full_text += delta
                        stop_flag["buffer"] = stop_flag.get("buffer", "") + delta
                    if data.get("done"):
                        log.info(f"[VISION] done — {nb} lignes — {len(full_text)} chars")
                except json.JSONDecodeError:
                    continue
    except requests.exceptions.Timeout:
        full_text += "\n\n[⏱ Timeout]"
    except Exception as e:
        full_text += f"\n\n[❌ {e}]"
    log.info(f"[VISION] Retour — {len(full_text)} chars : {full_text[:80]!r}")
    return full_text

# =============================================================================
#  OLLAMA — TEXTE (llama/mistral/qwen… via /api/chat)
# =============================================================================

def ollama_text_stream(history: list, stop_flag: dict, model: str, temperature: float) -> str:
    payload = {
        "model":      model,
        "messages":   history,
        "stream":     True,
        "keep_alive": KEEP_ALIVE,
        "options":    {
            "num_predict": TEXT_NUM_PREDICT,
            "temperature": temperature,
        },
    }
    log.info(f"[TEXT] Appel — modèle={model} temp={temperature} messages={len(history)}")
    full_text = ""
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload, stream=True, timeout=REQUESTS_TIMEOUT,
        ) as r:
            log.info(f"[TEXT] HTTP {r.status_code}")
            r.raise_for_status()
            for line in r.iter_lines():
                if stop_flag.get("stop"):
                    break
                if not line:
                    continue
                try:
                    data = json.loads(line.decode())
                    delta = data.get("message", {}).get("content", "")
                    if delta:
                        full_text += delta
                        stop_flag["buffer"] = stop_flag.get("buffer", "") + delta
                except json.JSONDecodeError:
                    continue
    except requests.exceptions.Timeout:
        full_text += "\n\n[⏱ Timeout]"
    except Exception as e:
        full_text += f"\n\n[❌ {e}]"
    log.info(f"[TEXT] Retour — {len(full_text)} chars")
    return full_text

# =============================================================================
#  MOTEUR DE GÉNÉRATION
# =============================================================================

async def _run_vision(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    prompt_en: str, images_b64: list, chat_id: int,
):
    """Génération vision — moondream, qualité fixe à 0.2, placeholder avec CPU temp."""
    session = get_session(chat_id)
    if session["generating"]:
        await update.effective_message.reply_text("⏳ Génération en cours. Utilisez /stop.")
        return
    await _check_temp_warning(update)

    session["generating"]      = True
    session["stop"]            = False
    session["buffer"]          = ""
    session["last_images_b64"] = images_b64
    session["last_prompt_en"]  = prompt_en

    temp_str = cpu_temp_str(get_cpu_temp())
    placeholder = await update.effective_message.reply_text(
        f"⏳ Analyse en cours… (`{MODEL_VISION}`) — {temp_str}",
        parse_mode="Markdown",
    )
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None, lambda: ollama_vision_stream(prompt_en, images_b64, session)
    )
    last_edit = ""
    while not future.done():
        await asyncio.sleep(STREAM_EDIT_INTERVAL)
        cur      = session.get("buffer", "")
        t_str    = cpu_temp_str(get_cpu_temp())
        cur_disp = cur + f"\n\n_{t_str}_" if cur else f"⏳ Analyse… — {t_str}"
        if cur_disp != last_edit:
            try:
                await placeholder.edit_text(_truncate(cur_disp + " ⏳", 4096), parse_mode="Markdown")
                last_edit = cur_disp
            except Exception:
                pass
    full_text = await future
    log.info(f"[GEN-V] Terminé — {len(full_text)} chars")

    kb = _vision_questions_keyboard()
    try:
        if full_text.strip():
            await placeholder.edit_text(_truncate(full_text, 4096), reply_markup=kb)
        else:
            await placeholder.edit_text("⚠️ Aucune réponse reçue.", reply_markup=kb)
    except Exception:
        await update.effective_message.reply_text(_truncate(full_text, 4096), reply_markup=kb)
    session["generating"] = False
    session["buffer"]     = ""

async def _run_text(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    prompt: str, chat_id: int,
):
    """Génération texte — historique par modèle, qualité réglable, placeholder CPU."""
    session = get_session(chat_id)
    if session["generating"]:
        await update.effective_message.reply_text("⏳ Génération en cours. Utilisez /stop.")
        return
    await _check_temp_warning(update)

    session["generating"] = True
    session["stop"]       = False
    session["buffer"]     = ""

    model       = session.get("text_model", DEFAULT_TEXT_MODEL)
    temperature = session.get("quality", TEMP_TEXT_DEFAULT)
    quality_lbl = _quality_label(temperature)

    # Historique spécifique au modèle actif
    history = get_model_history(session, chat_id, model)
    history.append({"role": "user", "content": prompt})
    history = trim_history(history)
    set_model_history(session, model, history)

    temp_str = cpu_temp_str(get_cpu_temp())
    placeholder = await update.effective_message.reply_text(
        f"⏳ Génération… (`{model}` · {quality_lbl}) — {temp_str}",
        parse_mode="Markdown",
    )
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None, lambda: ollama_text_stream(history, session, model, temperature)
    )
    last_edit = ""
    while not future.done():
        await asyncio.sleep(STREAM_EDIT_INTERVAL)
        cur      = session.get("buffer", "")
        t_str    = cpu_temp_str(get_cpu_temp())
        cur_disp = cur + f"\n\n_{t_str}_" if cur else f"⏳ Génération… — {t_str}"
        if cur_disp != last_edit:
            try:
                await placeholder.edit_text(_truncate(cur_disp + " ⏳", 4096), parse_mode="Markdown")
                last_edit = cur_disp
            except Exception:
                pass
    full_text = await future
    log.info(f"[GEN-T] Terminé — {len(full_text)} chars")

    try:
        if full_text.strip():
            await placeholder.edit_text(_truncate(full_text, 4096))
            history = get_model_history(session, chat_id, model)
            history.append({"role": "assistant", "content": full_text})
            history = trim_history(history)
            set_model_history(session, model, history)
            save_history(chat_id, model, history)
        else:
            await placeholder.edit_text("⚠️ Aucune réponse reçue.")
    except Exception:
        await update.effective_message.reply_text(_truncate(full_text, 4096))
    session["generating"] = False
    session["buffer"]     = ""

# =============================================================================
#  BOUTONS INLINE
# =============================================================================

async def handle_vision_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = get_session(chat_id)
    idx = int(query.data.split("_")[1])
    label, prompt_en = VISION_QUESTIONS[idx]
    images_b64 = session.get("last_images_b64", [])
    if not images_b64:
        await query.message.reply_text("⚠️ Aucune image en mémoire. Renvoyez une photo.")
        return
    await query.message.reply_text(f"🔍 *{label}*", parse_mode="Markdown")
    await _run_vision(update, ctx, prompt_en, images_b64, chat_id)

async def handle_relancer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Relance…")
    chat_id = query.message.chat_id
    session = get_session(chat_id)
    images_b64 = session.get("last_images_b64", [])
    prompt_en  = session.get("last_prompt_en", "Describe this image in detail.")
    if not images_b64:
        await query.message.reply_text("⚠️ Aucune image en mémoire. Renvoyez une photo.")
        return
    await query.message.reply_text("🔁 Relance…")
    await _run_vision(update, ctx, prompt_en, images_b64, chat_id)

async def handle_changer_modele_texte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    current = get_session(chat_id).get("text_model", DEFAULT_TEXT_MODEL)
    await query.message.reply_text(
        "🔄 *Choix du modèle texte :*",
        parse_mode="Markdown",
        reply_markup=_text_models_keyboard(current),
    )

async def handle_select_text_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    session = get_session(chat_id)
    model_id = query.data[3:]  # retire "tm_"
    label = next((l for m, l in MODELS_TEXT if m == model_id), model_id)
    session["text_model"] = model_id
    # Initialiser l'historique du nouveau modèle si pas encore chargé
    get_model_history(session, chat_id, model_id)
    nb_msgs = len(session["histories"].get(model_id, []))
    await query.answer(f"→ {label}")
    await query.message.reply_text(
        f"✅ Modèle texte : *{label}*\n"
        f"📝 Historique : {nb_msgs // 2} échange(s) en mémoire.",
        parse_mode="Markdown",
    )

async def handle_select_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Sélection d'un niveau de qualité des LLM via bouton inline."""
    query = update.callback_query
    chat_id = query.message.chat_id
    session = get_session(chat_id)
    val = float(query.data[3:])  # retire "ql_"
    session["quality"] = val
    label = _quality_label(val)
    await query.answer(f"Qualité : {label} ({val})")
    await query.message.reply_text(
        f"✅ *Qualité* réglée sur : \n*{label}* (`{val}`)\n"
        f"_{next((d for v, l, d in QUALITY_LEVELS if abs(v-val)<0.01), '')}_",
        parse_mode="Markdown",
    )

async def handle_effacer_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gestion des boutons d'effacement : modèle actuel ou tous."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    session = get_session(chat_id)
    if session["generating"]:
        await query.message.reply_text("⚠️ Faites /stop d'abord.")
        return
    if query.data == "eff_current":
        model = session.get("text_model", DEFAULT_TEXT_MODEL)
        session["histories"][model] = []
        path = _history_path(chat_id, model)
        if path.exists():
            path.unlink()
        await query.message.reply_text(f"🗑 Historique de `{model}` effacé.", parse_mode="Markdown")
    elif query.data == "eff_all":
        session["histories"]         = {}
        session["last_images_b64"]   = []
        session["last_prompt_en"]    = ""
        # Supprimer tous les fichiers d'historique de ce chat
        for f in HISTORY_DIR.glob(f"history_{chat_id}__*.json"):
            f.unlink()
        await query.message.reply_text("🗑🗑 Tous les historiques effacés.")

# =============================================================================
#  COMMANDES
# =============================================================================

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    temp = get_cpu_temp()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📷 Image & ❓",    callback_data="aide_vision"),
        InlineKeyboardButton("🔄 Modèle TxT", callback_data="changer_modele_texte"),
    ]])
    await update.message.reply_text(
        f"🤖 *Bot LLM — Ollama / Pi 5*\n"
        f"────────────────────────\n"
        f"📅 {now}   {cpu_temp_str(temp)}\n\n"
        f"📷 *Vision* → `{MODEL_VISION}` (qualité {TEMP_VISION_DEFAULT})\n"
        f"💬 *Texte*  → `/llm` ou texte direct\n"
        f"📄 *Fichier* → .txt ou .pdf\n\n"
        "*/llm <texte>* — Message texte\n"
        "*/modele* — Changer modèle texte\n"
        "*/qualite* — Qualité des réponses\n"
        "*/stop* — Interrompre la génération\n"
        "*/effacer* — Effacer historique(s)\n"
        "*/status* — Vérifier Ollama & 🌡 CPU",
        parse_mode="Markdown",
        reply_markup=kb,
    )

async def handle_aide_vision(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = get_session(query.message.chat_id)
    has_image = bool(session.get("last_images_b64"))
    note = "✅ Image en mémoire — cliquez pour analyser." if has_image \
        else "ℹ️ Envoyez d'abord 1 photo, puis revenez ici."
    lines = "\n".join(f"  {lbl} — _{en}_" for lbl, en in VISION_QUESTIONS)
    await query.message.reply_text(
        f"📷 *Questions disponibles :*\n{note}\n\n{lines}",
        parse_mode="Markdown",
        reply_markup=_vision_questions_keyboard(),
    )

async def cmd_modele(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current = get_session(chat_id).get("text_model", DEFAULT_TEXT_MODEL)
    await update.message.reply_text(
        f"🔄 *Modèle texte actuel :* `{current}`\n\nChoisissez :",
        parse_mode="Markdown",
        reply_markup=_text_models_keyboard(current),
    )

async def cmd_qualite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche le clavier de sélection de la qualité des LLM."""
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    current = session.get("quality", TEMP_TEXT_DEFAULT)
    label   = _quality_label(current)
    lines   = "\n".join(f"  *{l}* (`{v}`) — _{d}_" for v, l, d in QUALITY_LEVELS)
    await update.message.reply_text(
        f"🎚 *Qualité des réponses* (LLM)\n"
        f"Valeur actuelle : *{label}* (`{current}`)\n\n"
        f"📷 Vision (`{MODEL_VISION}`) : fixé à `{TEMP_VISION_DEFAULT}` (analyse factuelle)\n\n"
        f"{lines}\n\n"
        f"Choisissez :",
        parse_mode="Markdown",
        reply_markup=_quality_keyboard(current),
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(ChatAction.TYPING)
    session      = get_session(update.effective_chat.id)
    current_text = session.get("text_model", DEFAULT_TEXT_MODEL)
    quality      = session.get("quality", TEMP_TEXT_DEFAULT)
    quality_lbl  = _quality_label(quality)
    temp         = get_cpu_temp()
    temp_disp    = cpu_temp_str(temp)

    # Résumé des historiques en mémoire
    hist_lines = []
    for model_id, hist in session.get("histories", {}).items():
        nb = len([m for m in hist if m["role"] == "user"])
        hist_lines.append(f"  • `{model_id}` — {nb} échange(s)")
    hist_str = "\n".join(hist_lines) if hist_lines else "  (aucun)"

    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        installed = [m["name"] for m in r.json().get("models", [])]
        def icon(n): return "✅" if any(n.split(":")[0] in m for m in installed) else "⚠️"
        model_list = "\n".join(f"  • `{m}`" for m in installed) or "  (aucun)"
        await update.message.reply_text(
            f"🟢 *Ollama disponible*\n\n"
            f"*CPU Pi 5* : {temp_disp}\n\n"
            f"📷 Vision  : {icon(MODEL_VISION)} `{MODEL_VISION}` (`{TEMP_VISION_DEFAULT}`)\n"
            f"💬 Texte   : {icon(current_text)} `{current_text}`\n"
            f"🎚 Qualité : *{quality_lbl}* (`{quality}`)\n\n"
            f"*Historique(s) en session :*\n{hist_str}\n\n"
            f"*Modèles installés :*\n{model_list}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"🔴 *Ollama indisponible*\n`{e}`\n\n"
            f"CPU Pi 5 : {temp_disp}\n\n"
            f"Lancez : `ollama serve`",
            parse_mode="Markdown",
        )

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    if session["generating"]:
        session["stop"] = True
        await update.message.reply_text("🛑 Signal d'arrêt envoyé.")
    else:
        await update.message.reply_text("ℹ️ Aucune génération en cours.")

async def cmd_effacer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if session["generating"]:
        await update.message.reply_text("⚠️ Faites /stop d'abord.")
        return
    model = session.get("text_model", DEFAULT_TEXT_MODEL)
    await update.message.reply_text(
        f"🗑 *Effacer historique(s)*\n\n"
        f"Modèle actif : `{model}`\n\n"
        f"Que souhaitez-vous effacer ?",
        parse_mode="Markdown",
        reply_markup=_effacer_keyboard(),
    )

async def cmd_llm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args) if ctx.args else ""
    if not prompt:
        await update.message.reply_text(
            "💬 *Usage :* `/llm <votre message>`", parse_mode="Markdown",
        )
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    await _run_text(update, ctx, prompt, update.effective_chat.id)

# =============================================================================
#  HANDLERS MESSAGES
# =============================================================================

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = (update.message.caption or "").strip()

    translations = {
        "décris":  "Describe this image in detail.",
        "describe":"Describe this image in detail.",
        "qui":     "Who or what is the main subject? Describe in detail.",
        "who":     "Who or what is the main subject? Describe in detail.",
        "époque":  "What time period or era does this photo appear to be from? Explain why.",
        "date":    "What time period or era does this photo appear to be from? Explain why.",
        "texte":   "Extract and list all text visible in this image.",
        "text":    "Extract and list all text visible in this image.",
        "phrase":  "Write one single sentence that best captures this image.",
        "couleur": "Describe the colors, style, and technique used.",
        "color":   "Describe the colors, style, and technique used.",
        "analyse": "Analyze this image in detail.",
        "analyze": "Analyze this image in detail.",
    }
    prompt_en = "Describe this image in detail."
    if caption:
        low = caption.lower()
        matched = next((en for fr, en in translations.items() if fr in low), None)
        prompt_en = matched if matched else f"Answer in English. {caption}"

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text(
        f"📷 Image reçue — analyse avec `{MODEL_VISION}`…", parse_mode="Markdown",
    )

    photos = update.message.photo
    photo  = photos[-2] if len(photos) >= 2 else photos[-1]
    log.info(f"[PHOTO] taille={photo.file_size} bytes")

    file = await ctx.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await file.download_to_drive(tmp_path)
        images_b64 = [encode_image_base64(tmp_path)]
        log.info(f"[PHOTO] Base64 : {len(images_b64[0])} chars")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur téléchargement : {e}")
        return
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    await _run_vision(update, ctx, prompt_en, images_b64, chat_id)

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc     = update.message.document
    caption = (update.message.caption or "").strip()
    fname   = doc.file_name or ""
    ext     = os.path.splitext(fname)[1].lower()

    if ext not in (".txt", ".pdf"):
        await update.message.reply_text(
            f"⚠️ Format `{ext}` non pris en charge. Envoyez `.txt` ou `.pdf`.",
            parse_mode="Markdown",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text(f"📄 Fichier reçu (`{fname}`)…", parse_mode="Markdown")

    file = await ctx.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await file.download_to_drive(tmp_path)
        content = read_pdf_file(tmp_path) if ext == ".pdf" else read_text_file(tmp_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur lecture : {e}")
        return
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    question = caption if caption else "Analyse ce document et résume les points essentiels."
    prompt   = f"{question}\n\n[Contenu de {fname}]\n{content}"
    await _run_text(update, ctx, prompt, chat_id)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = (update.message.text or "").strip()
    if not prompt:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    await _run_text(update, ctx, prompt, update.effective_chat.id)

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
#  DÉMARRAGE
# =============================================================================

def main():
    token = load_token()
    app   = ApplicationBuilder().token(token).build()

    # Commandes
    app.add_handler(CommandHandler("aide",    cmd_aide))
    app.add_handler(CommandHandler("start",   cmd_aide))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("effacer", cmd_effacer))
    app.add_handler(CommandHandler("llm",     cmd_llm))
    app.add_handler(CommandHandler("modele",  cmd_modele))
    app.add_handler(CommandHandler("qualite", cmd_qualite))

    # Boutons inline
    app.add_handler(CallbackQueryHandler(handle_vision_question,      pattern=r"^vq_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_relancer,             pattern="^relancer$"))
    app.add_handler(CallbackQueryHandler(handle_aide_vision,          pattern="^aide_vision$"))
    app.add_handler(CallbackQueryHandler(handle_changer_modele_texte, pattern="^changer_modele_texte$"))
    app.add_handler(CallbackQueryHandler(handle_select_text_model,    pattern=r"^tm_.+$"))
    app.add_handler(CallbackQueryHandler(handle_select_quality,       pattern=r"^ql_.+$"))
    app.add_handler(CallbackQueryHandler(handle_effacer_choice,       pattern=r"^eff_(current|all)$"))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO,                     handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL,              handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,   handle_text))
    app.add_handler(MessageHandler(filters.COMMAND,                   cmd_inconnue))

    async def _on_start(app_ref):
        admin_id  = _get_admin_chat_id()
        if admin_id:
            ntp_ok    = await _attendre_ntp(timeout=60)
            temp      = get_cpu_temp()
            ntp_avert = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
            try:
                await app_ref.bot.send_message(
                    admin_id,
                    f"🤖 *Bot LLM Ollama démarré*\n"
                    f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M:%S ')}"
                    f"{cpu_temp_str(temp)}{ntp_avert}\n\n"
                    f"📷 Vision : `{MODEL_VISION}` (qualité `{TEMP_VISION_DEFAULT}`)\n"
                    f"💬 Texte  : `{DEFAULT_TEXT_MODEL}` (qualité `{TEMP_TEXT_DEFAULT}`)\n\n"
                    f"/aide pour les commandes.",
                    parse_mode="Markdown",
                )
            except Exception as exc:
                log.warning(f"Message démarrage impossible : {exc}")

    app.post_init = _on_start
    log.info(f"Bot démarré — vision={MODEL_VISION} texte={DEFAULT_TEXT_MODEL} "
             f"temp_vision={TEMP_VISION_DEFAULT} temp_text={TEMP_TEXT_DEFAULT}")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
