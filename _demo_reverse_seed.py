import sys, os
sys.path.insert(0, "src")
os.environ.setdefault("MHA_KG_BACKEND", "none")

from pathlib import Path
from mha.harnesses.arch.run import run_arch_interactive
from mha.llm.registry import Registry
from mha.llm.wire.openai_compat import OpenAICompatClient
from mha.context.kg import KG

repo = Path.cwd()
registry = Registry.load()
spec = registry.resolve("architect")
client = OpenAICompatClient()
kg = KG(repo)

run_dir = repo / ".mha" / "sessions" / "demo-reverse-seed"
run_dir.mkdir(parents=True, exist_ok=True)

task = """Use the `import_state` tool with scope "kg" to load the current architecture of the knowledge graph module from the code knowledge graph. This will display the as-is architecture on the Workbench. After importing, STOP — do not propose modifications, do not call `done`, do not finalize. Just load the current state and let the user review the diagram."""

run_arch_interactive(
    repo_root=repo,
    task=task,
    registry=registry,
    client=client,
    run_dir=run_dir,
    kg=kg,
    model="architect",
    no_open=False,
)