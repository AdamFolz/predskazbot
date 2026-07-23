# PredskazBot v2.1 – FINAL FIXED

Готовая сборка, которая реально запускается. Сделано после аудита твоего репозитория BOTOVODYVROT-main.

## Что было сломано / почему не работало

1. **Провайдер-зоопарк без единого конфига.**  
   Было: `OPENAI_API_KEY or MOONSHOT_API_KEY`, модель `kimi-k2.7-code`, base_url `https://api.kimi.com/coding/v1`, но в `.env.local.example` стоит `OPENAI_MODEL=gpt-5.5` + `OPENAI_BASE_URL=https://vip.j3gb.com/v1`.  
   Стало: `config.py` авто-определяет: OpenAI, Venice, KIMI/Moonshot, OpenRouter, DeepSeek, Groq, **любой OpenAI-compatible прокси** (vip.j3gb.com, bizdecipher и т.п.).

2. **base_url без /v1**. Многие прокси дают `https://vip.j3gb.com`, а openai-клиент требует `/v1`. Теперь авто-чинится.

3. **Venice не поддерживался.** Добавлен `VENICE_API_KEY`, `venice_parameters: include_venice_system_prompt=false`, модель по умолчанию `qwen3-4b`.

4. **validate_env падал без понятного сообщения.** Теперь пишет какой провайдер обнаружен и подсказывает `python scripts/setup_env.py`.

5. **Нет деплоя 24/7.** Добавлены: Dockerfile, docker-compose, systemd unit, Railway/Render Procfile, пошаговый `deploy/README_DEPLOY.md`.

6. **Нет проверки ключа.** Добавлен `scripts/check_provider.py` – делает 1 ping-запрос и сразу говорит 401/balance/ok.

## Быстрый старт

```bash
git clone <твой форк>
cd predskazbot_final
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux
pip install -r requirements.txt
python scripts/setup_env.py
python scripts/check_provider.py
python bot.py
```

В Telegram:
```
/whoami
/health
/future
```

## .env пресеты

**Твой текущий proxy (vip.j3gb.com):**
```
TELEGRAM_BOT_TOKEN=...
OPENAI_API_KEY=твой_ключ_от_j3gb
OPENAI_BASE_URL=https://vip.j3gb.com/v1
OPENAI_MODEL=gpt-4o-mini
# или gpt-5.5 если именно так называется у провайдера
ADMIN_USER_ID=...
```

**Venice.ai:**
```
VENICE_API_KEY=...
OPENAI_BASE_URL=https://api.venice.ai/api/v1
OPENAI_MODEL=qwen3-4b
```

**KIMI Code:**
```
MOONSHOT_API_KEY=...
OPENAI_BASE_URL=https://api.kimi.com/coding/v1
OPENAI_MODEL=kimi-k2.7-code
```

## Деплой автономно (чтобы не держать ПК включённым)

См. `deploy/README_DEPLOY.md`. Коротко:

- **VPS Hetzner 4€/мес** – systemd service уже готов (`deploy/predskazbot.service`)
- **Docker**: `docker compose up -d`
- **Railway/Render**: Procfile + railway.json включены

## Новые файлы

```
config.py                    # унифицированный LLM-провайдер
scripts/setup_env.py         # интерактивный мастер .env
scripts/check_provider.py    # ping LLM, 401 диагностика
Dockerfile
docker-compose.yml
Procfile
railway.json
deploy/predskazbot.service   # systemd
deploy/README_DEPLOY.md
README_FINAL.md              # этот файл
```

## Команды бота (без изменений)

`/future /profile /lore /summary /ask /kbstatus /kbsearch /kbask /kbimport /health /privacy /export_me /delete_me /forget /whoami /v2status`

`/health` теперь показывает:
```
llm_provider: proxy
llm_model: gpt-4o-mini
llm_base: https://vip.j3gb.com/v1
...
```

## Тесты

```
python -m pytest -q
# 9 passed
python scripts/smoke_test_product.py
# Smoke test passed.
```

---

Сделано: 10 июля 2026.  
Готовый репо: `/home/user/predskazbot_final`  
Если нужно – запакую в zip, запушу в твой GitHub, или помогу задеплоить на VPS/Railway прямо сейчас.
