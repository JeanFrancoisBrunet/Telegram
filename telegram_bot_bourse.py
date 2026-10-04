#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Bot Telegram — Suivi Boursier  (@bourse_pi5_bot)
#  Raspberry Pi 5 - 16Go RAM - 256Go SSD NVMe - Python 3.11+
#
#  Fonctions :
#    - Suivi d'une sélection personnalisée de valeurs (CAC40, S&P500,
#      NASDAQ100, Mid-Cap, Cryptos)
#    - Alertes automatiques sur franchissement de seuil haut/bas
#    - Commandes à la demande : /cours, /liste, /top, /alerte, /suivi
#    - Graphique intraday (journée en cours) envoyé avec /cours NOM
#
#  Dépendances :
#    pip install python-telegram-bot yfinance requests matplotlib --break-system-packages
#
#  Configuration :
#    Créer ~/.telegram_config avec :        (fichier caché)
#      [telegram]
#      token_bourse = VOTRE_TOKEN
#      chat_id      = VOTRE_CHAT_ID
#
#  Auteur : Jean-François BRUNET – JFBConseils – Juin 2026
# =============================================================================

import asyncio
import json
import logging
import logging.handlers
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import yfinance as yf

import matplotlib
matplotlib.use("Agg")   # backend non-interactif — indispensable sans Tkinter
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode

# ── CONFIGURATION ──

_CONFIG_FILE = Path.home() / ".telegram_config"

def _parse_config() -> dict:
    """Lit ~/.telegram_config et retourne un dict clé/valeur."""
    config = {}
    if not _CONFIG_FILE.exists():
        return config
    for line in _CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            config[key.strip().lower()] = val.strip()
    return config

def _load_token() -> str:
    token = _parse_config().get("token_bourse", "")
    if not token:
        raise ValueError(
            "Clé 'token_bourse' introuvable dans ~/.telegram_config\n"
            "Ajoutez : token_bourse = <votre_token>"
        )
    return token

def _load_chat_id() -> int:
    val = _parse_config().get("chat_id", "0")
    return int(val) if val.lstrip("-").isdigit() else 0

BOT_TOKEN = _load_token()
CHAT_ID   = _load_chat_id()

# Fréquence de fetch en arrière-plan (secondes)
FETCH_INTERVAL    = 300   # 5 minutes

# Timeouts réseau
YF_TIMEOUT        = 15
COINGECKO_TIMEOUT = 10

# Fichier de persistance (sélection + alertes)
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
BOT_STATE_FILE = os.path.join(SCRIPT_DIR, "bourse_bot_state.json")
LOG_FILE       = os.path.join(SCRIPT_DIR, "bourse_bot.log")

# Dossier pour les images du graphique intraday
IMAGES_DIR = Path.home() / "Projects" / "Telegram" / "images_bot"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# ── LOGGING avec rotation automatique ──────────────────────────────────────────
# Le fichier bourse_bot.log est limité à 2 Mo, avec 3 sauvegardes (≈ 8 Mo max)

def _setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    # Handler avec rotation : 2 Mo max, 1 fichier de sauvegarde
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

_setup_logging()
log = logging.getLogger("bourse_bot")

# ── UNIVERS DES VALEURS ────────────────────────────────────────────────────────

CAC40_ALL = {
    "Accor": "AC.PA", "Air Liquide": "AI.PA", "Airbus": "AIR.PA",
    "Atos": "ATO.PA", "AXA": "CS.PA", "BNP Paribas": "BNP.PA",
    "Bouygues": "EN.PA", "Bureau Veritas": "BVI.PA", "Capgemini": "CAP.PA",
    "Carrefour": "CA.PA", "Crédit Agricole": "ACA.PA", "Danone": "BN.PA",
    "Dassault Systèmes": "DSY.PA", "Eiffage": "FGR.PA", "Engie": "ENGI.PA",
    "Essilor": "EL.PA", "Eurofins": "ERF.PA",
    "Euronext": "ENX.PA", "Hermès": "RMS.PA", "Kering": "KER.PA",
    "L'Oréal": "OR.PA", "LVMH": "MC.PA", "Legrand": "LR.PA",
    "Michelin": "ML.PA", "Orange": "ORA.PA", "Pernod Ricard": "RI.PA",
    "Publicis": "PUB.PA", "Renault": "RNO.PA", "Safran": "SAF.PA",
    "Saint-Gobain": "SGO.PA", "Sanofi": "SAN.PA",
    "Schneider Electric": "SU.PA", "Société Générale": "GLE.PA",
    "Teleperformance": "TEP.PA", "Thales": "HO.PA",
    "TotalEnergies": "TTE.PA", "Unibail": "URW.PA",
    "Veolia": "VIE.PA", "Vinci": "DG.PA", "Worldline": "WLN.PA",
}

MIDCAP_SYMBOLS = {"LISI": "FII.PA"}

