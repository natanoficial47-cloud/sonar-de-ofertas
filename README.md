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
| `MIN_DISCOUNT_PCT` | Desconto mínimo quando há preço de tabela no anúncio (padrão 35) |
| `MIN_COMPARISON_PCT` | Desconto mínimo quando o anúncio é o mais barato entre os vendedores do mesmo produto (padrão 25) |
| `MIN_PRICE` / `MAX_PRICE` | Faixa de preço aceita (padrão 20 / 3000) |
| `MAX_OFFERS_PER_CYCLE` | Máximo de mensagens por ciclo (padrão 6) |
| `MAX_OFFERS_PER_KEYWORD_PER_CYCLE` | Máximo de ofertas da **mesma categoria** por ciclo (padrão 1 — o grupo não fica monopolizado por um nicho) |
| `MERCADOLIVRE_COMPARISON_MIN_RATIO` | Se o anúncio mais barato custa menos de X% do preço típico, não é desconto e sim outro produto (padrão 0.35) |
| `RESEND_COOLDOWN_HOURS` | Reenvio após N horas (padrão 24) |
| `PRICE_DROP_RETRIGGER_PCT` | Reenvio antecipado por queda de preço (padrão 10) |
| `POST_DEMO_OFFERS` | `true` publica ofertas de demonstração |

### Credenciais por loja

| Loja | Variáveis | Sem credencial |
| --- | --- | --- |
| Amazon | `AMAZON_ACCESS_KEY`, `AMAZON_SECRET_KEY`, `AMAZON_PARTNER_TAG` | modo demo |
| Shopee | `SHOPEE_PARTNER_ID`, `SHOPEE_PARTNER_KEY`, `SHOPEE_SHOP_ID`, `SHOPEE_ACCESS_TOKEN` (+ `SHOPEE_REFRESH_TOKEN`) | modo demo |
| Mercado Livre | `MERCADOLIVRE_CLIENT_ID` + `MERCADOLIVRE_CLIENT_SECRET` (ou `MERCADOLIVRE_ACCESS_TOKEN`) + `MERCADOLIVRE_AFFILIATE_ID` | modo demo |

**As três APIs exigem credencial hoje.** A busca do Mercado Livre sem token
responde `403` do PolicyAgent, e Shopee/Amazon só expõem dados via programa de
afiliados. Sem credencial, o bot roda em **modo demo**: publica ofertas
fictícias marcadas com o selo `DEMO - oferta de teste, nao e produto real`.

### Mercado Livre — qual credencial é qual

No painel (`developers.mercadolivre.com.br` → seu app → *Credenciais*) existem
duas coisas parecidas, e confundi-las derruba tudo:

| Credencial | Formato | Para que serve |
| --- | --- | --- |
| **Client ID** | número grande (ex.: `4458888970304935`) | identificação do app |
| **Client Secret** | 32 caracteres (`AbCdEfGhIjKlMnOpQrStUvWxYz`) | trocar por token |
| **Access Token** | `APP_USR-123456789-053009-cc03aaf33-123456` | autenticar as chamadas |

Com **Client ID + Client Secret** o bot troca as credenciais por um access
token sozinho (`POST /oauth/token`, `grant_type=client_credentials`) e renova
quando expira — o token vive ~6 horas, e o cron roda a cada 4 horas. O token novo
fica no banco, então o `.env` não precisa ser rotacionado a cada ciclo.

Se você colar o **Client Secret** no lugar do access token, a API responde
`403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES` em qualquer endpoint, inclusive
`/users/me`. Esse é o sintoma de credencial trocada.

`MERCADOLIVRE_AFFILIATE_ID` é o seu ID de afiliado: é ele que faz o link
sair com a sua tag (`&tag=`), para a venda virar comissão.

### Mercado Livre — o app só libera dois recursos (e isso muda o bot)

O app criado no painel começa com **dois recursos liberados**: *Busca*
(`/products/search`) e *Itens* (`/products/{id}/items`). Os endpoints clássicos
respondem `403 forbidden`:

| Endpoint | Situação | O que traz |
| --- | --- | --- |
| `/products/search?q=…&site_id=MLB` | ✅ liberado | produtos de catálogo (sem preço) |
| `/products/{id}/items` | ✅ liberado | anúncios do produto **com preço** |
| `/sites/MLB/search` | ❌ 403 | a busca clássica (preço + desconto + permalink) |
| `/items/{id}` | ❌ 403 | detalhe do anúncio (desconto exato) |

Por isso o bot monta a oferta assim:

```
busca (products/search)  ->  produtos de catálogo
                            ->  /products/{id}/items  ->  anúncios com preço
```

**Duas fontes de desconto, ambas reais:**

1. **Preço de tabela do anúncio** — o `original_price` é o valor riscado na
   página **daquele anúncio**. Como o link leva o `item_id` do anúncio
   escolhido, a página abre mostrando exatamente o preço e o desconto
   anunciados (`De: R$ 300,00` / `Por: R$ 144,00` / `52% OFF`). Quando outro
   vendedor do mesmo produto está cobrando perto do mesmo preço de tabela, a
   mensagem acrescenta *(mesmo preço em outros vendedores do produto)*.
2. **Menor preço do produto** — quando o anúncio **não tem preço de tabela**,
   o bot compara os anúncios do **mesmo produto de catálogo** (o mesmo fone, o
   mesmo modelo, em vários vendedores), publica **só o mais barato** (é o que a
   página mostra como "a partir de R$ X") e compara com o **preço típico**
   (mediana dos vendedores, não o mais caro, que inflaria o número):
   *Menor preço entre 20 ofertas do produto (preço típico R$ 59,45) — economia de
   51% frente ao preço típico*.

O filtro usa `MIN_DISCOUNT_PCT` para o caso 1 e `MIN_COMPARISON_PCT` para o
caso 2 — **e a mensagem nunca mostra "% OFF" quando o anúncio não tem preço de
tabela**, porque esse número seria inventado.

**Publicação com foto:** a oferta vai para o grupo como **imagem do produto com a
legenda** (`send_photo` com a foto do catálogo em `http2.mlstatic.com`). Se a
imagem falhar, o texto sozinho é enviado — a oferta nunca se perde.

#### Onde pedir a aprovação dos recursos no painel

Estado real da API com o app atual (verificado em 2026-10-31):

| Recurso | Endpoint | Situação |
| --- | --- | --- |
| Catálogo — busca | `/products/search` | ✅ 200 |
| Catálogo — anúncios | `/products/{id}/items` | ✅ 200 |
| Catálogo — detalhe | `/products/{id}` | ✅ 200 |
| Envio do anúncio | `/items/{id}/shipping` | ✅ 200 |
| Descrição do anúncio | `/items/{id}/description` | ✅ 200 |
| **Itens (clássico)** | `/items/{id}` | ❌ 403 `access_denied` |
| **Busca de itens (clássica)** | `/sites/MLB/search` | ❌ 403 `forbidden` |

Ou seja: **o bot já roda 100% com o que está liberado**. A aprovação que falta é
só do tópico **Itens (Items)**, que destrava `/items/{id}` e a busca clássica —
o que traria `permalink` oficial e `discount_percentage` do anúncio. É um
upgrade, não um bloqueio.

**Caminho no painel** (`developers.mercadolivre.com.br`):

1. Entre com a conta do Mercado Livre e abra o **DevCenter**
   (`https://developers.mercadolivre.com.br/devcenter`).
2. Em **Minhas aplicações**, clique nos **três pontinhos** da sua aplicação →
   **Editar** (ou **Configurações**).
3. Na página da aplicação, vá em **Configurações** → seção de **Permissões /
   Escopos**. É a mesma tela dos *Tópicos* citados na doc oficial
   (`Orders`, `Messages`, `Items`, `Catalog`, `Shipments`, `Promotions`).
