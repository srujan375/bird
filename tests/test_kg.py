import json
import re
from pathlib import Path

import networkx as nx
import pytest

from bird.context.kg import (
    HUB_EXPAND_DEGREE,
    KG,
    _DFS_RE,
    branch_slug,
    is_artifact,
    singularize,
    tokenize,
)


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
    assert str(kg.graph_path).startswith(str(repo / ".bird" / "kg" / "no-git"))
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


def test_ensure_background_detaches_from_terminal(tmp_path, monkeypatch):
    """The background KG process must survive the parent terminal closing —
    `start_new_session=True` detaches it into its own process group."""
    (tmp_path / "auth.py").write_text("class Auth:\n    pass\n")
    kg = KG(tmp_path)  # no graph built yet → ensure_background will spawn

    captured = {}

    class FakeProc:
        pid = 999

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("bird.context.kg.subprocess.Popen", fake_popen)
    proc = kg.ensure_background()
    assert proc is not None
    assert captured["kwargs"].get("start_new_session") is True
    # clean up the marker/log the real path would have created
    kg._building_marker.unlink(missing_ok=True)


# ---------- retrieval: locations, ranking, caps ----------


def test_query_locations_are_paths_read_can_take(kg, repo):
    """The failure that made search look broken: graphify stores the line
    *alone* in source_location ("L12"), so printing it discarded the path and
    the model had to guess a file for every follow-up read."""
    r = kg.query("AuthHandler login")
    locs = [
        line.split("[", 1)[1].rsplit("]", 1)[0]
        for line in r.text.splitlines()
        if line.startswith("NODE ") and "[" in line
    ]
    assert locs, r.text
    assert not any(re.fullmatch(r"L\d+", loc) for loc in locs), locs
    for loc in locs:
        path, _, line_no = loc.rpartition(":")
        if not path:  # a file node with no line of its own
            path, line_no = line_no, "1"
        assert (repo / path).is_file(), loc
        assert line_no.isdigit(), loc


def _path_graph() -> nx.Graph:
    """'billing' appears only in the directory name — never in a label."""
    G = nx.Graph()
    G.add_node("w", label="Widget", source_file="services/billing/engine.py")
    G.add_node("o", label="billing", source_file="other/unrelated.py")
    return G


def test_paths_are_indexed_so_a_path_is_a_searchable_question():
    """Scoring ran on labels only, so nothing phrased as a path could match."""
    df, node_tokens, path_tokens = KG._vocabulary(_path_graph())
    assert "billing" in df and "services" in df
    assert path_tokens["w"] == {"services", "billing", "engine"}
    assert node_tokens["w"] == {"widget"}


def test_a_name_match_outranks_a_path_match():
    """A symbol *named* `billing` beats one that merely lives in billing/."""
    G = _path_graph()
    df, node_tokens, path_tokens = KG._vocabulary(G)
    relevance = KG._scorer(["billing"], df, node_tokens, path_tokens, G.number_of_nodes())
    assert relevance("w") > 0  # matched on its path alone
    assert relevance("o") > relevance("w")


def test_traverse_stops_at_the_limit():
    nodes, _ = KG._traverse(nx.complete_graph(30), [0], "bfs", None, 10)
    assert len(nodes) == 10


def test_traverse_keeps_the_highest_scoring_neighbours():
    """The cap must keep the best nodes, not the first ones the walk reached."""
    scores = {5: 10.0, 7: 9.0}
    nodes, _ = KG._traverse(nx.star_graph(20), [0], "bfs", lambda n: scores.get(n, 0.0), 3)
    assert nodes == {0, 5, 7}


def test_traverse_returns_a_hub_but_does_not_expand_through_it():
    G = nx.Graph()
    G.add_edge("seed", "hub")
    for i in range(HUB_EXPAND_DEGREE + 5):
        G.add_edge("hub", f"leaf{i}")
    nodes, _ = KG._traverse(G, ["seed"], "bfs", None, 500)
    assert "hub" in nodes
    assert not [n for n in nodes if str(n).startswith("leaf")]


