import requests

# Cola aqui o teu token exato do BotFather
TOKEN = "9168321891:AAGisr2xO-yzaGb6nDNcN-0GRfnM3Johevc"

url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
resposta = requests.get(url).json()

print("--- RESPOSTA DO TELEGRAM ---")
print(resposta)