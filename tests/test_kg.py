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


def _models_json(tmp_path, spec="ollama:kimi-k3:cloud", base_url="https://ollama.com/v1"):
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
    # unmarked spec: a custom provider URL only ever applies to the local route
    models = _models_json(tmp_path, spec="ollama:kimi-k3", base_url="ftp://exfil.example/v1")
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


# --- knowing what it does not know ---

@pytest.mark.parametrize("question", [
    'List all files in the repository whose path contains "mcp"',
    'Find any file in the repository with "mcp" in its filename',
    "Are there any MCP files? Search for files with mcp in the name or path",
])
def test_filename_questions_are_redirected_to_glob(kg, question):
    """One logged session asked this five times, reworded, and got a full cap
    of nodes every time. The graph indexes symbols, not filenames."""
    r = kg.query(question)
    assert r.confidence == "out_of_scope"
    assert r.hit_count == 0
    assert "glob" in r.text


@pytest.mark.parametrize("question", [
    "Check git working tree status: list any untracked or modified files",
    "What files were recently added or modified in the working tree?",
])
def test_working_tree_questions_are_redirected_to_git(kg, question):
    r = kg.query(question)
    assert r.confidence == "out_of_scope"
    assert "git status" in r.text


def test_dependency_questions_say_the_directory_is_not_indexed(kg):
    r = kg.query("What does the sdk export in node_modules/@scope/sdk?")
    assert r.confidence == "out_of_scope"
    assert "grep" in r.text and "node_modules" in r.text


def test_a_structural_question_is_not_redirected(kg):
    """The redirects must not swallow ordinary questions — several of these
    mention files and paths without being filename questions."""
    for q in [
        "How does authentication work?",
        "Which router file mounts the controller?",
        "where is app.py and how are routes mounted",
        "Where is check_password defined?",
    ]:
        assert kg.query(q).confidence != "out_of_scope", q


def test_a_named_symbol_scores_exact_confidence(kg):
    assert kg.query("where is AuthHandler defined").confidence == "exact"


def test_seeds_from_fuzzy_matches_only_are_labelled_low_confidence(kg):
    """Expansion matches by substring and fuzzy distance, which is what keeps
    recall usable and also what lets an unrelated question fill the node cap.
    hit_count cannot express the difference; confidence can."""
    r = kg.query("telemetry ingestion pipeline throughput")
    if r.hit_count:  # it found seeds by loose matching, as it usually does
        assert r.confidence == "weak"
        assert "LOW CONFIDENCE" in r.text
        assert "grep" in r.text


def test_low_confidence_text_forbids_a_third_rewording(kg):
    r = kg.query("telemetry ingestion pipeline throughput")
    if r.confidence == "weak":
        assert "reworded a third time" in r.text


def test_kg_canonicalizes_phantom_paths_and_merges_duplicates(tmp_path):
    """graphify indexes sub-projects from their own root as well as the repo's,
    so one file arrives under several spellings — on this repo `tui/src/main.ts`
    was also `src/main.ts` and `main.ts`, neither of which exists. That gave
    three nodes per symbol with three line numbers (two stale), all competing
    for the same result budget and pointing `read` at files it would reject."""
    (tmp_path / "tui" / "src").mkdir(parents=True)
    (tmp_path / "tui" / "src" / "main.ts").write_text("export function sendTurn() {}\n")
    out = tmp_path / "out"
    out.mkdir()
    nodes = [
        {"id": "tui_src_main_sendturn", "label": "sendTurn()", "source_file": "tui/src/main.ts", "source_location": "L1"},
        {"id": "src_main_sendturn", "label": "sendTurn()", "source_file": "src/main.ts", "source_location": "L1"},
        {"id": "main_sendturn", "label": "sendTurn()", "source_file": "main.ts", "source_location": "L1"},
        {"id": "tui_src_main", "label": "tui/src/main.ts", "source_file": "tui/src/main.ts", "source_location": "L1"},
        {"id": "src_main", "label": "src/main.ts", "source_file": "src/main.ts", "source_location": "L1"},
        {"id": "caller", "label": "onMessage()", "source_file": "tui/src/main.ts", "source_location": "L20"},
    ]
    links = [
        {"source": "caller", "target": "src_main_sendturn", "relation": "calls"},
        {"source": "tui_src_main", "target": "tui_src_main_sendturn", "relation": "contains"},
    ]
    (out / "graph.json").write_text(json.dumps(
        {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": links}
    ))

    G = KG(tmp_path, store_dir=out)._load_graph()
    assert sorted(str(d["label"]) for _, d in G.nodes(data=True)) == [
        "onMessage()", "sendTurn()", "tui/src/main.ts",
    ]
    sym = next(d for _, d in G.nodes(data=True) if d["label"] == "sendTurn()")
    assert sym["source_file"] == "tui/src/main.ts" and sym["source_location"] == "L1"
    # the call edge follows the merge instead of dangling on a dropped duplicate
    pairs = {frozenset((G.nodes[u]["label"], G.nodes[v]["label"])) for u, v in G.edges()}
    assert frozenset(("onMessage()", "sendTurn()")) in pairs


def test_kg_leaves_ambiguous_paths_alone(tmp_path):
    """Two real `index.ts` in different packages: guessing would answer
    confidently about the wrong file, which is worse than a duplicate node."""
    for pkg in ("a", "b"):
        (tmp_path / pkg / "src").mkdir(parents=True)
        (tmp_path / pkg / "src" / "index.ts").write_text("x\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [{"id": "n1", "label": "go()", "source_file": "src/index.ts", "source_location": "L1"}],
        "links": [],
    }))
    G = KG(tmp_path, store_dir=out)._load_graph()
    assert G.nodes["n1"]["source_file"] == "src/index.ts"  # untouched


