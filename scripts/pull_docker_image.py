#!/usr/bin/env python3
"""Pull a Docker Hub image without the daemon HTTP proxy and docker load it."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token"

ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with opener().open(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with opener().open(req, timeout=300) as resp:
        return resp.read()


def parse_image(ref: str) -> tuple[str, str]:
    if ":" in ref and "/" in ref.split(":")[-1] or ref.count(":") == 0:
        return ref, "latest"
    name, tag = ref.rsplit(":", 1)
    return name, tag


def token(repository: str) -> str:
    qs = urllib.parse.urlencode(
        {
            "service": "registry.docker.io",
            "scope": f"repository:{repository}:pull",
        }
    )
    data = http_json(f"{AUTH}?{qs}")
    return data["token"]


def headers(tok: str, accept: bool = False) -> dict[str, str]:
    out = {"Authorization": f"Bearer {tok}"}
    if accept:
        out["Accept"] = ACCEPT
    return out


def pick_platform(manifest: dict) -> dict:
    media = manifest.get("mediaType", "")
    if "index" in media or "manifest.list" in media:
        for item in manifest.get("manifests", []):
            plat = item.get("platform") or {}
            if plat.get("os") == "linux" and plat.get("architecture") in {"amd64", "x86_64"}:
                return item
        raise RuntimeError("no linux/amd64 manifest in list")
    return manifest


def download_blob(repository: str, digest: str, tok: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    url = f"{REGISTRY}/v2/{repository}/blobs/{digest}"
    data = http_bytes(url, headers(tok))
    dest.write_bytes(data)
    print(f"  blob {digest[:19]} {len(data)} bytes", flush=True)


def load_image(ref: str) -> None:
    repository, tag = parse_image(ref)
    print(f"pulling {repository}:{tag}", flush=True)
    tok = token(repository)
    manifest = http_json(
        f"{REGISTRY}/v2/{repository}/manifests/{tag}",
        headers(tok, accept=True),
    )
    chosen = pick_platform(manifest)
    if "digest" in chosen and "layers" not in chosen:
        digest = chosen["digest"]
        manifest = http_json(
            f"{REGISTRY}/v2/{repository}/manifests/{digest}",
            headers(tok, accept=True),
        )
    config_digest = manifest["config"]["digest"]
    layers = manifest["layers"]

    tmp = Path(tempfile.mkdtemp(prefix="docker-pull-"))
    try:
        config_name = config_digest.removeprefix("sha256:") + ".json"
        download_blob(repository, config_digest, tok, tmp / config_name)
        layer_files: list[str] = []
        for layer in layers:
            digest = layer["digest"]
            hexdigest = digest.removeprefix("sha256:")
            gz_path = tmp / f"{hexdigest}.tar.gz"
            raw_dir = tmp / hexdigest
            raw_dir.mkdir(exist_ok=True)
            download_blob(repository, digest, tok, gz_path)
            layer_tar = raw_dir / "layer.tar"
            with gzip.open(gz_path, "rb") as src, layer_tar.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            (raw_dir / "VERSION").write_text("1.0\n", encoding="utf-8")
            (raw_dir / "json").write_text(
                json.dumps({"id": hexdigest, "config": {}}), encoding="utf-8"
            )
            layer_files.append(f"{hexdigest}/layer.tar")
            gz_path.unlink()

        manifest_file = [
            {
                "Config": config_name,
                "RepoTags": [f"{repository}:{tag}"],
                "Layers": layer_files,
            }
        ]
        (tmp / "manifest.json").write_text(json.dumps(manifest_file), encoding="utf-8")
        tar_path = tmp / "image.tar"
        with tarfile.open(tar_path, "w") as tar:
            for path in tmp.iterdir():
                if path.name == "image.tar":
                    continue
                tar.add(path, arcname=path.name)
        print("docker load ...", flush=True)
        subprocess.run(["docker", "load", "-i", str(tar_path)], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: pull_docker_image.py repo:tag [repo:tag ...]", file=sys.stderr)
        return 2
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    for ref in sys.argv[1:]:
        load_image(ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
