import json

from bot.config import ROOT, load_yaml


def test_configs_load():
    models = load_yaml("models")
    assert {"generator", "utility", "judge"} <= set(models["roles"])
    assert models["embeddings"]["dimensions"] <= 2000  # pgvector HNSW limit


def test_embedding_dim_matches_schema():
    sql = (ROOT / "src/bot/db/migrations/001_init.sql").read_text(encoding="utf-8")
    dim = load_yaml("models")["embeddings"]["dimensions"]
    assert f"vector({dim})" in sql


def test_manifest_files_exist():
    kb = ROOT / "data" / "kb"
    manifest = json.loads((kb / "manifest.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in kb.rglob("*") if p.is_file() and p.name != "manifest.json"}
    listed = {d["filename"] for d in manifest["documents"]}
    assert listed == on_disk
    assert len(listed) == manifest["total_documents"]
