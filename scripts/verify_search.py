#!/usr/bin/env python3
"""Quick search check after import. No secrets printed."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "import"))
from import_telegram import OpenSearchHttp, load_dotenv  # noqa: E402


def main() -> int:
    load_dotenv()
    os.environ.setdefault("OPENSEARCH_URL", "https://127.0.0.1:9200")
    client = OpenSearchHttp()
    catalog = client.request("POST", "/kb_catalog/_search", {"query": {"match_all": {}}, "size": 20})
    hits = catalog.get("hits", {}).get("hits", [])
    total = catalog.get("hits", {}).get("total", {})
    print("catalog_total", total)
    if not hits:
        print("catalog empty")
        return 1
    for hit in hits:
        src = hit.get("_source", {})
        print(
            "catalog",
            src.get("index_name"),
            "messages",
            src.get("message_count"),
            "alias",
            src.get("alias"),
        )
    search = client.request(
        "POST",
        "/1393071168_mssqlplus1c/_search",
        {"query": {"match": {"text": "tempdb"}}, "size": 2},
    )
    stotal = search.get("hits", {}).get("total", {})
    print("tempdb_total", stotal)
    docs = search.get("hits", {}).get("hits", [])
    if not docs:
        return 1
    print("sample_id", docs[0].get("_source", {}).get("message_id"))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
