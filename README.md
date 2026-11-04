# Bot de Ofertas Automatizado para o Telegram

Pipeline em Python que coleta ofertas de Amazon, Shopee e Mercado Livre,
valida desconto, evita reenvio e publica no Telegram em ciclo agendado.

> Baseado no blueprint "Guia Completo: Bot de Ofertas Automatizado para o
> Telegram". A estrutura de pastas foi mantida (`main.py`, `database.py`,
> `services/`), mas o motor foi reescrito para rodar de forma confiável —
> os problemas do guia original estão listados no final.

## Como funciona

```
3 fontes ──▶ coleta paralela ──▶ filtros de negócio ──▶ anti-duplicidade ──▶ Telegram
   (API)        (httpx async)     (desconto/preço)      (hash+cooldown)      (retry+rate limit)
                                        │
                                        └──▶ banco (SQLite local / Postgres no deploy)
```

- **Coleta paralela**: as três lojas são consultadas ao mesmo tempo; a falha de
  uma não derruba as outras.
- **Filtros**: desconto mínimo, faixa de preço e limite de ofertas por ciclo.
- **Anti-duplicidade**: hash SHA-256 da URL + cooldown de 24h + reenvio
  antecipado se o preço cair mais que 10%.
- **Publicação**: retentativa exponencial em erro de rede/`RetryAfter`, intervalo
  mínimo entre mensagens no mesmo grupo e escape de HTML nos títulos.

## Rodando local

```bash
pip install -r requirements.txt
copy .env.example .env      # Windows
# cp .env.example .env      # Linux/macOS

python main.py --once --dry-run   # não envia, só mostra o que seria publicado
python main.py --once             # executa um ciclo de verdade
python main.py --daemon           # loop contínuo
python scripts/check_sources.py   # diagnostica as credenciais de cada loja
```

Requer Python 3.11+.

## Configuração

Tudo é lido de variáveis de ambiente (`.env` em local, *Secrets* no GitHub).

| Variável | O que faz |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token do `@BotFather` |
| `TELEGRAM_CHAT_ID` | `@grupo`, `-100...` ou id do canal |
| `TELEGRAM_ENABLED` | `false` = dry-run (não envia) |
| `DATABASE_URL` | `sqlite:///./data/offers.db` ou `postgresql://...` |
| `OFFERS_SOURCES` | `amazon,shopee,mercadolivre` — lojas ativas |
| `KEYWORDS_*` | Termos buscados em cada loja |
| `MIN_DISCOUNT_PCT` | Desconto mínimo (padrão 35) |
| `MIN_PRICE` / `MAX_PRICE` | Faixa de preço aceita |
| `MAX_OFFERS_PER_CYCLE` | Máximo de mensagens por ciclo (padrão 5) |
| `RESEND_COOLDOWN_HOURS` | Reenvio após N horas (padrão 24) |
| `PRICE_DROP_RETRIGGER_PCT` | Reenvio antecipado por queda de preço (padrão 10) |
| `POST_DEMO_OFFERS` | `true` publica ofertas de demonstração |

### Credenciais por loja

| Loja | Variáveis | Sem credencial |
| --- | --- | --- |
| Amazon | `AMAZON_ACCESS_KEY`, `AMAZON_SECRET_KEY`, `AMAZON_PARTNER_ID`, `AMAZON_PARTNER_TAG` | modo demo |
| Shopee | `SHOPEE_PARTNER_ID`, `SHOPEE_PARTNER_KEY`, `SHOPEE_SHOP_ID` | modo demo |
| Mercado Livre | `MERCADOLIVRE_ACCESS_TOKEN` (+ `MERCADOLIVRE_AFFILIATE_ID`) | modo demo |

**As três APIs exigem credencial hoje.** A busca do Mercado Livre sem token
responde `403` do PolicyAgent, e Shopee/Amazon só expõem dados via programa de
afiliados. Sem credencial, o bot roda em **modo demo**: publica ofertas
fictícias marcadas com o selo `DEMO - oferta de teste, nao e produto real`.

### Passo a passo do Telegram

1. Fale com `@BotFather` → `/newbot` → copie o token.
2. Crie o grupo, adicione o bot como **administrador** com permissão de
   enviar mensagens.
3. Pegue o id do grupo: envie uma mensagem no grupo e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — o `chat.id` aparece em
   `result[].message.chat.id`. Grupos supergrupos começam com `-100`.

## Deploy: GitHub Actions + Supabase

