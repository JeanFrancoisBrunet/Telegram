#!/usr/bin/env python3
"""
Affiche le JSON brut retourné par moondream — streaming et non-streaming.
Usage : python3 test_moondream_raw.py [image.jpg]
"""
import base64, json, sys, requests
from pathlib import Path

OLLAMA_URL = "http://localhost:11434"
MODEL      = "moondream"
PROMPT     = "What do you see in this image?"

def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()

def make_test_image():
    from PIL import Image, ImageDraw
    import tempfile
    img = Image.new("RGB", (200, 200), (200, 50, 50))
    ImageDraw.Draw(img).rectangle([50,50,150,150], fill=(50,200,50))
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    img.save(tmp.name)
    return tmp.name

img_path = sys.argv[1] if len(sys.argv) > 1 else make_test_image()
print(f"Image : {img_path}  ({Path(img_path).stat().st_size} bytes)")
img_b64 = b64(img_path)
print(f"Base64 : {len(img_b64)} chars\n")

# ── 1. NON-STREAMING ────────────────────────────────────────────────────────
print("="*60)
print("TEST 1 : NON-STREAMING (stream=false)")
print("="*60)
payload = {"model": MODEL, "prompt": PROMPT, "images": [img_b64], "stream": False}
r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300)
print(f"HTTP {r.status_code}")
try:
    data = r.json()
    print("JSON complet :")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n→ data['response']  = {data.get('response')!r}")
    print(f"→ data['content']   = {data.get('content')!r}")
    print(f"→ data['message']   = {data.get('message')!r}")
except Exception as e:
    print(f"Erreur parsing JSON : {e}")
    print(r.text[:500])

# ── 2. STREAMING ────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("TEST 2 : STREAMING (stream=true) — toutes les lignes JSON")
print("="*60)
payload["stream"] = True
r2 = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, stream=True, timeout=300)
print(f"HTTP {r2.status_code}")
lines = []
for raw in r2.iter_lines():
    if not raw:
        continue
    try:
        obj = json.loads(raw.decode())
        lines.append(obj)
        # Afficher chaque ligne brute
        print(f"  LINE {len(lines):02d}: {json.dumps(obj, ensure_ascii=False)}")
    except Exception as e:
        print(f"  LINE ERR: {raw} → {e}")

print(f"\nTotal lignes reçues : {len(lines)}")
all_keys = set()
for l in lines:
    all_keys.update(l.keys())
print(f"Clés JSON rencontrées : {sorted(all_keys)}")

# Chercher la réponse dans toutes les clés possibles
for key in sorted(all_keys):
    vals = [l.get(key,"") for l in lines if l.get(key)]
    if vals:
        combined = "".join(str(v) for v in vals)
        print(f"\n→ Clé '{key}' cumulée ({len(combined)} chars) : {combined[:300]!r}")