SP500_SYMBOLS = {
    "3M": "MMM", "Abbott Laboratories": "ABT", "AbbVie": "ABBV",
    "Accenture": "ACN", "Adobe": "ADBE", "Airbnb": "ABNB",
    "Alphabet": "GOOGL", "Altria": "MO", "Amazon": "AMZN",
    "Amcor": "AMCR", "AMD": "AMD", "American Express": "AXP",
    "Amgen": "AMGN", "Apple": "AAPL", "AT&T": "T",
    "Autodesk": "ADSK", "Baker Hughes": "BKR", "Bank of America": "BAC",
    "Baxter International": "BAX", "Berkshire Hathaway": "BRK-B",
    "BlackRock": "BLK", "Blackstone": "BX", "Boeing": "BA",
    "Boston Scientific": "BSX", "Bristol Myers Squibb": "BMY",
    "Broadcom": "AVGO", "Carrier Global": "CARR", "Caterpillar": "CAT",
    "Charles River Laboratories": "CRL", "Charles Schwab": "SCHW",
    "Chevron": "CVX", "Cisco": "CSCO", "Citigroup": "C",
    "Coca-Cola": "KO", "Danaher": "DHR", "Deere": "DE",
    "Dell Technologies": "DELL", "Delta Air Lines": "DAL", "DuPont": "DD",
    "eBay": "EBAY", "Ecolab": "ECL", "ExxonMobil": "XOM",
    "FedEx": "FDX", "Ford Motor": "F", "Fox Corp.": "FOXA",
    "Garmin": "GRMN", "GE HealthCare": "GEHC", "General Electric": "GE",
    "General Motors": "GM", "Goldman Sachs": "GS", "Hasbro": "HAS",
    "Hewlett Packard Enterprise": "HPE", "Hilton Worldwide": "HLT",
    "Home Depot": "HD", "Honeywell": "HON", "Howmet Aerospace": "HWM",
    "HP Inc.": "HPQ", "IBM": "IBM", "Ingersoll Rand": "IR",
    "Intel": "INTC", "Johnson & Johnson": "JNJ", "Johnson Controls": "JCI",
    "JPMorgan Chase": "JPM", "Lilly (Eli)": "LLY", "Linde": "LIN",
    "Lockheed Martin": "LMT", "Lowe's": "LOW", "M&T Bank": "MTB",
    "Marriott International": "MAR", "Mastercard": "MA",
    "McDonald's": "MCD", "Medtronic": "MDT", "Merck & Co.": "MRK",
    "Meta Platforms": "META", "MGM Resorts": "MGM", "Microsoft": "MSFT",
    "Moody's": "MCO", "Morgan Stanley": "MS", "Motorola Solutions": "MSI",
    "Nasdaq Inc.": "NDAQ", "Netflix": "NFLX", "NextEra Energy": "NEE",
    "Nike": "NKE", "NVIDIA": "NVDA", "NXP Semiconductors": "NXPI",
    "Oracle": "ORCL", "Palo Alto Networks": "PANW",
    "Paramount Skydance": "PSKY", "PayPal": "PYPL", "PepsiCo": "PEP",
    "Pfizer": "PFE", "Philip Morris": "PM", "Procter & Gamble": "PG",
    "Prologis": "PLD", "PTC Inc.": "PTC", "Qualcomm": "QCOM",
    "Ralph Lauren": "RL", "Rockwell Automation": "ROK", "RTX": "RTX",
    "S&P Global": "SPGI", "Salesforce": "CRM", "Sandisk": "SNDK",
    "Stanley Black & Decker": "SWK", "Starbucks": "SBUX",
    "Stryker": "SYK", "Sysco": "SYY", "TE Connectivity": "TEL",
    "Tesla": "TSLA", "Texas Instruments": "TXN", "Textron": "TXT",
    "Thermo Fisher": "TMO", "T-Mobile US": "TMUS",
    "Travelers Companies": "TRV", "Uber": "UBER", "UnitedHealth": "UNH",
    "UPS": "UPS", "Verizon": "VZ", "Visa": "V", "Walmart": "WMT",
    "Walt Disney": "DIS", "Warner Bros. Discovery": "WBD",
    "Western Digital": "WDC", "Zimmer Biomet": "ZBH",
}

NASDAQ100_SYMBOLS = {
    "ADP": "ADP", "Booking Holdings": "BKNG", "CSX": "CSX",
    "Gilead Sciences": "GILD", "Intuit": "INTU",
    "Intuitive Surgical": "ISRG", "Keurig Dr Pepper": "KDP",
    "Mondelez": "MDLZ", "Regeneron": "REGN", "Vertex Pharma": "VRTX",
    "SpaceX": "SPCX",
}

CRYPTO_IDS = {
    "Bitcoin": "bitcoin", "Ethereum": "ethereum", "BNB": "binancecoin",
    "Solana": "solana", "XRP": "ripple", "Cardano": "cardano",
}

# Symboles yfinance pour les cryptos — utilisés pour le graphique intraday
CRYPTO_YF_SYMBOLS = {
    "Bitcoin":  "BTC-EUR",
    "Ethereum": "ETH-EUR",
    "BNB":      "BNB-EUR",
    "Solana":   "SOL-EUR",
    "XRP":      "XRP-EUR",
    "Cardano":  "ADA-EUR",
}

