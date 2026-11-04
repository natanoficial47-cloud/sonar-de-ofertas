"""Ajuda a configurar o Telegram: quem e o bot, onde esta o grupo, envio de teste.

Uso:
    python scripts/telegram_setup.py --discover
    python scripts/telegram_setup.py --send "mensagem de teste"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # pragma: no cover
    pass

import os  # noqa: E402

API = "https://api.telegram.org"


def _token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN vazio. Cole o token em .env")
        sys.exit(2)
    return token


async def whoami(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get(f"{API}/bot{token}/getMe")
    data = response.json()
    if not data.get("ok"):
        print(f"Token invalido: {data.get('description')}")
        sys.exit(1)
    bot = data["result"]
    print(f"Bot: @{bot['username']} ({bot.get('first_name')})")
    print(f"ID: {bot['id']}")


async def discover(client: httpx.AsyncClient, token: str) -> None:
    """Lista os chats onde o bot tem mensagens pendentes."""
    webhook = await client.get(f"{API}/bot{token}/getWebhookInfo")
    info = webhook.json().get("result", {}) if webhook.json().get("ok") else {}

    response = await client.get(
        f"{API}/bot{token}/getUpdates",
        params={"offset": -1, "limit": 50},
    )
    data = response.json()
    if not data.get("ok"):
        print(f"Falha no getUpdates: {data.get('description')}")
        sys.exit(1)

    updates = data.get("result", [])
    pending = info.get("pending_update_count", len(updates))
    if info.get("url"):
        print(f"Atencao: o bot tem webhook ativo em {info['url']}")
        print("Com webhook configurado o getUpdates nao retorna as mensagens.")
        print("Remova com: deleteWebhook\n")

    if not updates:
        print(f"Nenhuma mensagem pendente (pending_update_count={pending}).")
        print("Faca assim:")
        print("  1. Adicione o bot ao grupo (como administrador)")
        print("  2. Envie qualquer mensagem no grupo DEPOIS de adicionar")
        print("  3. Rode este script de novo")
        print()
        print("Atalho: mande uma mensagem direta para o bot no privado.")
        print("O id do chat privado aparece aqui e da para testar o envio na hora.")
        return

    chats: dict[tuple, dict] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if not chat.get("id"):
            continue
        key = (chat.get("id"), chat.get("type"))
        chats[key] = chat

    print(f"Chats encontrados ({len(chats)}):\n")
    for (chat_id, chat_type), chat in chats.items():
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "sem titulo"
        print(f"  id={chat_id}  tipo={chat_type}  nome={title}")
    print("\nCopie o id do grupo (comeca com -100) para TELEGRAM_CHAT_ID no .env")


async def send_test(client: httpx.AsyncClient, token: str, text: str) -> None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        print("TELEGRAM_CHAT_ID vazio. Rode primeiro com --discover")
        sys.exit(2)
    response = await client.post(
        f"{API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )
    data = response.json()
    if data.get("ok"):
        print(f"Mensagem enviada para {chat_id} (id {data['result']['message_id']})")
    else:
        print(f"Falha: {data.get('description')}")
        sys.exit(1)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Utilitario de setup do Telegram")
    parser.add_argument("--discover", action="store_true", help="lista os chats do bot")
    parser.add_argument("--send", metavar="TEXTO", help="envia uma mensagem de teste")
    args = parser.parse_args()

    token = _token()
    async with httpx.AsyncClient(timeout=20.0) as client:
        await whoami(client, token)
        if args.discover or not args.send:
            print()
            await discover(client, token)
        if args.send:
            print()
            await send_test(client, token, args.send)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
