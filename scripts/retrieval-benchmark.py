#!/usr/bin/env python3
"""Benchmark Atlas keyword and semantic retrieval with synthetic data.

This script is designed to run inside the Atlas container. It loads 100k
raw messages directly into the durable MongoDB collection with extraction
already marked done, avoiding 100k LLM extraction calls. A deterministic
subset is embedded and inserted as AtomicFact rows for semantic retrieval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from beever_atlas.models import AtomicFact
from beever_atlas.stores.weaviate_store import WeaviateStore
from pymongo import MongoClient

TOPICS = (
    ("регламент", "Закрытие месяца выполняется после проверки проводок и обмена."),
    ("интеграция", "Обмен между тестовыми базами использует очередь и повтор доставки."),
    ("производительность", "Замер запроса фиксирует план, время и число прочитанных строк."),
    ("резервирование", "Резервная копия проверяется восстановлением в отдельном окружении."),
    ("доступ", "Роли выдаются по минимально необходимым полномочиям."),
)


def text_for(index: int) -> str:
    topic, sentence = TOPICS[index % len(TOPICS)]
    return (
        f"Синтетическое сообщение {index + 1}. Тема: {topic}. "
        f"{sentence} Контрольный маркер-{index % 1000:04d}."
    )


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def cgroup_memory() -> int | None:
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            with open(path, encoding="ascii") as stream:
                return int(stream.read().strip())
        except (FileNotFoundError, ValueError):
            pass
    return None


def make_documents(channel_id: str, count: int) -> list[dict[str, Any]]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    now = datetime.now(tz=UTC)
    return [
        {
            "source_id": "synthetic-benchmark",
            "channel_id": channel_id,
            "message_id": str(index + 1),
            "channel_name": "Synthetic retrieval benchmark",
            "timestamp": start + timedelta(seconds=index * 30),
            "author": f"user{index % 25:02d}",
            "author_name": f"Участник {index % 25:02d}",
            "content": text_for(index),
            "thread_id": str(index) if index > 0 and index % 10 == 0 else None,
            "attachments": [],
            "reactions": [],
            "reply_count": 0,
            "is_bot": False,
            "links": [],
            "raw_metadata": {
                "synthetic": True,
                "reply_to_message_id": str(index) if index > 0 and index % 10 == 0 else None,
            },
            "extraction_status": "done",
            "attempt_count": 0,
            "next_attempt_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for index in range(count)
    ]


async def embed(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    response = await client.post(
        "http://llama-embedding:8080/v1/embeddings",
        json={"model": "bge-m3", "input": texts},
        timeout=180,
    )
    response.raise_for_status()
    rows = sorted(response.json()["data"], key=lambda row: row["index"])
    vectors = [row["embedding"] for row in rows]
    if any(len(vector) != 1024 for vector in vectors):
        raise RuntimeError("Embedding dimension is not 1024.")
    return vectors


async def prepare_semantic(
    store: WeaviateStore,
    channel_id: str,
    count: int,
    batch_size: int,
) -> float:
    started = time.perf_counter()
    async with httpx.AsyncClient() as client:
        for offset in range(0, count, batch_size):
            indexes = list(range(offset, min(offset + batch_size, count)))
            texts = [text_for(index) for index in indexes]
            vectors = await embed(client, texts)
            facts = [
                AtomicFact(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{channel_id}:{index}")),
                    memory_text=text,
                    quality_score=1.0,
                    tier="atomic",
                    channel_id=channel_id,
                    platform="synthetic",
                    author_id=f"user{index % 25:02d}",
                    author_name=f"Участник {index % 25:02d}",
                    message_ts=str(index + 1),
                    source_message_id=str(index + 1),
                    topic_tags=[TOPICS[index % len(TOPICS)][0]],
                    text_vector=vector,
                )
                for index, text, vector in zip(indexes, texts, vectors, strict=True)
            ]
            await store.batch_upsert_facts(facts)
    return time.perf_counter() - started


async def time_requests(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    queries: list[Any],
    warmup: int,
) -> list[float]:
    latencies: list[float] = []
    for index, query in enumerate(queries):
        started = time.perf_counter()
        if method == "GET":
            response = await client.get(url, params=query)
        else:
            response = await client.post(url, json=query)
        elapsed = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        if not (payload.get("messages") or payload.get("results")):
            raise RuntimeError(f"Retrieval returned no results for query: {query!r}")
        if index >= warmup:
            latencies.append(elapsed)
    return latencies


async def run(args: argparse.Namespace) -> dict[str, Any]:
    token = os.environ.get("BEEVER_API_KEYS", "").split(",")[0].strip()
    if not token:
        raise RuntimeError("BEEVER_API_KEYS is not configured.")
    mongo = MongoClient(os.environ.get("MONGODB_URI", "mongodb://mongodb:27017/beever_atlas"))
    collection = mongo.get_default_database()["channel_messages"]
    store = WeaviateStore(
        os.environ.get("WEAVIATE_URL", "http://weaviate:8080"),
        os.environ.get("WEAVIATE_API_KEY", ""),
    )
    await store.startup()
    memory_before = cgroup_memory()
    try:
        collection.delete_many({"channel_id": args.channel_id, "source_id": "synthetic-benchmark"})
        await store.delete_by_channel(args.channel_id)

        load_started = time.perf_counter()
        for offset in range(0, args.messages, args.mongo_batch):
            docs = make_documents(args.channel_id, min(args.mongo_batch, args.messages - offset))
            for local_index, document in enumerate(docs):
                actual = offset + local_index
                document["message_id"] = str(actual + 1)
                document["timestamp"] = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(
                    seconds=actual * 30
                )
                document["content"] = text_for(actual)
                document["author"] = f"user{actual % 25:02d}"
                document["author_name"] = f"Участник {actual % 25:02d}"
            collection.insert_many(docs, ordered=False)
        mongo_seconds = time.perf_counter() - load_started

        semantic_seconds = await prepare_semantic(
            store, args.channel_id, args.semantic_sample, args.embedding_batch
        )

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(headers=headers, timeout=60) as client:
            total = args.warmup + args.requests
            keyword_queries = [
                {"q": f"маркер-{index % 1000:04d}", "limit": 20}
                for index in range(total)
            ]
            semantic_queries = [
                {
                    "query": TOPICS[index % len(TOPICS)][1],
                    "channel_id": args.channel_id,
                    "limit": 20,
                    "threshold": 0.0,
                }
                for index in range(total)
            ]
            keyword = await time_requests(
                client,
                "GET",
                f"http://localhost:8000/api/channels/{args.channel_id}/message-search",
                queries=keyword_queries,
                warmup=args.warmup,
            )
            semantic = await time_requests(
                client,
                "POST",
                "http://localhost:8000/api/search",
                queries=semantic_queries,
                warmup=args.warmup,
            )

        memory_after = cgroup_memory()
        return {
            "methodology": {
                "raw_messages": args.messages,
                "raw_index": "MongoDB compound text index (channel_id, content)",
                "semantic_indexed_facts": args.semantic_sample,
                "semantic_note": "Representative deterministic subset; no LLM fact extraction used.",
                "warmup_requests_per_mode": args.warmup,
                "measured_requests_per_mode": args.requests,
                "concurrency": 1,
            },
            "load": {
                "mongodb_seconds": round(mongo_seconds, 3),
                "mongodb_messages_per_second": round(args.messages / mongo_seconds, 1),
                "semantic_index_seconds": round(semantic_seconds, 3),
                "semantic_facts_per_second": round(args.semantic_sample / semantic_seconds, 1),
            },
            "keyword_ms": {
                "p50": round(percentile(keyword, 0.50), 2),
                "p95": round(percentile(keyword, 0.95), 2),
                "max": round(max(keyword), 2),
            },
            "semantic_ms": {
                "p50": round(percentile(semantic, 0.50), 2),
                "p95": round(percentile(semantic, 0.95), 2),
                "max": round(max(semantic), 2),
            },
            "resources": {
                "atlas_container_memory_before_bytes": memory_before,
                "atlas_container_memory_after_bytes": memory_after,
                "benchmark_process_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
        }
    finally:
        if not args.keep:
            collection.delete_many(
                {"channel_id": args.channel_id, "source_id": "synthetic-benchmark"}
            )
            await store.delete_by_channel(args.channel_id)
        await store.shutdown()
        mongo.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=100_000)
    parser.add_argument("--semantic-sample", type=int, default=2_000)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--mongo-batch", type=int, default=5_000)
    parser.add_argument("--embedding-batch", type=int, default=64)
    parser.add_argument("--channel-id", default="synthetic-benchmark-100k")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if min(args.messages, args.semantic_sample, args.requests) < 1:
        parser.error("message, semantic sample, and request counts must be positive")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