4. Marque **Itens / Items** (e **Catalog**, se ainda não estiver marcado).
5. Se o tópico aparecer **desabilitado com um botão de "Solicitar"**, o pedido
   é feito ali mesmo — ele passa por análise do time do ML e o painel avisa que
   pode levar **até 3 dias úteis**. Depois de aprovado, a permissão passa a
   valer no token.

Até lá, nada trava: o bot usa só a API de catálogo, que já devolve preço real,
frete real e `original_price` do anúncio.

### Preço, frete e a mistura de categorias

O erro clássico deste tipo de bot é anunciar "R$ 29,99" quando o anúncio sai
por R$ 76,83 com o frete. As regras que evitam isso:

1. **Escolha pelo preço final.** Entre os anúncios do mesmo produto, o bot
   prefere o que tem **frete grátis**; quando nenhum tem, vence o **menor
   preço total (produto + frete)**. O preço anunciado é sempre o preço do
   `item_id` que vai no link.
2. **O frete aparece na mensagem.** `🚚 Frete: grátis` ou `🚚 Frete: R$ X`.
   Nunca some do texto — o que o grupo lê é o que o comprador paga.
3. **Comparação honesta.** O "preço típico" é a **mediana** dos anúncios da
   mesma base (nunca o mais caro, que inflaria o número). E se o anúncio mais
   barato custa menos de 35% da mediana, isso não é desconto: é outro produto
   (condição diferente, importado). Nesse caso o desconto não entra e a oferta
   não é aprovada — evita o "93% OFF" que não existe na página.
4. **Variedade por categoria.** `MAX_OFFERS_PER_KEYWORD_PER_CYCLE=1` corta as
   repetidas da mesma categoria **depois** de ordenar por desconto. Sem isso o
   grupo inteiro do dia sairia do nicho com o maior desconto.
5. **O rótulo é o termo de busca** (`Busca: air fryer`), não a categoria
   oficial: a API de catálogo não devolve o nome da categoria, e inventar um
   rótulo seria mentir.

**Nichos buscados** (22 termos, de 7 segmentos): tech (fone bluetooth,
smartwatch, cadeira gamer, notebook, monitor gamer, mouse gamer rgb, teclado
gamer), casa e cozinha (air fryer, garrafa térmica, panela de pressão, jogo de
panelas, tapete de sala, quadro decorativo), fitness (tapete de yoga,
suplemento), moda (tênis de corrida, mochila, óculos de sol), beleza (secador
de cabelo, perfume) e pet (comedouro para gato, brinquedo para cachorro).

> Termos genéricos funcionam melhor que termos super específicos: `organizador`
> trazia **livros** ("Livro ... (Organizador)") e `cookset` devolvia só **8
> resultados** (cookset de camping). Cada termo novo merece uma sondagem
> prévia, olhando os nomes que voltam.

Para liberar a busca clássica (que traz o desconto de tabela de quase todos os
anúncios e o `permalink` oficial):

1. `developers.mercadolivre.com.br` → seu app → **Solicitar credenciais**.
2. Marque os recursos **Busca** e **Itens** e submeta.
3. A aprovação aparece em *Credenciais*; a partir daí `/sites/MLB/search`
   responde 200 e o bot passa a achar muito mais ofertas com desconto real.

> O link publicado é `https://www.mercadolivre.com.br/p/MLB<id>?item_id=<id>&tag=<afiliado>`.
> Esse formato resolve no site; o formato com *slug* (`/nome-do-produto-p/MLB<id>`)
> devolve 404, porque o slug oficial tem atributos que a API não expõe.

**Ciclo rotativo:** a API só expõe 200 resultados por keyword e o Mercado Livre
tem rate limit. O bot intercala as keywords, respeita um orçamento de sondagem
(`MERCADOLIVRE_MAX_PROBES`) e **rotaciona a janela de produtos a cada ciclo**
— senão o cron de 4 em 4 horas repetiria as mesmas ofertas e a trava de 24h
impediria qualquer nova postagem. O contador de ciclo é guardado no banco
(`kv.cycle_counter`), porque no cron cada execução é um processo novo.

