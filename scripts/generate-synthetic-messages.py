#!/usr/bin/env python3
"""Generate a deterministic, anonymized Telegram Desktop result.json."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

TOPICS = (
    ("регламент", "Закрытие месяца выполняется после проверки проводок и обмена."),
    ("интеграция", "Обмен между тестовыми базами использует очередь и повтор доставки."),
    ("производительность", "Замер запроса фиксирует план, время и число прочитанных строк."),
    ("резервирование", "Резервная копия проверяется восстановлением в отдельном окружении."),
    ("доступ", "Роли выдаются по минимально необходимым полномочиям."),
)


def make_message(index: int, start: datetime) -> dict[str, object]:
    topic, sentence = TOPICS[index % len(TOPICS)]
    marker = f"маркер-{index % 1000:04d}"
    message: dict[str, object] = {
        "id": index + 1,
        "type": "message",
        "date": (start + timedelta(seconds=index * 30)).isoformat().replace("+00:00", "Z"),
        "from": f"Участник {index % 25:02d}",
        "from_id": f"user{index % 25:02d}",
        "text": (
            f"Синтетическое сообщение {index + 1}. Тема: {topic}. "
            f"{sentence} Контрольный {marker}."
        ),
        "text_entities": [],
    }
    if index > 0 and index % 10 == 0:
        message["reply_to_message_id"] = index
    return message


def generate(path: Path, count: int, channel_id: int) -> tuple[float, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            '{"name":"Synthetic 1C benchmark","type":"private_supergroup",'
            f'"id":{channel_id},"messages":[\n'
        )
        for index in range(count):
            if index:
                stream.write(",\n")
            json.dump(make_message(index, start), stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n]}\n")
    return time.perf_counter() - started, path.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/synthetic-100k/result.json"))
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--channel-id", type=int, default=9_999_001)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    elapsed, size = generate(args.output, args.count, args.channel_id)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "messages": args.count,
                "bytes": size,
                "elapsed_seconds": round(elapsed, 3),
                "messages_per_second": round(args.count / elapsed, 1),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