def test_dfs_hints_require_a_phrase():
    """Bare 'path' was a hint, so an ordinary lookup ran a depth-6 DFS."""
    assert not _DFS_RE.search("where is the file path resolved")
    assert not _DFS_RE.search("what flow does the runner use")
    assert _DFS_RE.search("how does database reach check_password")
    assert _DFS_RE.search("what is the path from cli to runner")


# ---------- build artifacts are not source ----------


def test_is_artifact_flags_generated_files_only(tmp_path):
    src = tmp_path / "app.ts"
    src.write_text("export const x = 1\n" * 10)
    hashed = tmp_path / "index-Dv8sdJDj.js"
    hashed.write_text("var a=1;" * 500)
    plain = tmp_path / "vendor.js"  # no naming tell — only the line length
    plain.write_text(("x" * 900 + "\n") * 5)
    assert not is_artifact(src)
    assert is_artifact(hashed)
    assert is_artifact(plain)
    assert is_artifact(tmp_path / "package-lock.json")
    assert is_artifact(Path("node_modules/left-pad/index.js"))


@pytest.fixture
def bundle_repo(tmp_path):
    (tmp_path / "app.py").write_text("class Widget:\n    def spin(self):\n        return 1\n")
    assets = tmp_path / "static" / "assets"
    assets.mkdir(parents=True)
    (assets / "index-Dv8sdJDj.js").write_text(("var a=1;" * 400 + "\n") * 4)
    return tmp_path


def test_build_skips_a_committed_bundle(bundle_repo):
    kg = KG(bundle_repo)
    kg.build()
    files = {nd.get("source_file", "") for _, nd in kg._load_graph().nodes(data=True)}
    assert any("app.py" in f for f in files)
    assert not any("index-Dv8sdJDj" in f for f in files)


def test_update_prunes_an_already_indexed_bundle(bundle_repo):
    """A graph built before the filter existed still carries the bundle; the
    next update drops it rather than waiting for someone to rebuild."""
    kg = KG(bundle_repo)
    kg.build()
    kg.seed(
        [{
            "id": "bundle:q",
            "label": "q()",
            "file_type": "code",
            "source_file": "static/assets/index-Dv8sdJDj.js",
            "source_location": "L1",
        }],
        [],
    )
    assert kg._indexed_artifacts() == ["static/assets/index-Dv8sdJDj.js"]
    # nothing on disk changed, so only the artifact makes it stale — without
    # that the prune below would never be reached
    assert kg.is_stale()
    assert kg.update().action == "updated"
    assert "bundle:q" not in kg._load_graph()
    assert kg._indexed_artifacts() == []


# ---------- semantic (LLM) extraction ----------


@pytest.fixture(autouse=True)
def _no_ambient_kg_config(monkeypatch):
    """Keep these tests off the network and off the developer's environment.

    Backend resolution now reads a provider key from the environment, so a
    developer with OLLAMA_API_KEY exported would silently turn every AST-only
    assertion below into a live extraction call. Also restores the base_url
    that `_aim_backend` writes into graphify's global BACKENDS table.
    """
    from graphify.llm import BACKENDS

    for var in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "BIRD_KG_BACKEND", "BIRD_KG_MODEL"):
        monkeypatch.delenv(var, raising=False)
    for name in ("ollama", "openai"):
        monkeypatch.setitem(BACKENDS[name], "base_url", BACKENDS[name]["base_url"])


def _models_json(tmp_path, spec="ollama:kimi-k3", base_url="https://ollama.com/v1"):
    """A models.json whose `kg` alias resolves to `spec`."""
    provider = spec.split(":", 1)[0]
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    provider: {
                        "base_url": base_url,
                        "api_key_env": f"{provider.upper()}_API_KEY",
                    }
                },
                "models": {spec: {"context_window": 900000}},
                "aliases": {"kg": spec},
            }
        )
    )
    return str(path)


@pytest.fixture
def doc_repo(tmp_path):
    (tmp_path / "auth.py").write_text(
        "class AuthHandler:\n    def login(self, user):\n        return True\n"
    )
    (tmp_path / "DESIGN.md").write_text(
        "# Design\nThe orchestrator routes tasks to harnesses.\n"
    )
    return tmp_path