### Amazon (PA-API v5) — o que a tracking ID não resolve

`natannunes-20` é o **tracking ID** (aparelho de rastreamento): é o que entra
na URL do link de afiliado (`.../dp/ASIN?tag=natannunes-20`). Para *buscar*
produtos e preço pela API, o painel de Associados emite um par de chaves AWS:

1. `associates.amazon.com.br` → **Ferramentas** → **API da Amazon**.
2. **Criar credenciais** e baixe o CSV com `aws_access_key_id` e
   `aws_secret_access_key` — preencha `AMAZON_ACCESS_KEY` e `AMAZON_SECRET_KEY`.
3. `AMAZON_PARTNER_TAG` = sua tracking ID (`natannunes-20`).

Duas regras que a Amazon não avisa:

- **As chaves expiram em 24h.** Se passarem 24h sem uso, a PA-API responde
  `InvalidAccessKey` e você precisa gerar chaves novas. Gere e use no mesmo dia.
- **A conta precisa de 3 vendas nos últimos 180 dias** para habilitar a API.
  Sem isso, o painel não libera a criação de chaves.

### Shopee (Open Platform) — assinatura e token

A assinatura oficial v2 é `hex(HMAC-SHA256(partner_id + path + timestamp +
access_token + shop_id, partner_key))`, com o `sign` indo como **query param**.
Os parâmetros de filtro (`keyword`, `page`…) **não** entram na assinatura.

O endpoint de produto é *shop-level*: exige `shop_id` e `access_token`, que
expira em ~4 horas. Por isso o bot renova o token sozinho (via
`SHOPEE_REFRESH_TOKEN`) e guarda o novo no banco — assim o cron de 4 em 4 horas
não quebra no meio do caminho.

### Passo a passo do Telegram

1. Fale com `@BotFather` → `/newbot` → copie o token.
2. Crie o grupo, adicione o bot como **administrador** com permissão de
   enviar mensagens.
3. Pegue o id do grupo: envie uma mensagem no grupo e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — o `chat.id` aparece em
   `result[].message.chat.id`. Grupos supergrupos começam com `-100`.

## Deploy: GitHub Actions + Supabase

O deploy é um *cron* do GitHub Actions: a cada 4 horas o runner executa
`python main.py --once` e encerra. Sem servidor, sem custo e sem máquina
ligada.

### Onde fazer o deploy (resposta curta)

**Em um repositório do GitHub + um projeto no Supabase.** Não existe "deploy"
noutro lugar: o código roda dentro do runner do GitHub Actions e o banco fica
no Supabase. São dois objetos, criados por você, e nada mais.

| Onde | O quê | Conta |
| --- | --- | --- |
| **github.com/<seu-user>/bot-ofertas-telegram** | código + workflow | repo **privado** (recomendado) |
| **supabase.com → New project** | Postgres persistente | free tier |
| Settings → Secrets and variables → Actions | credenciais | secrets do repo |

**Privado ou público?** O meu palpite é **privado**: o código expõe a
estratégia de monetização (IDs de afiliado, filtros de desconto, keywords) e
você não precisa de issue tracking público. O custo do privado é o limite de
minutos:

| Cron | Jobs/dia | Minutos/mês (~8 min/job) | Cabe no Free privado (2.000)? |
| --- | --- | --- | --- |
| `0 */2 * * *` | 12 | ~2.880 | ❌ estoura no meio do mês |
| `0 */3 * * *` | 8 | ~1.920 | ⚠️ apertado, sem folga |
| **`0 */4 * * *`** | **6** | **~1.440** | ✅ **recomendado** |
| `0 */6 * * *` | 4 | ~960 | ✅ folgado, 4 ofertas/dia |

