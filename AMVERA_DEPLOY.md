# Деплой PredskazBot на Amvera – пошагово

1. Зарегистрируйся на https://cloud.amvera.ru – получишь 111₽ тест.
2. Нажми «Создать проект»
   - Название: predskazbot
   - Тариф: **Пробный 170₽/мес** (0,1 ГБ RAM хватает)
3. Способ загрузки:
   **Вариант А – через GitHub (рекомендую)**
   - Залей ветку `v2.1-final` в свой GitHub
   - В Amvera: «Импорт из GitHub» → выбери репозиторий `AdamFolz/BOTOVODYVROT-main`, ветка `v2.1-final`
   
   **Вариант B – через git push в Amvera**
   ```
   git remote add amvera https://git.amvera.ru/seksov/predskazbot
   git push amvera v2.1-final:master
   ```
   логин/пароль – от Amvera

   **Вариант C – ZIP upload через веб-интерфейс**
   - Скачай `predskazbot_v2.1-final.zip`
   - В Amvera: «Загрузить архив»
4. Файл `amvera.yml` уже в корне:
```
meta:
  environment: python
  toolchain:
    name: pip
    version: 3.12
build:
  requirementsPath: requirements.txt
run:
  scriptName: bot.py
  persistenceMount: /data
  containerPort: 80
```
5. Переменные окружения (вкладка «Конфигурация» → «Переменные окружения»):
```
TELEGRAM_BOT_TOKEN=123456:AAA...
OPENAI_API_KEY=твой_ключ_j3gb
OPENAI_BASE_URL=https://vip.j3gb.com/v1
OPENAI_MODEL=gpt-4o-mini
ADMIN_USER_ID=твой_telegram_user_id
ALLOWED_CHAT_IDS=
DATABASE_PATH=/data/predskazbot.sqlite3
V2_SQLITE_PATH=/data/predskazbot_v2.sqlite3
V2_MEMORY_ENABLED=1
V2_FULL_TRANSITION=0
KNOWLEDGE_BASE_DIR=AI_Knowledge_Base
```
ВАЖНО: `DATABASE_PATH=/data/...` и `V2_SQLITE_PATH=/data/...` – иначе база сотрётся при пересборке!

6. Нажми «Запустить» / «Пересобрать»
   Логи: вкладка «Логи»
   Должно быть:
```
LLM provider: proxy | model=gpt-4o-mini | base=https://vip.j3gb.com/v1 | key=sk-... 
PredskazBot started with v2 mode=bridge
```

7. В Telegram: `/whoami`, `/health`
   Если `/health` показывает:
```
llm_provider: proxy
llm_model: gpt-4o-mini
llm_base: https://vip.j3gb.com/v1
...
```
– всё ок.

Если 401:
- проверь `OPENAI_BASE_URL` заканчивается на `/v1`
- проверь ключ через локально: `python scripts/check_provider.py`
- в логах Amvera будет `AuthenticationError`

8. После успешного теста можно переключить тариф на «Начальный 290₽» – будет 0,5 ГБ RAM, стабильнее.

Стоимость:
- Пробный: 0,24₽/час = **~172₽/мес**
- Начальный: 0,4₽/час = **~288₽/мес**
- Тестовые 111₽ = **19 дней бесплатно на Пробном**

SQLite persistence:
- Всегда используй пути `/data/...`
- Бэкап: в Amvera есть кнопка «Скачать persistent storage» или делай `/export_me` в боте
