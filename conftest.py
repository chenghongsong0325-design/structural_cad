"""pytest 設定:把專案根目錄放進 sys.path,讓測試能 `import src...`。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _rag_offline_by_default(monkeypatch):
    """Existing tests use fake LLMs; RAG tests explicitly enable an injected embedder."""
    monkeypatch.setenv("RAG_ENABLED", "0")
