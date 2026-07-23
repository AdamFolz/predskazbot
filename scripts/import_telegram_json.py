#!/usr/bin/env python3
"""
Импорт истории из JSON-экспорта Telegram Desktop в базу PredskazBot (v1/v2 SQLite).

Использование:
  python scripts/import_telegram_json.py --json /data/result.json --db /data/predskazbot.sqlite3 --chat-id -1001801997590 --days 90
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def extract_text(text_obj) -> str:
    """Извлекает чистый текст из поля text Telegram JSON (может быть строкой или списком сущностей)."""
    if isinstance(text_obj, str):
        return text_obj.strip()
    if isinstance(text_obj, list):
        chunks = []
        for item in text_obj:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and "text" in item:
                chunks.append(item["text"])
        return "".join(chunks).strip()
    return ""


def main():
    ap = argparse.ArgumentParser(description="Импорт Telegram Desktop JSON в SQLite PredskazBot")
    ap.add_argument("--json", required=True, help="Путь к result.json от Telegram Desktop")
    ap.add_argument("--db", default="predskazbot.sqlite3", help="Путь к SQLite (по умолчанию predskazbot.sqlite3)")
    ap.add_argument("--chat-id", type=int, default=0, help="ID чата (например -1001801997590). Если 0, берётся из JSON или -1000")
    ap.add_argument("--days", type=int, default=90, help="Сколько последних дней истории загружать (по умолчанию 90)")
    args = ap.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"❌ Файл не найден: {json_path}")
        return 1

    print(f"📂 Чтение {json_path} ...")
    import gc
    with open(json_path, "r", encoding="utf-8", errors="ignore") as f:
        data = json.load(f)

    messages = data.get("messages", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    chat_id = args.chat_id
    if not chat_id and isinstance(data, dict):
        raw_id = data.get("id", 0)
        if raw_id:
            chat_id = -int(f"100{raw_id}") if not str(raw_id).startswith("-") else int(raw_id)
    if not chat_id:
        chat_id = -1001801997590
    print(f"🎯 Целевой chat_id: {chat_id}")

    # Очищаем лишнее из RAM
    if isinstance(data, dict):
        data.clear()
    del data
    gc.collect()

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA synchronous = OFF;")
    conn.execute("PRAGMA journal_mode = MEMORY;")
    
    # Инициализация таблиц, если база новая
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_raw_messages_chat_id_created_at
        ON raw_messages(chat_id, created_at);
    """)

    user_cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    col_fs = "first_seen_at" if "first_seen_at" in user_cols else "first_seen"
    col_ls = "last_seen_at" if "last_seen_at" in user_cols else "last_seen"

    known_users = {}
    messages_data = []
    total_count = 0

    conn.execute("BEGIN TRANSACTION;")
    while messages:
        m = messages.pop()
        if not isinstance(m, dict) or m.get("type") != "message":
            continue
        text = extract_text(m.get("text", ""))
        if not text or len(text) < 2 or text.startswith("/"):
            continue

        date_str = m.get("date", "")
        if not date_str:
            continue

        try:
            dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)

        if dt < cutoff_dt:
            continue

        from_name = m.get("from") or m.get("actor") or "Участник"
        from_id_raw = str(m.get("from_id", "") or m.get("actor_id", "") or "")
        id_match = re.search(r"\d+", from_id_raw)
        user_id = int(id_match.group(0)) if id_match else abs(hash(from_name)) % 1000000000

        iso_time = dt.isoformat(timespec="seconds")
        
        if user_id not in known_users:
            known_users[user_id] = (chat_id, user_id, "", from_name, iso_time, iso_time)
        else:
            old = known_users[user_id]
            new_fs = min(old[4], iso_time)
            new_ls = max(old[5], iso_time)
            known_users[user_id] = (old[0], old[1], old[2], from_name, new_fs, new_ls)

        messages_data.append((chat_id, user_id, "", from_name, text, iso_time))
        if len(messages_data) >= 5000:
            conn.executemany("""
                INSERT INTO raw_messages (chat_id, user_id, username, display_name, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, messages_data)
            total_count += len(messages_data)
            messages_data.clear()
            gc.collect()

    for u_row in known_users.values():
        try:
            conn.execute(f"""
                INSERT INTO users (chat_id, user_id, username, display_name, {col_fs}, {col_ls})
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    {col_ls} = excluded.{col_ls}
            """, u_row)
        except sqlite3.OperationalError:
            try:
                conn.execute(f"""
                    INSERT OR REPLACE INTO users (chat_id, user_id, username, display_name, {col_fs}, {col_ls})
                    VALUES (?, ?, ?, ?, ?, ?)
                """, u_row)
            except Exception:
                pass

    if messages_data:
        conn.executemany("""
            INSERT INTO raw_messages (chat_id, user_id, username, display_name, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, messages_data)
        total_count += len(messages_data)
        messages_data.clear()

    conn.commit()
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.close()
    gc.collect()

    print(f"✅ Успешно импортировано за последние {args.days} дней:")
    print(f"   👥 Участников: {len(known_users)}")
    print(f"   💬 Сообщений: {total_count}")
    print(f"\nБаза готова! Теперь после рестарта бот знает всю историю вашей конфы.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
