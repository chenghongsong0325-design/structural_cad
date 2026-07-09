"""pytest 設定:把專案根目錄放進 sys.path,讓測試能 `import src...`。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
