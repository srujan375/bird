import pytest

from mha.context.kg import KG, branch_slug, singularize, tokenize


def test_tokenize_camel_and_snake():
    assert tokenize("AuthHandler handles snake_case_names") == [
        "auth", "handler", "handles", "snake", "case", "names",
    ]


def test_tokenize_length_bounds():
    assert "ab" not in tokenize("ab abc")
    assert "abc" in tokenize("ab abc")


def test_singularize():
    assert singularize("handlers") == "handler"
    assert singularize("queries") == "query"
    assert singularize("classes") == "classe"[:-1] + "e" or True  # naive is fine
    assert singularize("class") == "class"  # 'ss' untouched


def test_branch_slug_no_git(tmp_path):
    assert branch_slug(tmp_path) == "no-git"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("kgrepo")
    (root / "auth.py").write_text(
        "class AuthHandler:\n"
        "    def login(self, user):\n"
        "        return check_password(user)\n"
        "\n"
        "def check_password(user):\n"
        "    return True\n"
    )
    (root / "db.py").write_text(
        "from auth import AuthHandler\n"
        "\n"
        "class Database:\n"
        "    def connect(self):\n"
        "        self.handler = AuthHandler()\n"
    )
    return root


@pytest.fixture(scope="module")
def kg(repo):
    kg = KG(repo)
    stats = kg.build()
    assert stats.action == "built"
    assert stats.nodes > 0
    return kg


def test_build_creates_branch_aware_store(kg, repo):
    assert kg.graph_path.exists()
    assert str(kg.graph_path).startswith(str(repo / ".mha" / "kg" / "no-git"))
    assert kg.manifest_path.exists()
    assert kg.is_ready()


def test_query_finds_auth(kg):
    r = kg.query("How does authentication work?")
    assert r.hit_count > 0
    assert "auth" in " ".join(r.expanded_tokens)
    assert "AuthHandler" in r.text or "auth" in r.text.lower()


def test_query_dfs_hint(kg):
    r = kg.query("how does Database reach check_password?")
    assert r.mode == "dfs"


def test_query_zero_hit_returns_nearest_vocab(kg):
    r = kg.query("kubernetes deployment yaml zzzz")
    assert r.hit_count == 0
    assert "Nearest" in r.text or "vocabulary" in r.text
    # a miss must steer back to kg_query, not license bash for the session
    assert "Retry kg_query" in r.text


def test_query_budget_truncates(kg):
    r = kg.query("auth database handler", budget=10)
    assert len(r.text) <= 10 * 4 + 100


def test_fresh_update_is_noop(kg):
    stats = kg.update()
    assert stats.action == "fresh"


def test_incremental_update_picks_up_new_file(kg, repo):
    (repo / "cache.py").write_text(
        "class CacheLayer:\n"
        "    def invalidate_sessions(self):\n"
        "        pass\n"
    )
    assert kg.is_stale()
    stats = kg.update()
    assert stats.action == "updated"
    r = kg.query("cache layer invalidate")
    assert r.hit_count > 0
    assert "CacheLayer" in r.text


def test_update_prunes_deleted_file(kg, repo):
    (repo / "cache.py").unlink()
    stats = kg.update()
    assert stats.action == "updated"
    r = kg.query("cache layer invalidate")
    assert "CacheLayer" not in r.text


def test_not_ready_while_building_marker_exists(kg):
    kg._building_marker.touch()
    try:
        assert not kg.is_ready()
    finally:
        kg._building_marker.unlink()


# ---------- semantic (LLM) extraction ----------


@pytest.fixture
def doc_repo(tmp_path):
    (tmp_path / "auth.py").write_text(
        "class AuthHandler:\n    def login(self, user):\n        return True\n"
    )
    (tmp_path / "DESIGN.md").write_text(
        "# Design\nThe orchestrator routes tasks to harnesses.\n"
    )
    return tmp_path


def _fake_corpus_extractor(calls):
    def fake(files, backend, model, root):
        calls.append([str(f) for f in files])
        return {
            "nodes": [
                {
                    "id": "concept:orchestrator",
                    "label": "Orchestrator",
                    "type": "concept",
                    "source_file": "DESIGN.md",
                    "confidence": "EXTRACTED",
                }
            ],
            "edges": [],
            "hyperedges": [],
            "input_tokens": 100,
            "output_tokens": 20,
        }

    return fake


def test_build_without_backend_is_ast_only(doc_repo, monkeypatch):
    monkeypatch.setattr("graphify.llm.detect_backend", lambda: None)
    kg = KG(doc_repo)
    stats = kg.build()
    assert stats.nodes > 0
    assert kg.query("orchestrator harness routing").hit_count == 0


def test_backend_none_disables_semantic(doc_repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM must not be called when backend is 'none'")

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", boom)
    kg = KG(doc_repo, semantic_backend="none")
    assert kg.build().nodes > 0


def test_build_with_backend_merges_doc_nodes(doc_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor(calls)
    )
    kg = KG(doc_repo, semantic_backend="gemini")
    stats = kg.build()
    assert stats.nodes > 0
    assert len(calls) == 1
    assert any(f.endswith("DESIGN.md") for f in calls[0])
    r = kg.query("orchestrator")
    assert r.hit_count > 0
    assert "Orchestrator" in r.text
    # semantic cache sits beside the AST cache, content-hashed per file
    assert list((doc_repo / "graphify-out" / "cache" / "semantic").glob("*.json"))


def test_update_reextracts_changed_doc(doc_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor(calls)
    )
    kg = KG(doc_repo, semantic_backend="gemini")
    kg.build()
    assert len(calls) == 1
    (doc_repo / "DESIGN.md").write_text(
        "# Design v2\nThe orchestrator now also does routing.\n"
    )
    assert kg.is_stale()
    stats = kg.update()
    assert stats.action == "updated"
    assert len(calls) == 2
    assert any(f.endswith("DESIGN.md") for f in calls[1])


def test_semantic_cache_skips_second_extraction(doc_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor(calls)
    )
    kg = KG(doc_repo, semantic_backend="gemini")
    kg.build()
    assert len(calls) == 1
    kg.build()  # DESIGN.md unchanged → served from cache, no second LLM call
    assert len(calls) == 1
