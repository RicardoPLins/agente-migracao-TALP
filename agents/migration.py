from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_SCRIPT = _PROJECT_ROOT / "migration_agent" / "langgraph-mig03.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_agent_langgraph_mig03", _MIGRATION_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load migration module from {_MIGRATION_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=4)
def _get_cached_assets(num_examples: int) -> tuple[Any, Any, str]:
    mod = _load_migration_module()
    examples = mod.carregar_exemplos_treino(num_examples)
    if not examples:
        raise RuntimeError(
            "No training examples loaded. Ensure dataset/Request-Urllib.xlsx exists."
        )
    system_prompt = mod.criar_prompt_treino(examples)
    graph = mod.criar_agente_migracao(examples, system_prompt)
    return mod, graph, system_prompt


def run_migration(code: str, *, num_examples: int = 30) -> dict[str, Any]:
    """Run the urllib→requests migration agent.

    Returns a dict with migrated_code, inference (dict when available), status, and messages.
    """
    code = (code or "").strip()
    if not code:
        raise ValueError("code must be a non-empty string")

    _, graph, _ = _get_cached_assets(int(num_examples))

    final_state = graph.invoke(
        {
            "messages": [HumanMessage(content="Migrar código urllib para requests")],
            "codigo_usuario": code,
            "codigo_migrado": "",
            "inferencia_semantica": "",
            "analise_agente": "",
            "status": "",
        }
    )

    inference: Any = None
    raw_inference = final_state.get("inferencia_semantica")
    if isinstance(raw_inference, str) and raw_inference.strip():
        try:
            inference = json.loads(raw_inference)
        except Exception:
            inference = raw_inference

    messages = []
    for msg in final_state.get("messages", []) or []:
        content = getattr(msg, "content", None)
        if content:
            messages.append(content)

    return {
        "status": final_state.get("status"),
        "messages": messages,
        "original_code": code,
        "migrated_code": final_state.get("codigo_migrado", ""),
        "semantic_inference": inference,
        "agent_analysis": final_state.get("analise_agente", ""),
    }