# Indice CAC 40 — toujours affiché dans /suivi (hors watchlist)
CAC40_INDEX_SYMBOL = "^FCHI"

# Univers complet nom → ticker
ALL_SYMBOLS: dict[str, str] = {}
ALL_SYMBOLS.update(CAC40_ALL)
ALL_SYMBOLS.update(MIDCAP_SYMBOLS)
ALL_SYMBOLS.update(SP500_SYMBOLS)
ALL_SYMBOLS.update(NASDAQ100_SYMBOLS)

# Index inversé ticker → nom pour recherche par ticker
TICKER_TO_NAME: dict[str, str] = {v: k for k, v in ALL_SYMBOLS.items()}

# Sélection par défaut au premier lancement
DEFAULT_WATCHLIST = [
    "LVMH", "TotalEnergies", "Sanofi", "Airbus", "Schneider Electric",
    "NVIDIA", "Apple", "Microsoft", "Tesla", "SpaceX", "Amazon",
    "Bitcoin", "Ethereum",
]

# ── ÉTAT GLOBAL ──

_state_lock = threading.Lock()

_state: dict = {
    "watchlist":  DEFAULT_WATCHLIST.copy(),
    "alerts":     {},
    "_triggered": {},
}

_cache_lock  = threading.Lock()
_price_cache: dict = {}

# ── PERSISTANCE ──

def _load_state():
    global _state
    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        with _state_lock:
            _state["watchlist"]  = saved.get("watchlist",  DEFAULT_WATCHLIST.copy())
            _state["alerts"]     = saved.get("alerts",     {})
            _state["_triggered"] = saved.get("_triggered", {})
        log.info(f"État chargé : {len(_state['watchlist'])} valeurs, "
                 f"{len(_state['alerts'])} alertes")
    except (FileNotFoundError, json.JSONDecodeError):
        log.info("Pas d'état sauvegardé — démarrage avec valeurs par défaut.")

def _save_state():
    try:
        with _state_lock:
            to_save = {
                "watchlist":  _state["watchlist"],
                "alerts":     _state["alerts"],
                "_triggered": _state["_triggered"],
            }
        with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Erreur sauvegarde état : {e}")

# ── FETCH COURS ──

def _fetch_one_yf(name: str, symbol: str) -> tuple[str, dict | None]:
    def _inner():
        info = yf.Ticker(symbol).fast_info
        return info.last_price, info.previous_close

    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_inner)
        try:
            price, prev = future.result(timeout=YF_TIMEOUT)
        except TimeoutError:
            log.warning(f"{name} ({symbol}) : timeout yfinance")
            return name, None
        except Exception as e:
            log.warning(f"{name} ({symbol}) : {type(e).__name__} — {e}")
            return name, None

    if price and prev and prev > 0:
        return name, {
            "price":      float(price),
            "change_pct": (price - prev) / prev * 100,
            "ts":         time.time(),
            "source":     "yf",
        }
    return name, None

def _fetch_crypto_coingecko(names: list[str]) -> dict[str, dict | None]:
    ids_needed = {n: CRYPTO_IDS[n] for n in names if n in CRYPTO_IDS}
    if not ids_needed:
        return {}
    ids_str = ",".join(ids_needed.values())
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids_str}&vs_currencies=eur&include_24hr_change=true"
    )
    try:
        resp = requests.get(url, timeout=COINGECKO_TIMEOUT)
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", 5))
            log.warning(f"CoinGecko 429 — réessai dans {retry}s")
            time.sleep(retry)
            resp = requests.get(url, timeout=COINGECKO_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = {}
        for name, cg_id in ids_needed.items():
            d = data.get(cg_id, {})
            results[name] = {
                "price":      d.get("eur"),
                "change_pct": d.get("eur_24h_change"),
                "ts":         time.time(),
                "source":     "coingecko",
            } if d else None
        return results
    except Exception as e:
        log.error(f"CoinGecko fetch : {e}")
        return {n: None for n in names}

def fetch_watchlist() -> dict[str, dict | None]:
    with _state_lock:
        watchlist = list(_state["watchlist"])

    yf_names     = [n for n in watchlist if n in ALL_SYMBOLS]
    crypto_names = [n for n in watchlist if n in CRYPTO_IDS]
    results: dict[str, dict | None] = {}

    if yf_names:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                ex.submit(_fetch_one_yf, name, ALL_SYMBOLS[name]): name
                for name in yf_names
            }
            for future in as_completed(futures):
                name, data = future.result()
                results[name] = data

    if crypto_names:
        results.update(_fetch_crypto_coingecko(crypto_names))

    return results

def _update_cache(data: dict[str, dict | None]):
    with _cache_lock:
        for name, d in data.items():
            if d is not None:
                _price_cache[name] = d

def get_cached(name: str) -> dict | None:
    with _cache_lock:
        return _price_cache.get(name)

# ── GRAPHIQUE INTRADAY ──

