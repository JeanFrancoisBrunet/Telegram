#!/usr/bin/env python3
"""
Test diagnostic Ollama / Moondream sur Raspberry Pi 5
Usage :  python3 test_moondream.py [chemin/vers/image.jpg]
         python3 test_moondream.py          (génère une image test 100x100)
"""

import base64
import json
import sys
import time
from pathlib import Path

import requests

OLLAMA_URL = "http://localhost:11434"
MODELS_TO_TEST = ["moondream", "llama3.2-vision:11b"]
TIMEOUT = 300  # 5 min max

# =============================================================================
def separator(title=""):
    print("\n" + "─" * 50)
    if title:
        print(f"  {title}")
        print("─" * 50)

# =============================================================================
def test_ollama_alive():
    separator("1. Ollama joignable ?")
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        print(f"✅ Ollama répond — {len(models)} modèle(s) installé(s) :")
        for m in models:
            print(f"   • {m}")
        return models
    except Exception as e:
        print(f"❌ Ollama injoignable : {e}")
        print("   → Lancez :  ollama serve")
        sys.exit(1)

# =============================================================================
def test_model_present(installed: list, model: str) -> bool:
    separator(f"2. Modèle '{model}' disponible ?")
    base = model.split(":")[0]
    found = any(base in m for m in installed)
    if found:
        print(f"✅ '{model}' trouvé")
    else:
        print(f"⚠️  '{model}' non trouvé")
        print(f"   → Installez :  ollama pull {model}")
    return found

# =============================================================================
def make_test_image() -> str:
    """Crée une image PNG 100x100 rouge/verte sans dépendance externe."""
    separator("3. Image de test")
    # PNG minimal 1x1 pixel blanc (base64 hardcodé)
    # On génère un PNG 10x10 rouge via struct si Pillow absent
    try:
        from PIL import Image, ImageDraw
        import tempfile, os
        img = Image.new("RGB", (200, 200), color=(200, 50, 50))
        d = ImageDraw.Draw(img)
        d.rectangle([50, 50, 150, 150], fill=(50, 200, 50))
        d.text((60, 90), "TEST", fill=(255, 255, 255))
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        img.save(tmp.name, "JPEG")
        print(f"✅ Image test générée (Pillow) : {tmp.name}")
        return tmp.name
    except ImportError:
        # Fallback : PNG 1×1 blanc encodé en base64 → on écrit le fichier
        import struct, zlib, tempfile
        def png1x1():
            sig = b'\x89PNG\r\n\x1a\n'
            def chunk(t, d):
                c = struct.pack('>I', len(d)) + t + d
                return c + struct.pack('>I', zlib.crc32(c[4:]) & 0xffffffff)
            ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            idat = chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00'))
            iend = chunk(b'IEND', b'')
            return sig + ihdr + idat + iend
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(png1x1())
        tmp.close()
        print(f"✅ Image test minimale générée : {tmp.name}")
        return tmp.name

# =============================================================================
def encode_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

# =============================================================================
def test_vision_generate(model: str, image_b64: str, prompt: str = "What do you see?"):
    """Test via /api/generate (mode non-streaming pour diagnostic simple)."""
    separator(f"4. Test /api/generate — modèle '{model}' (non-streaming)")
    payload = {
        "model":  model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"num_predict": 100},
    }
    print(f"   Prompt : {prompt!r}")
    print("   Envoi requête… (patience)")
    t0 = time.time()
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=TIMEOUT,
        )
        elapsed = time.time() - t0
        print(f"   HTTP {r.status_code} — {elapsed:.1f}s")
        if r.status_code != 200:
            print(f"❌ Erreur HTTP : {r.text[:300]}")
            return False
        data = r.json()
        response = data.get("response", "").strip()
        if response:
            print(f"✅ Réponse reçue ({len(response)} chars) :")
            print(f"   {response[:300]}")
            return True
        else:
            print(f"⚠️  Réponse vide. Contenu brut :")
            print(f"   {json.dumps(data, indent=2)[:500]}")
            return False
    except requests.exceptions.Timeout:
        print(f"❌ Timeout après {TIMEOUT}s")
        return False
    except Exception as e:
        print(f"❌ Exception : {e}")
        return False