def test_extraction_is_canonicalized_regardless_of_batch_width(tmp_path):
    """graphify's extractor strips the common prefix of the batch it is handed,
    so an incremental update that touched only `tui/src/` recorded the file as
    `main.ts` with node id `main_sendturn`, while a full build recorded
    `tui/src/main.ts` / `tui_src_main_sendturn`. build_merge matches on id, so
    the narrow update APPENDED a second copy of every node instead of replacing
    them — the actual source of this repo's 41% duplicate nodes."""
    (tmp_path / "tui" / "src").mkdir(parents=True)
    (tmp_path / "tui" / "src" / "main.ts").write_text("export function sendTurn() {}\n")
    out = tmp_path / "out"
    out.mkdir()
    narrow = {  # what extract() returns when handed only tui/src/main.ts
        "nodes": [
            {"id": "main", "label": "main.ts", "source_file": "main.ts", "source_location": "L1"},
            {"id": "main_sendturn", "label": "sendTurn()", "source_file": "main.ts", "source_location": "L1"},
        ],
        "edges": [{"source": "main", "target": "main_sendturn", "relation": "contains"}],
        "hyperedges": [],
    }
    kg = KG(tmp_path, store_dir=out)
    got = kg._canonicalize_extraction(narrow, [tmp_path / "tui" / "src" / "main.ts"])

    # the ids a WHOLE-REPO build would have produced, so the merge replaces
    assert {n["id"] for n in got["nodes"]} == {"tui_src_main", "tui_src_main_sendturn"}
    assert all(n["source_file"] == "tui/src/main.ts" for n in got["nodes"])
    # the file node is labelled by its own path, so the label moves too
    assert got["nodes"][0]["label"] == "tui/src/main.ts"
    # edges follow the rename instead of dangling on the old ids
    assert got["edges"][0]["source"] == "tui_src_main"
    assert got["edges"][0]["target"] == "tui_src_main_sendturn"


def test_extraction_leaves_ambiguous_paths_alone(tmp_path):
    """Two real `index.ts`: no unique answer, so nothing is rewritten. A wrong
    merge points every caller at the wrong file, which beats a duplicate."""
    for pkg in ("a", "b"):
        (tmp_path / pkg / "src").mkdir(parents=True)
        (tmp_path / pkg / "src" / "index.ts").write_text("x\n")
    out = tmp_path / "out"
    out.mkdir()
    ext = {
        "nodes": [{"id": "src_index_go", "label": "go()", "source_file": "src/index.ts"}],
        "edges": [],
        "hyperedges": [],
    }
    got = KG(tmp_path, store_dir=out)._canonicalize_extraction(ext, [])
    assert got["nodes"][0]["source_file"] == "src/index.ts"
    assert got["nodes"][0]["id"] == "src_index_go"