def _generate_intraday_chart(name: str, symbol: str) -> Path | None:
    """Génère un graphique intraday (journée en cours, intervalles 5 min).
    Fonctionne pour les actions & les cryptos (via symboles yfinance BTC-EUR etc.).
    Retourne le chemin du fichier PNG, ou None en cas d'échec."""
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="5m", auto_adjust=True)
        if df is None or df.empty:
            log.warning(f"Graphique {name} : historique intraday vide")
            return None
        if len(df) < 2:
            log.warning(f"Graphique {name} : pas assez de points ({len(df)})")
            return None

        closes    = df["Close"].values
        dates     = df.index.to_pydatetime()
        first     = float(closes[0])
        last      = float(closes[-1])
        total_pct = (last - first) / first * 100 if first != 0 else 0.0

        # Couleurs selon sens de variation (identiques au dashboard)
        line_col = "#4ade80" if total_pct >= 0 else "#f87171"
        fill_col = "#0d2a14" if total_pct >= 0 else "#2a0d0d"
        BG_FIG   = "#0d0f14"
        BG_AX    = "#13161e"
        TXT      = "#9ca3af"
        GRID     = "#1f2535"

        fig = plt.figure(figsize=(7, 3.8), dpi=110, facecolor=BG_FIG)
        ax_p = fig.add_axes([0.07, 0.30, 0.89, 0.60], facecolor=BG_AX)
        ax_v = fig.add_axes([0.07, 0.06, 0.89, 0.19], facecolor=BG_AX, sharex=ax_p)

        # Courbe prix + remplissage sous la courbe
        ax_p.plot(dates, closes, color=line_col, linewidth=1.5, zorder=3)
        ax_p.fill_between(dates, closes, closes.min() * 0.999,
                          color=fill_col, alpha=0.55, zorder=2)

        # Annotations min / max
        i_max  = int(closes.argmax())
        i_min  = int(closes.argmin())
        crypto = name in CRYPTO_YF_SYMBOLS
        def _fmt(v):
            if crypto and v < 1:   return f"{v:,.4f}"
            if crypto and v < 100: return f"{v:,.2f}"
            return f"{v:,.2f}"
        ax_p.annotate(_fmt(closes[i_max]),
                      xy=(dates[i_max], closes[i_max]),
                      xytext=(0, 6), textcoords="offset points",
                      color="#4ade80", fontsize=7, ha="center", va="bottom")
        ax_p.annotate(_fmt(closes[i_min]),
                      xy=(dates[i_min], closes[i_min]),
                      xytext=(0, -10), textcoords="offset points",
                      color="#f87171", fontsize=7, ha="center", va="top")

        # Titre avec variation
        arrow = "▲" if total_pct >= 0 else "▼"
        ts    = datetime.now().strftime("%d/%m %H:%M")
        ax_p.set_title(
            f"{name}  ·  Journée  {arrow} {total_pct:+.2f}%   ({ts})",
            color="#f0c040", fontsize=9, fontweight="bold", pad=5
        )
        ax_p.tick_params(colors=TXT, labelsize=6, labelbottom=False)
        ax_p.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
        ax_p.yaxis.tick_right()
        ax_p.grid(True, color=GRID, linewidth=0.5, linestyle="--", zorder=1)
        for sp in ax_p.spines.values(): sp.set_edgecolor(GRID)

        # Histogramme volume (vert si clôture ≥ ouverture, rouge sinon)
        if "Volume" in df.columns and df["Volume"].sum() > 0:
            vol_col = ["#4ade80" if float(c) >= float(o) else "#f87171"
                       for c, o in zip(df["Close"], df["Open"])]
            # largeur des barres en fraction de jour (5 min = 5/1440)
            ax_v.bar(dates, df["Volume"].values,
                     color=vol_col, alpha=0.7, width=0.003, zorder=2)
        ax_v.set_ylabel("Vol", color=TXT, fontsize=5, labelpad=2)
        ax_v.tick_params(colors=TXT, labelsize=5)
        ax_v.yaxis.tick_right()
        ax_v.yaxis.set_major_formatter(
            mticker.FuncFormatter(
                lambda x, _: f"{x/1e6:.1f}M" if x >= 1e6 else f"{x/1e3:.0f}k"
            )
        )
        ax_v.grid(True, color=GRID, linewidth=0.4, linestyle="--", zorder=1)
        for sp in ax_v.spines.values(): sp.set_edgecolor(GRID)

        # Axe X : heures
        ax_v.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_v.xaxis.set_major_locator(mdates.HourLocator(interval=1))
        ax_v.tick_params(axis="x", labelsize=6, colors=TXT, rotation=0)

        # Sauvegarde — un fichier par valeur, écrasé à chaque appel
        safe_name = name.replace(" ", "_").replace("/", "-")
        out = IMAGES_DIR / f"intraday_{safe_name}.png"
        fig.savefig(out, dpi=110, bbox_inches="tight", facecolor=BG_FIG)
        plt.close(fig)
        log.info(f"Graphique intraday généré : {out}")
        return out

    except Exception as e:
        log.error(f"Graphique intraday {name} : {type(e).__name__} — {e}")
        return None

# ── FORMATAGE ──