# =============================================================================
def test_vision_stream(model: str, image_b64: str, prompt: str = "Describe briefly."):
    """Test via /api/generate en streaming (comme le bot)."""
    separator(f"5. Test /api/generate — modèle '{model}' (STREAMING)")
    payload = {
        "model":  model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": True,
        "options": {"num_predict": 100},
    }
    print(f"   Prompt : {prompt!r}")
    print("   Streaming en cours… (tokens reçus en direct)")
    t0 = time.time()
    full = ""
    token_count = 0
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            stream=True,
            timeout=TIMEOUT,
        ) as r:
            r.raise_for_status()
            for raw in r.iter_lines():
                if not raw:
                    continue
                try:
                    data = json.loads(raw.decode())
                    delta = data.get("response", "")
                    if delta:
                        print(delta, end="", flush=True)
                        full += delta
                        token_count += 1
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
        elapsed = time.time() - t0
        print(f"\n\n✅ Streaming OK — {token_count} tokens en {elapsed:.1f}s")
        print(f"   Texte complet ({len(full)} chars) : {full[:200]!r}")
        return True
    except requests.exceptions.Timeout:
        print(f"\n❌ Timeout après {TIMEOUT}s")
        return False
    except Exception as e:
        print(f"\n❌ Exception : {e}")
        return False

# =============================================================================
def test_chat_api(model: str):
    """Test /api/chat (mode texte pur — sans image)."""
    separator(f"6. Test /api/chat texte — modèle '{model}'")
    payload = {
        "model":   model,
        "messages": [{"role": "user", "content": "Réponds juste 'OK' en un mot."}],
        "stream":  False,
        "options": {"num_predict": 5},
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        msg = r.json().get("message", {}).get("content", "").strip()
        if msg:
            print(f"✅ /api/chat OK : {msg!r}")
            return True
        else:
            print(f"⚠️  Réponse vide : {r.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Exception : {e}")
        return False

# =============================================================================
def main():
    print("=" * 50)
    print("  DIAGNOSTIC OLLAMA VISION — Raspberry Pi 5")
    print("=" * 50)

    # Image : argument CLI ou générée automatiquement
    if len(sys.argv) > 1:
        img_path = sys.argv[1]
        if not Path(img_path).exists():
            print(f"❌ Image introuvable : {img_path}")
            sys.exit(1)
        separator("3. Image fournie")
        print(f"✅ {img_path}")
        cleanup = False
    else:
        img_path = make_test_image()
        cleanup  = True

    image_b64 = encode_b64(img_path)
    print(f"   Taille base64 : {len(image_b64):,} chars")

    # Tests
    installed = test_ollama_alive()

    # Tester chaque modèle disponible
    for model in MODELS_TO_TEST:
        if not test_model_present(installed, model):
            continue
        ok_gen    = test_vision_generate(model, image_b64)
        ok_stream = test_vision_stream(model, image_b64)   if ok_gen else False
        ok_chat   = test_chat_api(model)

        separator(f"RÉSUMÉ — {model}")
        print(f"  /api/generate non-stream : {'✅' if ok_gen    else '❌'}")
        print(f"  /api/generate stream     : {'✅' if ok_stream else '❌'}")
        print(f"  /api/chat texte          : {'✅' if ok_chat   else '❌'}")

    # Nettoyage image temporaire
    if cleanup and Path(img_path).exists():
        Path(img_path).unlink()

    separator("FIN DU DIAGNOSTIC")
    print("Copiez-collez le résultat complet pour analyse.\n")

if __name__ == "__main__":
    main()
