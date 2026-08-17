"""全測試共用的保護措施。

## 為什麼需要這支

`src/design/api_keys.py` 除了環境變數,還會讀專案根目錄的 `api_keys.json`
(本機放金鑰的地方,已 gitignore)。這讓「清掉環境變數 → 應該沒有金鑰」的測試
**突然變成會打真的 API**:

    monkeypatch.delenv("GEMINI_API_KEY")   # 只清了環境變數
    ...                                     # api_keys.json 還在 → 有金鑰 → 真的送出請求

實測 `test_missing_api_key_is_503` 就這樣從 503 變成 502(真的打出去、失敗了)。
Gemini 免費額度是**每把金鑰每天 20 次**,跑一次全套測試就可能把口試 demo 的額度
燒光 —— 這比測試失敗嚴重得多。

所以這裡用 autouse fixture 把金鑰檔指到一個**不存在**的路徑:測試要有金鑰就自己
設環境變數(明示),絕不會從開發機的檔案「撿」到金鑰。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _no_local_api_keys(monkeypatch, tmp_path):
    """測試一律看不到本機的 api_keys.json(要金鑰請自己 setenv)。"""
    from src.design import api_keys
    monkeypatch.setattr(api_keys, "KEY_FILE", tmp_path / "no-such-api_keys.json")