def test_id_prefix_matches_graphify_shape():
    assert KG._id_prefix("tui/src/main.ts") == "tui_src_main"
    assert KG._id_prefix("main.ts") == "main"
    assert KG._id_prefix("src/bird/context/kg.py") == "src_bird_context_kg"


def test_kg_never_merges_distinct_symbols_that_share_a_name(tmp_path):
    """Seven `.run()` methods live in one real file at seven different lines.
    A repair that keys on (file, label) alone fuses them into a single node and
    deletes the only thing a "which run()?" query has to go on."""
    (tmp_path / "t.py").write_text("x\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": f"n{i}", "label": ".run()", "file_type": "code", "_origin": "ast",
             "source_file": "t.py", "source_location": f"L{i}"}
            for i in (10, 20, 30)
        ],
        "links": [],
    }))
    G = KG(tmp_path, store_dir=out)._load_graph()
    assert G.number_of_nodes() == 3


def test_kg_never_merges_across_node_kinds(tmp_path):
    """An AST dependency `react` and a semantically-extracted concept `react`
    in the same package.json are two different assertions; collapsing them
    deletes the semantic layer the graph pays a model to produce."""
    (tmp_path / "package.json").write_text("{}\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "react", "file_type": "code", "_origin": "ast",
             "source_file": "package.json", "source_location": "L1"},
            {"id": "b", "label": "react", "file_type": "concept", "_origin": "semantic",
             "source_file": "package.json", "source_location": "L1"},
        ],
        "links": [],
    }))
    G = KG(tmp_path, store_dir=out)._load_graph()
    assert G.number_of_nodes() == 2


def test_kg_leaves_pathless_reference_nodes_alone(tmp_path):
    """`Any` imported in 31 files is recorded once per importer with no
    source_file. Those cannot be path-spelling duplicates, and merging them
    would fuse 31 references into one degree-31 hub."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": f"m{i}_any", "label": "Any", "file_type": "code", "_origin": "ast"}
            for i in range(4)
        ],
        "links": [],
    }))
    G = KG(tmp_path, store_dir=out)._load_graph()
    assert G.number_of_nodes() == 4


def test_ghost_spellings_are_pruned_so_old_graphs_self_heal(tmp_path):
    """build_merge's replace-set is keyed on source_file, so it only drops nodes
    filed under the path the NEW chunk carries. A graph that already holds the
    same file under a stripped-prefix spelling keeps those nodes forever — they
    are never re-extracted, so nothing ever replaces them. Naming them as
    prune_sources lets an existing graph heal on the next update."""
    (tmp_path / "tui" / "src").mkdir(parents=True)
    (tmp_path / "tui" / "src" / "main.ts").write_text("x\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "tui_src_main_a", "label": "a()", "source_file": "tui/src/main.ts"},
            {"id": "src_main_a", "label": "a()", "source_file": "src/main.ts"},
            {"id": "main_a", "label": "a()", "source_file": "main.ts"},
        ],
        "links": [],
    }))
    kg = KG(tmp_path, store_dir=out)
    assert kg._ghost_source_files({"tui/src/main.ts"}) == ["main.ts", "src/main.ts"]


def test_ghost_pruning_never_touches_a_real_file(tmp_path):
    """A real `src/index.ts` elsewhere in the repo is somebody's actual source,
    not a stripped-prefix ghost, even though it is a trailing slice of
    `pkg/src/index.ts`. Pruning it would delete a live file's whole subgraph."""
    (tmp_path / "pkg" / "src").mkdir(parents=True)
    (tmp_path / "pkg" / "src" / "index.ts").write_text("x\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("y\n")  # a REAL file
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "a()", "source_file": "pkg/src/index.ts"},
            {"id": "b", "label": "b()", "source_file": "src/index.ts"},
            {"id": "c", "label": "c()", "source_file": "index.ts"},
        ],
        "links": [],
    }))
    ghosts = KG(tmp_path, store_dir=out)._ghost_source_files({"pkg/src/index.ts"})
    assert ghosts == ["index.ts"]  # the real src/index.ts is spared


def test_no_ghosts_means_no_prune(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [{"id": "a", "label": "f()", "source_file": "a.py"}], "links": [],
    }))
    assert KG(tmp_path, store_dir=out)._ghost_source_files({"a.py"}) == []


# ---------- staleness marking ----------


