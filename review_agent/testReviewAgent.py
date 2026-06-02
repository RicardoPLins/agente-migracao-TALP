import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # .../review_agent/
sys.path.insert(0, str(HERE))

# Carrega variáveis de .env se existir (API_3, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env", override=False)
    load_dotenv(HERE.parent / ".env", override=False)  # raiz do repo também
except ImportError:
    pass

import importlib.util
spec = importlib.util.spec_from_file_location("review_agent", HERE / "review-agent.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

original = (HERE / "test1" / "original.py").read_text(encoding="utf-8")
migrado  = (HERE / "test1" / "migrado.py").read_text(encoding="utf-8")

result = mod._executar_grafo(original, migrado)
print(result["relatorio_final"])