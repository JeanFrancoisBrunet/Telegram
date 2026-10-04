#!/usr/bin/env python3
# =============================================================================
#  motion_mode_manager.py  –  Fichier Source unique pour le mode Motion
#
#  Utilisé par :
#    - pilotage_cameras_PI5_v6.py  (GUI Tkinter)
#    - telegram_bot_motion_imx708.py  (Bot Telegram)
#    - tout futur programme interagissant avec Motion
#
#  Principe :
#    Un seul programme à la fois peut écrire motion.conf grâce à un verrou
#    fichier POSIX (fcntl.flock).  L'état courant est également persisté dans
#    motion_mode.json (lecture rapide sans parser motion.conf).
#
#  Modes :
#    "stream"      → movie_output off   (Flux continu HTTP 8081, pour Visio…)
#    "record"      → movie_output on    (Enregistrement .mkv sur détection)
#
#  Auteur : Jean-François BRUNET – JFBConseils – Juin 2026
# =============================================================================

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
_MOTION_CONF_CANDIDATES = [
    "/etc/motion/motion.conf",
    str(Path.home() / ".motion/motion.conf"),
]

# Fichier d'état rapide (même répertoire que ce module)
_STATE_FILE = Path(__file__).parent / "motion_mode.json"

# Verrou inter-processus (fichier séparé pour ne pas verrouiller motion.conf)
_LOCK_FILE = Path(__file__).parent / "motion_mode.lock"

# Timeout d'attente du verrou (secondes)
_LOCK_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
Mode = Literal["stream", "record"]

_MODE_TO_MOVIE_OUTPUT: dict[Mode, str] = {
    "stream": "off",
    "record": "on",
}

# ---------------------------------------------------------------------------
# Utilitaires internes
# ---------------------------------------------------------------------------

def _get_conf_path() -> Optional[str]:
    for p in _MOTION_CONF_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

def _read_state() -> Optional[dict]:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return None

def _write_state(mode: Mode, conf_path: str) -> None:
    try:
        _STATE_FILE.write_text(json.dumps({
            "mode": mode,
            "movie_output": _MODE_TO_MOVIE_OUTPUT[mode],
            "conf_path": conf_path,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, indent=2))
    except Exception:
        pass

def _apply_to_conf(conf_path: str, movie_output_value: str) -> bool:
    """Lit motion.conf, remplace (ou ajoute) la directive movie_output,
    et réécrit le fichier.  Doit être appelé sous verrou."""
    try:
        with open(conf_path, "r") as f:
            lines = f.readlines()
    except Exception as e:
        raise RuntimeError(f"Impossible de lire {conf_path} : {e}")

    new_lines = []
    modified = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("movie_output") and not stripped.startswith("#"):
            new_lines.append(f"movie_output {movie_output_value}\n")
            modified = True
        else:
            new_lines.append(line)

    if not modified:
        new_lines.append(f"\nmovie_output {movie_output_value}\n")

    content = "".join(new_lines)

    # Tentative d'écriture directe
    try:
        with open(conf_path, "w") as f:
            f.writelines(new_lines)
        return True
    except PermissionError:
        pass

    # Repli : écriture via sudo tee (motion.conf appartenant à root)
    try:
        result = subprocess.run(
            ["sudo", "tee", conf_path],
            input=content, text=True,
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception as e:
        raise RuntimeError(f"Impossible d'écrire {conf_path} via sudo tee : {e}")

# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def set_mode(mode: Mode, caller: str = "inconnu") -> bool:
    """Définit le mode Motion de façon exclusive et persistante.

    Paramètres
    ----------
    mode   : "stream" (flux HTTP, movie_output off)
             "record" (enregistrement .mkv, movie_output on)
    caller : nom du programme appelant, pour les logs

    Retourne True si tout s'est bien passé.

    Lève ValueError si le mode est inconnu.
    Lève RuntimeError si le verrou ne peut être obtenu ou la conf introuvable."""
    if mode not in _MODE_TO_MOVIE_OUTPUT:
        raise ValueError(f"Mode inconnu : '{mode}'. Valeurs acceptées : {list(_MODE_TO_MOVIE_OUTPUT)}")

    conf_path = _get_conf_path()
    if conf_path is None:
        raise RuntimeError(
            "motion.conf introuvable dans les chemins connus : "
            + ", ".join(_MOTION_CONF_CANDIDATES)
        )

    movie_val = _MODE_TO_MOVIE_OUTPUT[mode]
    lock_fd = None

    try:
        lock_fd = open(_LOCK_FILE, "w")

        # Attente non-bloquante avec timeout
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Impossible d'obtenir le verrou sur motion.conf "
                        f"après {_LOCK_TIMEOUT} s (appelant : {caller})."
                    )
                time.sleep(0.2)

        # --- Section critique ---
        _apply_to_conf(conf_path, movie_val)
        _write_state(mode, conf_path)
        print(
            f"[MotionModeManager] Mode '{mode}' appliqué "
            f"(movie_output={movie_val}) par '{caller}'."
        )
        return True

    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass

def get_mode() -> Optional[Mode]:
    """Retourne le mode courant ("stream" ou "record") depuis le fichier d'état,
    ou None si l'état est inconnu.

    Lecture rapide sans verrou (lecture seule du JSON)."""
    state = _read_state()
    if state:
        return state.get("mode")

    # Repli : lire directement motion.conf
    conf_path = _get_conf_path()
    if conf_path is None:
        return None
    try:
        with open(conf_path) as f:
            for line in f:
                s = line.strip()
                if s.startswith("movie_output") and not s.startswith("#"):
                    parts = s.split()
                    if len(parts) >= 2:
                        val = parts[1].lower()
                        return "record" if val == "on" else "stream"
    except Exception:
        pass
    return None

def get_movie_output() -> bool:
    """Retourne True si movie_output est 'on' (mode record).
    Compatibilité avec l'API existante des deux programmes."""
    return get_mode() == "record"