> Em repo **público** os minutos do GitHub são **grátis**, e aí `*/2` (12
> ofertas/dia) funciona sem custo. O trade-off é o código visível e a regra dos
> **60 dias sem atividade**, que desliga o cron agendado em repo público. Se
> escolher público, é só trocar a linha `cron:` no
> `.github/workflows/publish.yml`.
>
> O cron no arquivo já vem em `0 */4 * * *` — é o padrão seguro para repo
> privado e não estoura o limite em nenhum mês.

**Por que Postgres e não SQLite:** cada execução do Actions roda numa máquina
nova. O arquivo SQLite seria recriado do zero a cada ciclo e a
anti-duplicidade nunca encontraria o produto — o bot repostaria as mesmas
ofertas para sempre. O banco precisa ser externo e persistente.

### 1. Banco (Supabase)

No Supabase: *New project* → *Database* → copie a connection string
(*Session pooler*, modo **Session**, porta 5432) e troque `[YOUR-PASSWORD]`
pela senha. O host direto é IPv6-only; a VM/roster IPv4 precisa do pooler.

```
DATABASE_URL=postgresql://postgres.<ref>:<senha>@aws-0-<reg>.pooler.supabase.com:5432/postgres?sslmode=require
```

> **Use a string exatamente como o Supabase entrega**, inclusive o
> `?sslmode=require` no final — o Supabase exige conexão cifrada, e sem esse
> parâmetro o `psycopg` pode recusar a conexão. (O Postgres local do
> `docker-compose` não usa SSL: para ele, a URL fica sem `sslmode`.)

A primeira execução cria as tabelas `offers` e `runs` automaticamente.

### 2. Secrets e variables no repositório

*Settings → Secrets and variables → Actions*:

**Secrets:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` e as
credenciais das lojas (deixe vazio o que ainda não tiver).

**Variables:** `MIN_DISCOUNT_PCT`, `MAX_OFFERS_PER_CYCLE`,
`POST_DEMO_OFFERS` (`false` assim que houver credencial real), etc.

### 3. Subir

O repositório local já está inicializado e com o primeiro commit pronto. Só
falta apontar para o repo que você criou no GitHub:

```bash
git remote add origin git@github.com:<user>/bot-ofertas-telegram.git
git push -u origin main
```

O workflow `.github/workflows/publish.yml` roda a cada 4 horas e também
pode ser disparado na mão (*Run workflow*), com a opção de dry-run.

### Checklist de deploy (na ordem)

1. **Banco criado no Supabase** e `DATABASE_URL` no secret — com `?sslmode=require`.
2. **Preflight local**, antes de mexer no GitHub:
   ```bash
   python scripts/check_sources.py --ping
   ```
   Cada loja configurada responde em **1 request**. Se o ML vier `OK`, a
   credencial vale; se vier `FALHOU`, o job nem começa.
3. **Secrets no repositório** (Settings → Secrets and variables → Actions):

   | Secret | Origem | Nome legado aceito |
   | --- | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | `@BotFather` (token **novo**) | `TELEGRAM_TOKEN` |
   | `TELEGRAM_CHAT_ID` | id do grupo (`-100...`) | `TELEGRAM_GROUP_ID` |
   | `DATABASE_URL` | Supabase, com `?sslmode=require` | — |
   | `MERCADOLIVRE_CLIENT_ID` | painel do ML | `ML_CLIENT_ID` |
   | `MERCADOLIVRE_CLIENT_SECRET` | painel do ML | `ML_CLIENT_SECRET` |
   | `MERCADOLIVRE_AFFILIATE_ID` | seu ID de afiliado | — |
   | `AMAZON_*`, `SHOPEE_*` | quando tiver as credenciais | — |

   > Os nomes legados funcionam porque o workflow usa
   > `secrets.TELEGRAM_BOT_TOKEN || secrets.TELEGRAM_TOKEN` — o primeiro que
   > existir ganha. **Não precisa renomear secret nenhum.**
   >
   > `DATABASE_URL` é **obrigatório**: sem ele (ou com SQLite) o job falha na
   > hora, com a mensagem "DATABASE_URL em CI precisa ser Postgres". Isso é
   > proposital — com SQLite o runner recria o banco a cada ciclo e o bot
   > repetiria as mesmas ofertas no grupo para sempre.
   >
   > O `MERCADOLIVRE_CLIENT_SECRET` tem que ser o valor **exato** do painel do
   > ML. Um caractere trocado (ex.: `I` no lugar de `l`) devolve *400 Bad
   > Request* no `oauth/token` e o preflight acusa a falha antes do job.

   *Variables* (opcionais, com default no workflow): `MIN_DISCOUNT_PCT`,
   `MIN_COMPARISON_PCT`, `MAX_OFFERS_PER_CYCLE`,
   `MAX_OFFERS_PER_KEYWORD_PER_CYCLE`, `KEYWORDS_MERCADOLIVRE`,
   `MERCADOLIVRE_MAX_PROBES`, `POST_DEMO_OFFERS`, `LOG_LEVEL`.
4. **Primeiro disparo manual**: *Actions → Publicar ofertas no Telegram →
   Run workflow*, com `dry_run = true`. O log mostra o que seria publicado
   (sem enviar). Depois rode com `dry_run = false`.
5. **Acompanhe**: a aba *Actions* mostra cada ciclo; a tabela `runs` guarda
   histórico, contagem de ofertas encontradas/enviadas e o erro de cada ciclo.

### Cuidados com o cron do GitHub

- O cron do GitHub **não é exato**: o job costuma começar com 5–20 min de
  atraso, e o horário de pico do dia é o mais concorrido. Para o grupo isso é
  irrelevante; para um horário cravado, use um VPS.
- **Repositório sem atividade por 60 dias desativa os cron agendados.**
  Se o projeto ficar parado, o bot para de rodar sozinho — de olho no *badge*
  do workflow. (Regra vale para repo público; em repo privado o schedule é
  desativado só por inatividade de 60 dias no repositório inteiro.)
- O grupo `concurrency` impede duas execuções simultâneas (uma cancela a
  outra), então o bot nunca publica em duplicidade.
- `timeout-minutes: 20` dá folga para a varredura do catálogo (o ciclo real
  medido levou 5m34s com 95 ofertas brutas e 400 sondagens).

### Daemon ou cron? (por que não precisa de APScheduler)

São dois modos, e eles não se misturam:

- **`--once` + cron do GitHub** (deploy): cada execução é um processo novo que
  faz um ciclo e sai. Sem estado em memória, sem loop, sem CPU parada. É o
  modo de produção.
- **`--daemon`** (VPS/Docker): loop contínuo que espera `INTERVAL_SECONDS`
  entre os ciclos com `asyncio.wait_for(stop.wait(), timeout=...)` — ou seja,
  **espera por evento, não `time.sleep` nem busy loop**: o processo fica
  parado (0% de CPU) entre os ciclos e acorda no instante do timer. Também
  trata `SIGINT`/`SIGTERM` para encerrar sem cortar o ciclo no meio.

APScheduler não traz nada aqui: o que seria agendado é um ciclo único que
roda de 4 em 4 horas. O agendamento já é do cron, e a espera é feita por
evento.

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
anti-duplicidade, a escolha do anúncio pelo preço final (produto + frete), o
teto de variedade por categoria, a trava de comparação absurda, o ciclo completo
(com bot falso), retentativa de envio e a verificação das assinaturas AWS SigV4
(Amazon) e HMAC (Shopee) contra valores conhecidos.

## Resiliência: uma loja quebrada não derruba o bot

A falha de rede mais comum em marketplace é **rate limit (429)** e **bloqueio
de IP**. O bot tem quatro camadas, da mais externa para a mais interna:

| Camada | Comportamento |
| --- | --- |
| **Coleta** | `asyncio.gather(..., return_exceptions=True)`: as três lojas são consultadas em paralelo e a que falha é isolada — as outras entregam normal |
| **Loja** | `StoreAdapter.collect` envolve a chamada em `try/except`: exceção vira lista vazia, **nunca** oferta fictícia |
| **Requisição** | No ML, `_get_with_retry` refaz a chamada em 429 e em erro de rede, com espera crescente; token vencido (401) dispara a troca por um novo |
| **Ciclo** | `run_cycle` inteiro em `try/except`: o erro é gravado na tabela `runs` e o processo segue vivo (no daemon, o próximo ciclo roda) |

Além disso, na **Amazon** cada keyword é buscada em separado: uma keyword que
derruba (503 do provedor) é registrada e as outras continuam — o erro fica no
log, a coleta da loja não.

E o que **nunca** acontece: uma loja configurada que dá erro devolve lista
vazia, não mock. Publicar produto fictício num grupo real é pior do que não
publicar — e como `POST_DEMO_OFFERS` agora tem padrão **false**, nem uma
oferta de demonstração escapa sem você pedir.



| Sintoma | Causa provável |
| --- | --- |
| `TELEGRAM_BOT_TOKEN nao configurado` | `.env` ausente ou token vazio |
| `bot is not a member of the group` | Bot não foi adicionado como admin |
| `chat not found` | `TELEGRAM_CHAT_ID` errado (use `getUpdates`) |
| `403 forbidden` do Mercado Livre | Recurso não aprovado no app (veja a seção do ML) ou token vencido |
| `429` do Mercado Livre | Rate limit — aumente `MERCADOLIVRE_REQUEST_DELAY` / reduza `MERCADOLIVRE_MAX_PROBES` |
| `RetryAfter` frequente | Grupo com rate limit; aumente `SEND_DELAY_SECONDS` |
| Nenhuma oferta aprovada | Ajuste `MIN_DISCOUNT_PCT`/`MIN_COMPARISON_PCT`/`MIN_PRICE` ou as keywords |
| O grupo inteiro sai do mesmo nicho | `MAX_OFFERS_PER_KEYWORD_PER_CYCLE` está alto demais (use 1) |
| Publica sempre a mesma coisa | `DATABASE_URL` está pointing para um banco novo a cada ciclo (o contador de ciclo e a trava de 24h vivem no banco) |
| A oferta foi sem foto | A imagem do catálogo não baixou — o texto é enviado do mesmo jeito; o aviso fica no log (`Nao consegui enviar a foto`) |
| **A mesma oferta aparece 2x no grupo** | Causa raiz já corrigida: o `python-telegram-bot` dava **timeout de 5s** ao baixar a foto do CDN e o retry reenviava. Agora os timeouts são longos (leitura 120s) e um timeout vira `DeliveryUncertain` → **não reenvia** e marca a oferta como enviada |

### Duplicata no grupo: o que foi corrigido

Este era o único jeito de a mesma oferta aparecer duas vezes — e **não** era o
pipeline: o banco local confirma zero duplicatas por título, produto e URL.

A causa era o `python-telegram-bot`, que usa **timeout de leitura de 5s** por
padrão. Baixar a foto do produto no CDN do Mercado Livre leva mais que isso, o
cliente dava timeout **depois** de o Telegram já ter entregado a mensagem, e o
retry reenviava. O que mudou em `telegram_client.py`:

| Situação | Antes | Agora |
| --- | --- | --- |
| Timeout do PTB | caía no `except Exception` → retry → **oferta duplicada** | `TimedOut`/`httpx.TimeoutException` → `DeliveryUncertain`, **sem retry** |
| Timeout na foto | depois mandava o texto de fallback → **duplicata** | não manda texto; marca a oferta como enviada |
| `BadRequest` (HTML inválido, chat inválido) | retry 4x à toa | erro definitivo, sem retry |
| `RetryAfter` / `NetworkError` | retry com backoff | igual (esses são reenvio seguro) |
| Timeout de leitura do Telegram | 5s (padrão do PTB) | **120s** (connect 30s / write 120s) |

O princípio: **na dúvida, não reenvia**. Perder uma oferta é ruim; duplicar no
grupo é pior — porque o usuário vê a mesma oferta duas vezes e desconfia do bot.

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