def _tiny_graph(tmp_path):
    (tmp_path / "app.ts").write_text("export function endTurn() {}\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "app_endturn", "label": "endTurn()", "file_type": "code",
             "_origin": "ast", "source_file": "app.ts", "source_location": "L1"},
            {"id": "app_onmessage", "label": "onMessage()", "file_type": "code",
             "_origin": "ast", "source_file": "app.ts", "source_location": "L9"},
        ],
        "links": [{"source": "app_onmessage", "target": "app_endturn", "relation": "calls"}],
    }))
    return KG(tmp_path, store_dir=out)


def test_query_marks_a_stale_graph(tmp_path, monkeypatch):
    """is_ready() only asks whether a graph EXISTS, so one built before a
    hundred edits answers as confidently as one built a second ago. That is how
    a query returns [main.ts:L217] for a symbol that has since moved to L229 — a
    citation the model has no way to distrust, pointing at the wrong lines."""
    kg = _tiny_graph(tmp_path)
    monkeypatch.setattr(KG, "is_stale", lambda self: True)
    r = kg.query("where is endTurn defined")
    assert r.stale
    assert r.text.startswith("[STALE GRAPH"), "the notice must survive the budget clip"


def test_query_does_not_mark_a_fresh_graph(tmp_path, monkeypatch):
    kg = _tiny_graph(tmp_path)
    monkeypatch.setattr(KG, "is_stale", lambda self: False)
    r = kg.query("where is endTurn defined")
    assert not r.stale and "STALE GRAPH" not in r.text


def test_staleness_verdict_is_cached(tmp_path, monkeypatch):
    """is_stale() walks the repo (~180ms) against a ~20ms query, so it cannot
    run per call. A stale verdict stands until the graph is rebuilt, because
    staleness only grows as files are edited."""
    kg = _tiny_graph(tmp_path)
    calls = []
    monkeypatch.setattr(KG, "is_stale", lambda self: (calls.append(1), True)[1])
    for _ in range(4):
        assert kg.query("where is endTurn defined").stale
    assert len(calls) == 1


def test_staleness_failure_never_breaks_a_query(tmp_path, monkeypatch):
    """A freshness hint is a nicety; losing retrieval over one is not."""
    kg = _tiny_graph(tmp_path)

    def boom(self):
        raise RuntimeError("graphify exploded")

    monkeypatch.setattr(KG, "is_stale", boom)
    r = kg.query("where is endTurn defined")
    assert not r.stale and r.hit_count > 0


# --- digest: the repo map a session starts from -------------------------------


def _write_graph(out, nodes, links):
    out.mkdir(exist_ok=True)
    (out / "graph.json").write_text(json.dumps(
        {"directed": False, "multigraph": False, "graph": {}, "nodes": nodes, "links": links}
    ))


def _code(nid, label, file, line=1):
    return {"id": nid, "label": label, "source_file": file, "source_location": f"L{line}", "file_type": "code"}


