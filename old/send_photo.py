import telegram
import requests
import glob
import os

TOKEN = "8758743361:AAHfnPCcRO1a_6hZI5oWscabT38GiOMTov4"
CHAT_ID = 6325187898

# Trouver la dernière image dans un dossier
folder = "/home/jfbrunet"
extensions = ("*.jpg", "*.png", "*.jpeg", "*.JPG", "*.PNG", "*.JPEG")

files = []
for ext in extensions:
    files.extend(glob.glob(os.path.join(folder, ext)))

if not files:
    print("Aucune image trouvée.")
    exit()

PHOTO_PATH = max(files, key=os.path.getmtime)

url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"

with open(PHOTO_PATH, "rb") as photo:
    files = {"photo": photo}
    data = {"chat_id": CHAT_ID}
    r = requests.post(url, files=files, data=data)

print("Image envoyée :", PHOTO_PATH)
print("Statut :", r.status_code)
print("Réponse :", r.text)