O deploy é um *cron* do GitHub Actions: a cada 2 horas o runner executa
`python main.py --once` e encerra. Sem servidor, sem custo e sem máquina
ligada.

**Por que Postgres e não SQLite:** cada execução do Actions roda numa máquina
nova. O arquivo SQLite seria recriado do zero a cada ciclo e a
anti-duplicidade nunca encontraria o produto — o bot repostaria as mesmas
ofertas para sempre. O banco precisa ser externo e persistente.

### 1. Banco (Supabase)

No Supabase: *New project* → *Database* → copie a connection string
(*Session pooler*, modo **Session**, porta 5432) e troque `[YOUR-PASSWORD]`
pela senha. O host direto é IPv6-only; a VM/roster IPv4 precisa do pooler.

```
DATABASE_URL=postgresql://postgres.<ref>:<senha>@aws-0-<reg>.pooler.supabase.com:5432/postgres
```

A primeira execução cria as tabelas `offers` e `runs` automaticamente.

### 2. Secrets e variables no repositório

*Settings → Secrets and variables → Actions*:

**Secrets:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` e as
credenciais das lojas (deixe vazio o que ainda não tiver).

**Variables:** `MIN_DISCOUNT_PCT`, `MAX_OFFERS_PER_CYCLE`,
`POST_DEMO_OFFERS` (`false` assim que houver credencial real), etc.

### 3. Subir

```bash
git init && git add . && git commit -m "bot de ofertas"
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

O workflow `.github/workflows/publish.yml` roda em todo `*/2 * * *` e também
pode ser disparado na mão (*Run workflow*), com a opção de dry-run.

## Docker (opcional)

```bash
docker build -t bot-ofertas .
docker run --rm --env-file .env bot-ofertas            # um ciclo
docker run -d --restart unless-stopped --env-file .env bot-ofertas python main.py --daemon
```

Para testar o mesmo backend de produção localmente:

```bash
docker compose up -d db
# .env: DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ofertas
```

## Testes

```bash
python -m pytest -q
```

A suíte cobre escape de HTML, filtros de desconto/preço, as três regras de
anti-duplicidade, o ciclo completo (com bot falso), retentativa de envio e a
verificação das assinaturas AWS SigV4 (Amazon) e HMAC (Shopee) contra valores
conhecidos.

## Problemas comuns

| Sintoma | Causa provável |
| --- | --- |
| `TELEGRAM_BOT_TOKEN nao configurado` | `.env` ausente ou token vazio |
| `bot is not a member of the group` | Bot não foi adicionado como admin |
| `chat not found` | `TELEGRAM_CHAT_ID` errado (use `getUpdates`) |
| `403 forbidden` do Mercado Livre | Token ausente/vencido — a API exige autenticação |
| `RetryAfter` frequente | Grupo com rate limit; aumente `SEND_DELAY_SECONDS` |
| Nenhuma oferta aprovada | Ajuste `MIN_DISCOUNT_PCT`/`MIN_PRICE` ou as keywords |
| Pubblica sempre a mesma coisa | `DATABASE_URL` está pointing para um banco novo a cada ciclo |

## O que mudou em relação ao guia original

O guia funciona como blueprint, mas o código de exemplo tinha problemas que
impediam o bot de rodar de forma confiável. Todos foram corrigidos aqui:

1. **`while True` fora do `asyncio.run()`** — o loop nunca agendava nada e o
   event loop era recriado a cada volta. Agora o daemon usa `async` +
   `asyncio.sleep()`.
2. **Token e grupo hardcoded** — movidos para variáveis de ambiente.
3. **Sem tratamento de erro** — uma requisição quebrada derrubava o ciclo.
   Agora cada loja é isolada e o ciclo registra o erro no banco sem morrer.
4. **Payload de exemplo fingindo ser a API real** — cada loja tem um adapter
   com o contrato e a assinatura reais (SigV4 na Amazon, HMAC na Shopee),
   com fallback explícito para modo demo.
5. **Hash de URL como única proteção** — o mesmo produto nunca era reenviado,
   nem mesmo quando o preço caía. Agora há cooldown e reenvio por queda de
   preço.
6. **Sem rate limit nem proteção contra flood** — o Telegram pode banir o bot
   em envios seguidos. Agora há intervalo mínimo entre mensagens e
   retentativa com backoff.
7. **SQLite em produção** — inviável em CI efêmero; o deploy usa Postgres.
8. **Títulos sem escape** — um título com `&` ou `<` faria o Telegram recusar
   a mensagem inteira.