def _fmt_price(value, crypto=False) -> str:
    if value is None:
        return "---"
    if crypto and value >= 1000:
        return f"{value:,.0f} €"
    if crypto and value >= 1:
        return f"{value:,.2f} €"
    if crypto:
        return f"{value:,.4f} €"
    return f"{value:,.2f} €"

def _fmt_pct(value) -> str:
    if value is None:
        return "---"
    arrow = "▲" if value >= 0 else "▼"
    return f"{arrow} {value:+.2f}%"

def _pct_emoji(value) -> str:
    if value is None:   return "⬜"
    if value >= 2:      return "🟢"
    if value >= 0:      return "⚪"      # 🔵
    if value >= -2:     return "🟠"      # 🟡
    return "🔴"

def _line(name: str, d: dict | None, crypto=False) -> str:
    if d is None:
        return f"⬜ *{name}* — indisponible"
    emoji = _pct_emoji(d.get("change_pct"))
    price = _fmt_price(d.get("price"), crypto=crypto)
    pct   = _fmt_pct(d.get("change_pct"))
    return f"{emoji} *{name}* : {price}  `{pct}`"

def _resolve_name(query: str) -> str | None:
    q = query.strip()
    for name in ALL_SYMBOLS:
        if name.lower() == q.lower():
            return name
    ticker_up = q.upper()
    if ticker_up in TICKER_TO_NAME:
        return TICKER_TO_NAME[ticker_up]
    for name in ALL_SYMBOLS:
        if q.lower() in name.lower():
            return name
    for name in CRYPTO_IDS:
        if q.lower() in name.lower():
            return name
    return None

# ── VÉRIFICATION ALERTES ──

async def _check_alerts(bot: Bot, data: dict[str, dict | None]):
    with _state_lock:
        alerts    = dict(_state["alerts"])
        triggered = dict(_state["_triggered"])

    new_triggers: dict[str, dict] = {}
    messages: list[str] = []

    for name, thresholds in alerts.items():
        d = data.get(name) or get_cached(name)
        if d is None:
            continue
        price = d.get("price")
        if price is None:
            continue

        t      = triggered.get(name, {"low": False, "high": False})
        low    = thresholds.get("low")
        high   = thresholds.get("high")
        crypto = name in CRYPTO_IDS

        if low is not None and price <= low and not t["low"]:
            messages.append(
                f"🔴 *ALERTE SEUIL BAS* — *{name}*\n"
                f"Cours : {_fmt_price(price, crypto)}  ≤  seuil {_fmt_price(low, crypto)}\n"
                f"Variation : `{_fmt_pct(d.get('change_pct'))}`"
            )
            new_triggers[name] = {**t, "low": True}
        elif low is not None and price > low * 1.005 and t["low"]:
            new_triggers[name] = {**t, "low": False}

        if high is not None and price >= high and not t.get("high", False):
            messages.append(
                f"🟢 *ALERTE SEUIL HAUT* — *{name}*\n"
                f"Cours : {_fmt_price(price, crypto)}  ≥  seuil {_fmt_price(high, crypto)}\n"
                f"Variation : `{_fmt_pct(d.get('change_pct'))}`"
            )
            nt = new_triggers.get(name, dict(t))
            new_triggers[name] = {**nt, "high": True}
        elif high is not None and price < high * 0.995 and t.get("high", False):
            nt = new_triggers.get(name, dict(t))
            new_triggers[name] = {**nt, "high": False}

    if new_triggers:
        with _state_lock:
            _state["_triggered"].update(new_triggers)
        _save_state()

    for msg in messages:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=msg,
                                   parse_mode=ParseMode.MARKDOWN)
            log.info(f"Alerte envoyée : {msg[:60]}…")
        except Exception as e:
            log.error(f"Erreur envoi alerte : {e}")

# ── ATTENTE NTP ──

async def _attendre_ntp(timeout: int = 60) -> bool:
    """Attend que l'horloge système soit synchronisée NTP avant de démarrer."""
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

# ── BOUCLE ARRIÈRE-PLAN ──

_bg_stop = threading.Event()

def _background_loop(bot: Bot):
    loop = asyncio.new_event_loop()
    log.info(f"Boucle arrière-plan démarrée (intervalle : {FETCH_INTERVAL}s)")
    while not _bg_stop.is_set():
        try:
            data = fetch_watchlist()
            _update_cache(data)
            loop.run_until_complete(_check_alerts(bot, data))
            ok = sum(1 for v in data.values() if v)
            log.info(f"Fetch OK — {ok}/{len(data)} valeurs")
        except Exception as e:
            log.error(f"Erreur boucle arrière-plan : {e}")
        _bg_stop.wait(timeout=FETCH_INTERVAL)
    loop.close()
    log.info("Boucle arrière-plan arrêtée.")

# ── SÉCURITÉ ──

def _auth(update: Update) -> bool:
    if CHAT_ID == 0:
        return True
    return update.effective_chat.id == CHAT_ID

