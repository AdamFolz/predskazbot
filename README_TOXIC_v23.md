# PredskazBot v2.3-toxic
UNRESTRICTED / private conf build – 11 July 2026

Что внутри:
- prompts.py = UNRESTRICTED (prompts_v3_unrestricted.py)
  toxicity_style: ВСЁ разрешено – мат, чёрный юмор, болезни, смерть, нация, религия, доксинг-стайл
  future: 1-2 предложения, 120-240 симв, панч обязателен, можно выдумывать будущее
  memory curator: сохраняет всё как есть, без safety-фильтров
- speed:
  /future temperature 0.75, max_tokens 220
  /summary temperature 0.5, max_tokens 380
  MAX_RECENT_MESSAGES по умолчанию 80 – рекомендуется в Amvera поставить 30
  REGENERATION_ATTEMPTS=1
- provider auto-detect: OpenAI / Venice / KIMI / Moonshot / OpenRouter / DeepSeek / Groq / vip.j3gb.com proxy
- /health показывает llm_provider / llm_model / llm_base
- amvera.yml, Dockerfile, docker-compose.yml, Procfile, railway.json – готовы
- scripts/filter_chat_export.py – экспорт чата с фильтром по дням

Быстрый деплой Amvera Warsaw:
1. Залей predskazbot_v2.3-toxic.zip через “Загрузить архив”
2. Переменные окружения:
TELEGRAM_BOT_TOKEN=...
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://vip.j3gb.com/v1
OPENAI_MODEL=gpt-5.5
ADMIN_USER_ID=...
DATABASE_PATH=/data/predskazbot.sqlite3
V2_SQLITE_PATH=/data/predskazbot_v2.sqlite3
MAX_RECENT_MESSAGES=30
MAX_RECENT_BOT_RESPONSES=20
REGENERATION_ATTEMPTS=1
FUTURE_COOLDOWN_SECONDS=12
3. Старт. /health → llm_provider: proxy

ВНИМАНИЕ – приватная конфа:
- Включён полный токсик-режим без цензуры.
- Telegram ToS / EU DSA – на вашей ответственности.
- Не публикуйте токены в логах / чатах – предыдущий токен 8948095738:AAGV… уже утёк, обязательно Revoke в @BotFather.

Файлы:
- bot.py – v2.3 speed
- prompts.py – v3_unrestricted
- config.py – multi-provider
- scripts/filter_chat_export.py – бэкап с фильтром --days
- amvera.yml / Dockerfile / docker-compose.yml – готовы