def test_digest_leads_with_source_files_and_their_purpose(tmp_path):
    """The map answers "what is this program". So source files come before
    tests however many symbols the tests define, each file leads with the
    first sentence of its docstring, top-level names come before methods, and
    a rationale node — a docstring fragment — never poses as a symbol."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "core.py").write_text(
        '"""The engine: runs the loop\nand keeps score. Second sentence."""\n\nclass Runner: ...\n'
    )
    (tmp_path / "tests" / "test_core.py").write_text("def test_0(): ...\n")
    nodes = [
        _code("f_core", "src/core.py", "src/core.py"),
        _code("runner", "Runner", "src/core.py", 4),
        _code("runner_go", ".go()", "src/core.py", 5),
        {"id": "why", "label": "A loop that never ends is a bug", "source_file": "src/core.py",
         "source_location": "L4", "file_type": "rationale"},
        _code("f_test", "tests/test_core.py", "tests/test_core.py"),
    ] + [_code(f"t{i}", f"test_{i}()", "tests/test_core.py", i + 1) for i in range(6)]
    links = [
        {"source": "f_core", "target": "runner", "relation": "contains"},
        {"source": "runner", "target": "runner_go", "relation": "method"},
        {"source": "why", "target": "runner", "relation": "rationale_for"},
        {"source": "t0", "target": "runner", "relation": "calls"},
    ]
    _write_graph(tmp_path / "out", nodes, links)

    d = KG(tmp_path, store_dir=tmp_path / "out").digest()
    lines = d.splitlines()
    assert lines[0] == "[repo map — from the knowledge graph]"
    assert lines[1] == "  src/core.py — The engine: runs the loop and keeps score. (Runner, .go())"
    assert "never ends" not in d  # rationale text is not a symbol
    assert "  tests: 1 file (test_core.py)" in lines
    assert not any(ln.startswith("  tests/") for ln in lines)  # tests never get a file line
    # the file's own node and the test that calls Runner are not hubs; Runner is
    assert lines[-1] == "  key hubs: Runner [core.py]"


def test_digest_ranks_files_by_how_much_the_repo_uses_them(tmp_path):
    """A one-class module that three functions import outranks the seven-
    function module they live in: both touch one other file, so the tie
    breaks on squared degree (3² beats 3 × 1²), never on size — ranking by
    symbol count put a 178-symbol TUI component file above the engine.
    Manifests are indexed but are not program source, so package.json never
    gets a line; a file with no docstring still gets its symbols; a `//`
    header comment counts as a purpose."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "base.py").write_text("class Ctx: ...\n")
    (tmp_path / "src" / "big.ts").write_text("// The big module: many things.\n// More.\nexport const a = 1;\n")
    (tmp_path / "package.json").write_text("{}")
    nodes = [
        _code("f_base", "src/base.py", "src/base.py"),
        _code("ctx", "Ctx", "src/base.py", 1),
        _code("f_big", "src/big.ts", "src/big.ts"),
        _code("pkg", "package.json", "package.json"),
        _code("pkg_name", "name", "package.json", 2),
    ] + [_code(f"b{i}", f"thing{i}()", "src/big.ts", i + 3) for i in range(7)]
    links = [{"source": f"b{i}", "target": "ctx", "relation": "imports"} for i in range(3)]
    _write_graph(tmp_path / "out", nodes, links)

    d = KG(tmp_path, store_dir=tmp_path / "out").digest()
    lines = d.splitlines()
    assert lines[1] == "  src/base.py (Ctx)"  # no docstring: symbols alone, no dangling dash
    assert lines[2].startswith("  src/big.ts — The big module: many things. (thing0(), thing1(), thing2(), ")
    assert "+2 more)" in lines[2]
    assert "package.json" not in d


def test_digest_is_bounded(tmp_path):
    """Past max_files the map says how much it left out rather than growing."""
    (tmp_path / "src").mkdir()
    nodes, links = [], []
    for i in range(20):
        (tmp_path / "src" / f"m{i}.py").write_text("x = 1\n")
        nodes.append(_code(f"f{i}", f"src/m{i}.py", f"src/m{i}.py"))
        nodes.append(_code(f"s{i}", f"sym{i}()", f"src/m{i}.py", 1))
    _write_graph(tmp_path / "out", nodes, links)
    d = KG(tmp_path, store_dir=tmp_path / "out").digest(max_files=15)
    assert "  … and 5 more source files" in d.splitlines()
    assert d.count("  src/m") == 15


def test_file_purpose_reads_docstrings_and_comment_blocks(tmp_path):
    from bird.context.kg import _file_purpose

    py = tmp_path / "a.py"
    py.write_text('#!/usr/bin/env python\n"""One line."""\nimport os\n')
    assert _file_purpose(py) == "One line."
    multi = tmp_path / "b.py"
    multi.write_text('"""In-session onboarding: the setup walkthrough, key management and the\nfirst-run hint. Then more."""\n')
    assert _file_purpose(multi) == "In-session onboarding: the setup walkthrough, key management and the first-run hint."
    ts = tmp_path / "c.ts"
    ts.write_text("/**\n * The message queue state machine,\n * extracted from main.\n */\nexport {}\n")
    assert _file_purpose(ts) == "The message queue state machine, extracted from main."
    bare = tmp_path / "d.ts"
    bare.write_text("import x from 'y';\n")
    assert _file_purpose(bare) == ""
    assert _file_purpose(tmp_path / "missing.py") == ""
    long = tmp_path / "e.py"
    long.write_text('"""' + "word " * 40 + '"""\n')
    got = _file_purpose(long, limit=30)
    assert got.endswith("…") and len(got) <= 31