# ── COMMANDES TELEGRAM ──

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text(
        "📈 *Bot Suivi Bourse* — Cdes dispo. :\n\n"
        "`/suivi` — affiche liste suivie\n"
        "`/ajouter NOM` — ajoute valeur / liste\n"
        "`/retirer NOM` — retire valeur / liste\n"
        "`/cours NOM` — cours & graph\n"
        "`/liste CAC40|SP500|NASDAQ|\n       CRYPTO|MIDCAP`\n"
        "`/top` — meilleures & pires variations\n"
        "`/alerte NOM BAS HAUT` — seuils\n"
        "`/alerte NOM` — gestion d'une alerte\n"
        "`/alertes` — voir les alertes actives\n"
        "`/refresh` — force un fetch immédiat\n"
        "`/aide` — ce mémo",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_suivi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text("⏳ Récupération des cours…")

    with _state_lock:
        watchlist = list(_state["watchlist"])

    if not watchlist:
        await update.message.reply_text(
            "La liste de suivi est vide.\nUtilise `/ajouter NOM`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Fetch watchlist + CAC40 index en parallèle
    data = fetch_watchlist()
    _update_cache(data)

    _, cac40_data = _fetch_one_yf("CAC 40", CAC40_INDEX_SYMBOL)

    ts    = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"📊 *Suivi — {ts}*\n"]

    # ── Ligne CAC 40 toujours en tête ──
    if cac40_data:
        emoji = _pct_emoji(cac40_data.get("change_pct"))
        price = f"{cac40_data['price']:,.2f} pts"
        pct   = _fmt_pct(cac40_data.get("change_pct"))
        lines.append(f"{emoji} *CAC 40* : {price}  `{pct}`")
    else:
        lines.append("⬜ *CAC 40* — indisponible")
    lines.append("")   # ligne vide de séparation

    for name in watchlist:
        lines.append(_line(name, data.get(name), name in CRYPTO_IDS))

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_cours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if not ctx.args:
        await update.message.reply_text(
            "Usage : `/cours NOM_OU_TICKER`\nEx : `/cours LVMH` ou `/cours NVDA`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    query = " ".join(ctx.args)
    name  = _resolve_name(query)
    if name is None:
        await update.message.reply_text(
            f"❌ Valeur *{query}* introuvable.\nUtilise `/liste` pour voir les valeurs disponibles.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.message.reply_text(f"⏳ Fetch {name}…")
    crypto = name in CRYPTO_IDS

    if crypto:
        data = _fetch_crypto_coingecko([name])
        d    = data.get(name)
    else:
        _, d = _fetch_one_yf(name, ALL_SYMBOLS[name])

    if d:
        _update_cache({name: d})

    ticker = ALL_SYMBOLS.get(name, CRYPTO_IDS.get(name, ""))
    ts     = datetime.now().strftime("%d/%m %H:%M")
    msg = (
        f"{'₿' if crypto else '📈'} *{name}*  `({ticker})`\n"
        f"Cours     : *{_fmt_price(d.get('price') if d else None, crypto)}*\n"
        f"Variation : `{_fmt_pct(d.get('change_pct') if d else None)}`\n"
        f"_Mis à jour : {ts}_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # ── Graphique intraday ──
    # Pour les actions : symbole yfinance direct (ex. "MC.PA")
    # Pour les cryptos  : symbole yfinance en EUR  (ex. "BTC-EUR")
    yf_symbol = ALL_SYMBOLS.get(name) or CRYPTO_YF_SYMBOLS.get(name)
    if yf_symbol:
        loop       = asyncio.get_event_loop()
        chart_path = await loop.run_in_executor(
            None, _generate_intraday_chart, name, yf_symbol
        )
        if chart_path and chart_path.exists():
            try:
                with open(chart_path, "rb") as img:
                    await update.message.reply_photo(photo=img)
            except Exception as e:
                log.warning(f"Envoi photo {name} : {e}")
        else:
            # Hors séance ou données insuffisantes — pas de message d'erreur intrusif
            log.info(f"Graphique {name} non disponible (hors séance ou données vides)")

async def cmd_ajouter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if not ctx.args:
        await update.message.reply_text("Usage : `/ajouter NOM_OU_TICKER`", parse_mode=ParseMode.MARKDOWN)
        return

    name = _resolve_name(" ".join(ctx.args))
    if name is None:
        await update.message.reply_text(
            f"❌ *{' '.join(ctx.args)}* introuvable.", parse_mode=ParseMode.MARKDOWN
        )
        return

    with _state_lock:
        if name in _state["watchlist"]:
            await update.message.reply_text(
                f"ℹ️ *{name}* est déjà dans le suivi.", parse_mode=ParseMode.MARKDOWN
            )
            return
        _state["watchlist"].append(name)

    _save_state()
    await update.message.reply_text(
        f"✅ *{name}* ajouté au suivi ({len(_state['watchlist'])} valeurs).",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_retirer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if not ctx.args:
        await update.message.reply_text("Usage : `/retirer NOM_OU_TICKER`", parse_mode=ParseMode.MARKDOWN)
        return

    name = _resolve_name(" ".join(ctx.args))
    if name is None:
        await update.message.reply_text(f"❌ introuvable.", parse_mode=ParseMode.MARKDOWN)
        return

    with _state_lock:
        if name not in _state["watchlist"]:
            await update.message.reply_text(
                f"ℹ️ *{name}* n'est pas dans le suivi.", parse_mode=ParseMode.MARKDOWN
            )
            return
        _state["watchlist"].remove(name)
        _state["alerts"].pop(name, None)
        _state["_triggered"].pop(name, None)

    _save_state()
    await update.message.reply_text(f"🗑 *{name}* retiré du suivi.", parse_mode=ParseMode.MARKDOWN)

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return

    univers_map = {
        "CAC40":  CAC40_ALL,
        "SP500":  SP500_SYMBOLS,
        "NASDAQ": NASDAQ100_SYMBOLS,
        "MIDCAP": MIDCAP_SYMBOLS,
        "CRYPTO": {n: n for n in CRYPTO_IDS},
    }

    if not ctx.args:
        counts = {k: len(v) for k, v in univers_map.items()}
        msg = (
            "📋 <b>Univers disponible</b> — précise l'index :\n\n"
            + "\n".join(f"<code>/liste {k}</code> — {v} valeurs" for k, v in counts.items())
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    key = ctx.args[0].upper()
    if key not in univers_map:
        await update.message.reply_text(
            "Index inconnu. Choix : <code>CAC40</code>, <code>SP500</code>, "
            "<code>NASDAQ</code>, <code>MIDCAP</code>, <code>CRYPTO</code>",
            parse_mode=ParseMode.HTML
        )
        return

    universe = univers_map[key]
    with _state_lock:
        watchlist = set(_state["watchlist"])

    lines = [f"📋 <b>{key}</b> — {len(universe)} valeurs\n"]
    for name, ticker in sorted(universe.items()):
        mark = "✔" if name in watchlist else "  "
        lines.append(f"<code>{mark}</code> {name}  <code>{ticker}</code>")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        current = lines[0]
        for line in lines[1:]:
            if len(current) + len(line) + 1 > 4000:
                await update.message.reply_text(current, parse_mode=ParseMode.HTML)
                current = line
            else:
                current += "\n" + line
        await update.message.reply_text(current, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text("⏳ Calcul du classement…")

    data = fetch_watchlist()
    _update_cache(data)

    ranked = [
        (name, d["change_pct"], d["price"], name in CRYPTO_IDS)
        for name, d in data.items()
        if d and d.get("change_pct") is not None
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)

    if not ranked:
        await update.message.reply_text("Aucune donnée disponible.")
        return

    ts    = datetime.now().strftime("%d/%m %H:%M")
    lines = [f"🏆 *Classement — {ts}*\n"]
    lines.append("*▲ Meilleures performances :*")
    for i, (name, pct, price, crypto) in enumerate(ranked[:3], 1):
        lines.append(f"  {i}. {_pct_emoji(pct)} *{name}* : {_fmt_price(price, crypto)}  `{_fmt_pct(pct)}`")
    lines.append("\n*▼ Moins bonnes performances :*")
    for i, (name, pct, price, crypto) in enumerate(ranked[-3:][::-1], 1):
        lines.append(f"  {i}. {_pct_emoji(pct)} *{name}* : {_fmt_price(price, crypto)}  `{_fmt_pct(pct)}`")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_alerte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if not ctx.args:
        await update.message.reply_text(
            "Usage :\n"
            "`/alerte NOM BAS HAUT` — seuils\n"
            "`/alerte NOM` — voir / suppress.\n\n"
            "Exemples :\n"
            "`/alerte LVMH 580 700`\n"
            "`/alerte Bitcoin 50000 90000`\n"
            "`/alerte LVMH supprimer`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    args = ctx.args
    low_val = high_val = None
    cmd_word = None

    if args[-1].lower() in ("supprimer", "delete", "effacer"):
        cmd_word  = "supprimer"
        name_args = args[:-1]
    else:
        numeric_tail = []
        for a in reversed(args):
            try:
                numeric_tail.insert(0, float(a.replace(",", ".")))
            except ValueError:
                break
        name_args = args[: len(args) - len(numeric_tail)]
        if len(numeric_tail) == 2:
            low_val, high_val = numeric_tail[0], numeric_tail[1]
        elif len(numeric_tail) == 1:
            low_val = numeric_tail[0]

    if not name_args:
        await update.message.reply_text("❌ Impossible de déterminer le nom de la valeur.", parse_mode=ParseMode.MARKDOWN)
        return

    name = _resolve_name(" ".join(name_args))
    if name is None:
        await update.message.reply_text(f"❌ *{' '.join(name_args)}* introuvable.", parse_mode=ParseMode.MARKDOWN)
        return

    if cmd_word == "supprimer":
        with _state_lock:
            removed = _state["alerts"].pop(name, None)
            _state["_triggered"].pop(name, None)
        _save_state()
        msg = f"🗑 Alerte *{name}* supprimée." if removed else f"ℹ️ Pas d'alerte définie pour *{name}*."
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    if low_val is None and high_val is None:
        with _state_lock:
            existing = _state["alerts"].get(name)
        if existing:
            crypto = name in CRYPTO_IDS
            l = _fmt_price(existing.get("low"),  crypto) if existing.get("low")  is not None else "—"
            h = _fmt_price(existing.get("high"), crypto) if existing.get("high") is not None else "—"
            await update.message.reply_text(
                f"🔔 *Alerte {name}*\nSeuil bas : {l}\nSeuil haut : {h}\n\n"
                f"Pour supprimer :\n`/alerte {name} supprimer`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                f"ℹ️ Pas d'alerte définie pour *{name}*.\nUsage : `/alerte {name} BAS HAUT`",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    crypto = name in CRYPTO_IDS
    with _state_lock:
        _state["alerts"][name]     = {"low": low_val, "high": high_val}
        _state["_triggered"][name] = {"low": False, "high": False}
        if name not in _state["watchlist"]:
            _state["watchlist"].append(name)

    _save_state()
    l_str = _fmt_price(low_val,  crypto) if low_val  is not None else "—"
    h_str = _fmt_price(high_val, crypto) if high_val is not None else "—"
    await update.message.reply_text(
        f"✅ *Alerte définie — {name}*\n"
        f"🔴 Seuil bas  : {l_str}\n"
        f"🟢 Seuil haut : {h_str}\n",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_alertes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    with _state_lock:
        alerts    = dict(_state["alerts"])
        triggered = dict(_state["_triggered"])

    if not alerts:
        await update.message.reply_text(
            "Aucune alerte définie.\nUtilise `/alerte NOM BAS HAUT`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = ["🔔 *Alertes actives*\n"]
    for name, th in alerts.items():
        crypto    = name in CRYPTO_IDS
        d         = get_cached(name)
        price_str = f"cours actuel : {_fmt_price(d['price'], crypto)}" if d else "cours non chargé"
        t         = triggered.get(name, {})
        l_str = (_fmt_price(th.get("low"), crypto) + (" 🔴 déclenchée" if t.get("low") else "")) if th.get("low") is not None else "—"
        h_str = (_fmt_price(th.get("high"), crypto) + (" 🟢 déclenchée" if t.get("high") else "")) if th.get("high") is not None else "—"
        lines.append(f"*{name}* _{price_str}_\n Bas : {l_str}  |  Haut : {h_str}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text("🔄 Fetch en cours…")
    data = fetch_watchlist()
    _update_cache(data)
    ok    = sum(1 for v in data.values() if v)
    ts    = datetime.now().strftime("%d/%m %H:%M:%S")
    await update.message.reply_text(f"✅ Fetch terminé : {ok}/{len(data)} valeurs Ok\n      {ts}")
    await _check_alerts(ctx.application.bot, data)

async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await update.message.reply_text(
        "Commande inconnue. Tape `/aide` pour la liste des commandes.",
        parse_mode=ParseMode.MARKDOWN
    )

# ── POINT D'ENTRÉE ──

def main():
    _load_state()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("aide",     cmd_aide))
    app.add_handler(CommandHandler("suivi",    cmd_suivi))
    app.add_handler(CommandHandler("cours",    cmd_cours))
    app.add_handler(CommandHandler("ajouter",  cmd_ajouter))
    app.add_handler(CommandHandler("retirer",  cmd_retirer))
    app.add_handler(CommandHandler("liste",    cmd_liste))
    app.add_handler(CommandHandler("top",      cmd_top))
    app.add_handler(CommandHandler("alerte",   cmd_alerte))
    app.add_handler(CommandHandler("alertes",  cmd_alertes))
    app.add_handler(CommandHandler("refresh",  cmd_refresh))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    # Message de démarrage + attente NTP
    async def _on_start(app_ref):
        ntp_ok    = await _attendre_ntp(timeout=60)
        now       = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ntp_avert = "" if ntp_ok else "\n⚠️ Heure non synchronisée NTP"
        if CHAT_ID:
            try:
                await app_ref.bot.send_message(
                    CHAT_ID,
                    f"📈 *Bot Suivi Bourse démarré*\n"
                    f"📅 {now}{ntp_avert}\n\n"
                    f"{len(_state['watchlist'])} valeurs en suivi.\n"
                    f"Fetch automatique toutes les {FETCH_INTERVAL // 60} min.\n"
                    f"Tapez /suivi pour voir les cours\n      ou /aide",
                    parse_mode=ParseMode.MARKDOWN
                )
                log.info(f"Message de démarrage envoyé — chat_id {CHAT_ID}")
            except Exception as exc:
                log.warning(f"Message de démarrage impossible : {exc}")

    app.post_init = _on_start

    # Lancement de la boucle arrière-plan
    bg_thread = threading.Thread(
        target=_background_loop, args=(app.bot,), daemon=True, name="BourseBot-BG"
    )
    bg_thread.start()

    log.info("Bot Bourse démarré — polling Telegram…")
    print("✅ Bot Bourse démarré. Ctrl+C pour arrêter.")
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        _bg_stop.set()
        bg_thread.join(timeout=5)
        log.info("Bot arrêté proprement.")

if __name__ == "__main__":
    main()