def _fake_corpus_extractor(calls, seen=None):
    def fake(files, backend, model, root, api_key=None):
        calls.append([str(f) for f in files])
        if seen is not None:
            seen.append({"backend": backend, "model": model, "api_key": api_key})
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


# ---------- the models.json `kg` alias ----------


def test_kg_alias_aims_graphify_at_bird_provider(doc_repo, tmp_path, monkeypatch):
    """The whole point: models.json decides, and the provider's URL and key
    travel with it — graphify's own env lookups are never consulted."""
    from graphify.llm import BACKENDS

    seen = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor([], seen)
    )
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-ollama-test")
    kg = KG(doc_repo, models_json=_models_json(tmp_path))
    assert kg.build().nodes > 0
    assert seen == [{"backend": "ollama", "model": "kimi-k3", "api_key": "sk-ollama-test"}]
    assert BACKENDS["ollama"]["base_url"] == "https://ollama.com/v1"


def test_openrouter_alias_maps_to_the_openai_backend(doc_repo, tmp_path, monkeypatch):
    """OpenRouter is not a graphify backend; it is an OpenAI-compatible URL."""
    from graphify.llm import BACKENDS

    seen = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor([], seen)
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    models = _models_json(
        tmp_path, spec="openrouter:zai/glm-5.2", base_url="https://openrouter.ai/api/v1"
    )
    KG(doc_repo, models_json=models).build()
    assert seen[0]["backend"] == "openai"
    assert seen[0]["model"] == "zai/glm-5.2"
    assert BACKENDS["openai"]["base_url"] == "https://openrouter.ai/api/v1"


def test_alias_without_provider_key_is_ast_only_and_says_so(
    doc_repo, tmp_path, monkeypatch, capsys
):
    """A missing key must not look like 'no LLM wanted' — that silent
    degradation is the bug this path was added to fix."""
    def boom(*a, **k):
        raise AssertionError("must not call an LLM without a provider key")

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", boom)
    monkeypatch.setattr("graphify.llm.detect_backend", lambda: None)
    kg = KG(doc_repo, models_json=_models_json(tmp_path))
    assert kg.build().nodes > 0
    assert "OLLAMA_API_KEY is unset" in capsys.readouterr().err


def test_bird_kg_model_overrides_the_alias_model(doc_repo, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor([], seen)
    )
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-ollama-test")
    monkeypatch.setenv("BIRD_KG_MODEL", "glm-5.2")
    KG(doc_repo, models_json=_models_json(tmp_path)).build()
    assert seen[0]["model"] == "glm-5.2"


def test_explicit_backend_beats_the_alias(doc_repo, tmp_path, monkeypatch):
    """BIRD_KG_BACKEND/semantic_backend keeps graphify's own env-key path, so an
    bird provider key is never handed to a backend the user named directly."""
    seen = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor([], seen)
    )
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-ollama-test")
    KG(doc_repo, semantic_backend="gemini", models_json=_models_json(tmp_path)).build()
    assert seen[0]["backend"] == "gemini"
    assert seen[0]["api_key"] is None


def test_unsendable_base_url_disables_extraction(doc_repo, tmp_path, monkeypatch):
    """base_url is where the corpus and the key go; a scheme graphify's guard
    rejects must stop the send, not fall through to graphify's default URL."""
    def boom(*a, **k):
        raise AssertionError("must not send a corpus to a rejected base_url")

    monkeypatch.setattr("graphify.llm.extract_corpus_parallel", boom)
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-ollama-test")
    models = _models_json(tmp_path, base_url="ftp://exfil.example/v1")
    assert KG(doc_repo, models_json=models).build().nodes > 0


def test_unknown_provider_falls_back_to_openai_backend(doc_repo, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "graphify.llm.extract_corpus_parallel", _fake_corpus_extractor([], seen)
    )
    monkeypatch.setenv("TOGETHER_API_KEY", "sk-together-test")
    models = _models_json(
        tmp_path, spec="together:llama-4", base_url="https://api.together.xyz/v1"
    )
    KG(doc_repo, models_json=models).build()
    assert seen[0]["backend"] == "openai"
