#!/usr/bin/env python3
"""
Фильтрованный экспорт чата для подгрузки в память.
- берёт только последние N дней
- убирает команды, ссылки, короткие мусорные сообщения
- дедупликация
- топ-мемы по частоте
"""
import argparse, json, sqlite3, re
from datetime import datetime, timedelta, timezone
from collections import Counter
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="predskazbot.sqlite3")
    ap.add_argument("--chat-id", type=int, required=False, help="например -1001801997590")
    ap.add_argument("--days", type=int, default=60, help="сколько последних дней брать")
    ap.add_argument("--min-len", type=int, default=12)
    ap.add_argument("--out", default="exports/chat_filtered.jsonl")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    since_ts = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())
    q = "SELECT * FROM raw_messages WHERE created_at >= ?"
    params = [since_ts]
    if args.chat_id:
        q += " AND chat_id = ?"
        params.append(args.chat_id)
    q += " ORDER BY created_at ASC"
    rows = conn.execute(q, params).fetchall()
    # фильтры
    seen = set()
    out = []
    freq = Counter()
    url_re = re.compile(r'https?://')
    for r in rows:
        text = (r["text"] or "").strip()
        if len(text) < args.min_len: continue
        if text.startswith("/"): continue  # команды
        if url_re.search(text) and len(text) < 40: continue
        norm = re.sub(r'\s+', ' ', text.lower())[:120]
        if norm in seen: continue
        seen.add(norm)
        out.append(dict(r))
        # мем-кандидаты: частые фразы 3-6 слов
        words = re.findall(r'[а-яА-ЯёЁa-zA-Z0-9_]{3,}', text.lower())
        if 2 <= len(words) <= 8:
            freq[" ".join(words[:5])] += 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for row in out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Сохранено {len(out)} сообщений из {len(rows)} (days={args.days}) → {args.out}")
    print("\nТоп-мемы/фразы:")
    for phrase, c in freq.most_common(20):
        if c > 1:
            print(f"  {c:3d} × {phrase}")
    print("\nДальше:")
    print(f"  python scripts/build_v2_seed.py --in {args.out} --out exports/v2-seed-filtered.jsonl")
    print("  # или вручную /remember самые жирные мемы")

if __name__ == "__main__":
    main()
