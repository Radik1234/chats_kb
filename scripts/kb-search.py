#!/usr/bin/env python3
"""kb-search — компактный клиент к кластеру chats_kb (OpenSearch на VPS).

Понижает расход токенов при поиске по базе знаний: возвращает только
дату, автора и текст сообщения без служебных полей OpenSearch.

Использование:
  kb-search.py chats                          — список чатов (алиасы + счётчики)
  kb-search.py catalog [--chat PREFIX]        — реестр дней из kb_catalog
  kb-search.py search QUERY [opts]            — поиск по тексту
      --chat PREFIX       префикс индексов/алиас чата (напр. 1389409398)
      --date-from YYYY-MM-DD  ограничение снизу по дате
      --date-to YYYY-MM-DD    ограничение сверху по дате
      --author NAME       фильтр по автору (user_name)
      --size N            сколько сообщений вернуть (по умолчанию 10)
      --phrase            искать как фразу (match_phrase)
  kb-search.py stat                           — сводка по кластеру

Пароль берётся из /opt/chats_kb/.env — секреты в этот файл не дублировать.
"""
import argparse
import base64
import json
import ssl
import sys
import urllib.request

ENV_PATH = "/opt/chats_kb/.env"
ES_URL = "https://127.0.0.1:9200"
DATE_FIELD = "timestamp"
AUTHOR_FIELD = "user_name"
USER_ID_FIELD = "user_id"


def load_env(path=ENV_PATH):
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


_env = load_env()


def es(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ES_URL + path, data=data, method=method)
    auth = base64.b64encode(
        f"{_env.get('OPENSEARCH_USERNAME', 'admin')}:{_env['OPENSEARCH_INITIAL_ADMIN_PASSWORD']}".encode()
    ).decode()
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.load(resp)


def resolve_index(prefix):
    """Возвращает алиас или wildcard-паттерн для префикса чата."""
    if not prefix:
        return "*"
    aliases = es("GET", "/_alias/" + prefix + "*")
    if aliases:
        return prefix + "*"
    raise SystemExit(f"Нет чата с префиксом {prefix!r}. Смотри: kb-search.py chats")


def fmt_doc(hit):
    src = hit["_source"]
    date = (src.get(DATE_FIELD) or src.get("message_date") or "")[:16].replace("T", " ")
    author = str(src.get(AUTHOR_FIELD) or src.get(USER_ID_FIELD) or "?")
    text = " ".join(src.get("text", "").split())
    return f"{date} | {author} | {text}"


def cmd_chats(_):
    aliases = es("GET", "/_cat/aliases?format=json")
    seen = {}
    for row in aliases:
        alias = row.get("alias", "")
        if alias == "kb_catalog" or "_" not in alias:
            continue
        if alias not in seen:
            info = es("GET", f"/{alias}/_count")
            seen[alias] = info.get("count", 0)
    for alias, count in sorted(seen.items()):
        print(f"{alias}: {count} docs")


def cmd_catalog(args):
    indices = es("GET", "/_cat/indices/kb_catalog?format=json")
    if not indices:
        raise SystemExit("kb_catalog пуст")
    docs = es("GET", "/kb_catalog/_search", {"size": 10000, "sort": [{"index_name": "asc"}]})
    pattern = args.chat + "_" if args.chat else ""
    for hit in docs["hits"]["hits"]:
        src = hit["_source"]
        name = str(src.get("index_name") or src.get("index") or "")
        if pattern and pattern not in name:
            continue
        print(name, src.get("message_count", src.get("count", "")))


def cmd_search(args):
    index = resolve_index(args.chat)
    must = []
    if args.phrase:
        must.append({"match_phrase": {"text": args.query}})
    else:
        must.append({"match": {"text": args.query}})
    if args.author:
        must.append({"match": {AUTHOR_FIELD: args.author}})
    flt = []
    if args.date_from:
        flt.append({"range": {DATE_FIELD: {"gte": args.date_from}}})
    if args.date_to:
        flt.append({"range": {DATE_FIELD: {"lte": args.date_to}}})
    query = {"bool": {"must": must}}
    if flt:
        query["bool"]["filter"] = flt
    body = {
        "size": args.size,
        "query": query,
        "sort": [{DATE_FIELD: "asc"}],
        "_source": [DATE_FIELD, "message_date", AUTHOR_FIELD, USER_ID_FIELD, "text"],
    }
    result = es("GET", f"/{index}/_search", body)
    total = result["hits"]["total"].get("value", 0)
    print(f"# hits: {total} (показано {len(result['hits']['hits'])})")
    for hit in result["hits"]["hits"]:
        print(fmt_doc(hit))


def cmd_stat(_):
    health = es("GET", "/_cluster/health")
    print(f"cluster: {health.get('status')}")
    aliases = es("GET", "/_cat/aliases?format=json")
    seen = {}
    for row in aliases:
        alias = row.get("alias", "")
        if alias and alias != "kb_catalog":
            seen[alias] = True
    for alias in sorted(seen):
        count = es("GET", f"/{alias}/_count").get("count", 0)
        days = es("GET", f"/_cat/indices/{alias}_*?format=json&h=index")
        print(f"{alias}: {count} docs / {len(days)} дней")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("chats")
    p_cat = sub.add_parser("catalog")
    p_cat.add_argument("--chat")
    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--chat")
    p_search.add_argument("--date-from", dest="date_from")
    p_search.add_argument("--date-to", dest="date_to")
    p_search.add_argument("--author")
    p_search.add_argument("--size", type=int, default=10)
    p_search.add_argument("--phrase", action="store_true")
    sub.add_parser("stat")

    args = parser.parse_args()
    {"chats": cmd_chats, "catalog": cmd_catalog, "search": cmd_search, "stat": cmd_stat}[args.cmd](args)


if __name__ == "__main__":
    main()
