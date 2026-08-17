"""Gemini 金鑰多把輪替(`src/design/api_keys.py`)測試。

重點:

  * 金鑰來源三種(單一環境變數、逗號分隔的環境變數、api_keys.json)都讀得到,
    去重且保持順序。
  * **額度用完會換下一把**,而且換過去之後就停在那把(不要每次都從第一把重試)。
  * **只有額度類錯誤才換**:金鑰打錯/網路斷掉換幾把都一樣,要直接往上丟。
  * 全部用完 → 一個講人話的錯誤,不是把原始 429 JSON 砸出來。
  * 沒有任何金鑰 → make_client 明確 raise,並告訴人怎麼設。

⚠️ 這組測試**完全不打真的 API**(factory 注入假客戶端)。免費額度是每把每天 20 次,
   拿來跑測試會直接把口試 demo 的額度燒光。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.design import api_keys as ak

ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY",
             "GEMINI_API_KEYS", "GOOGLE_API_KEYS")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """乾淨環境:清掉所有金鑰環境變數,金鑰檔指到 tmp(不碰真的那份)。"""
    for n in ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(ak, "KEY_FILE", tmp_path / "api_keys.json")
    return tmp_path


class Quota(Exception):
    """假的額度用完錯誤(訊息長得像 Gemini 真的回的那種)。"""

    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED: quota exceeded for model")


class Boom(Exception):
    """不是額度問題的錯誤(例如金鑰打錯)。"""


class FakeModels:
    def __init__(self, key, dead):
        self.key, self.dead = key, dead

    def generate_content(self, **kw):
        if self.key in self.dead:
            raise self.dead[self.key]
        return f"ok:{self.key}"


class FakeClient:
    def __init__(self, key, dead):
        self.models = FakeModels(key, dead)


def _factory(dead=None):
    dead = dead or {}
    return lambda key: FakeClient(key, dead)


# ── 金鑰來源 ────────────────────────────────────────────────────────────────
def test_reads_single_env_vars(clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "A")
    monkeypatch.setenv("GOOGLE_API_KEY", "B")
    assert ak.load_keys() == ["A", "B"]


def test_reads_many_keys_from_one_env_var(clean_env, monkeypatch):
    """★ 雲端主機常常只能設固定幾個變數名 → 一個變數塞多把(逗號分隔)。"""
    monkeypatch.setenv("GEMINI_API_KEYS", " A , B ;C ")
    assert ak.load_keys() == ["A", "B", "C"]


def test_reads_key_file_after_env(clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "A")
    (clean_env / "api_keys.json").write_text(
        json.dumps({"keys": ["B", "C"]}), encoding="utf-8")
    assert ak.load_keys() == ["A", "B", "C"]     # 環境變數優先,順序保持


def test_duplicates_are_dropped(clean_env, monkeypatch):
    """★ 同一把重複列不會變兩份額度(它就是同一把),去重免得白試一輪。"""
    monkeypatch.setenv("GEMINI_API_KEY", "A")
    monkeypatch.setenv("GOOGLE_API_KEY", "A")
    (clean_env / "api_keys.json").write_text(
        json.dumps({"keys": ["A", "B"]}), encoding="utf-8")
    assert ak.load_keys() == ["A", "B"]


def test_broken_key_file_is_ignored(clean_env, monkeypatch):
    """★ 金鑰檔壞掉(手改壞了)不該讓整個網站起不來。"""
    monkeypatch.setenv("GEMINI_API_KEY", "A")
    (clean_env / "api_keys.json").write_text("{ 這不是 json", encoding="utf-8")
    assert ak.load_keys() == ["A"]


def test_have_key_and_mask(clean_env, monkeypatch):
    # ⚠️ 這裡刻意用**明顯是假的**字串。測試檔會進 git,不要放真金鑰的片段——
    #    即使只留頭尾,那也是真憑證的一部分。
    fake = "XX.NotARealKey0000000000000000ZZZZ"
    assert not ak.have_key()
    monkeypatch.setenv("GEMINI_API_KEY", fake)
    assert ak.have_key()
    masked = ak.mask(fake)
    assert masked.startswith("XX.Not") and masked.endswith("ZZZZ")
    assert "RealKey00000" not in masked           # 中間一定要遮掉


# ── 輪替 ────────────────────────────────────────────────────────────────────
def test_falls_through_to_the_next_key_when_quota_runs_out(clean_env):
    """★★ 第一把額度用完 → 自動換第二把,呼叫端拿到正常結果。"""
    c = ak.RotatingClient(["A", "B"], factory=_factory({"A": Quota()}))
    assert c.models.generate_content(model="m") == "ok:B"


def test_stays_on_the_working_key(clean_env):
    """★★ 換過去之後就停在那把 —— 每次都從第一把重試等於每次都白燒一次呼叫。"""
    c = ak.RotatingClient(["A", "B"], factory=_factory({"A": Quota()}))
    c.models.generate_content(model="m")
    assert c.current_key == "B"
    assert c.models.generate_content(model="m") == "ok:B"


def test_non_quota_errors_are_raised_immediately(clean_env):
    """★★ 只有**額度**類錯誤才換金鑰。

    金鑰打錯、網路斷掉那種,換幾把都是同一個錯 —— 換了只是把同一個錯誤重複 N 遍、
    log 也更難看。要讓它直接往上丟。"""
    calls = []

    def factory(key):
        calls.append(key)
        return FakeClient(key, {key: Boom("金鑰無效")})

    c = ak.RotatingClient(["A", "B", "C"], factory=factory)
    with pytest.raises(Boom):
        c.models.generate_content(model="m")
    assert calls == ["A"], f"不該去試其他金鑰,卻試了 {calls}"


def test_all_keys_exhausted_gives_a_readable_error(clean_env):
    """★ 全部用完 → 講人話,不是把原始 429 JSON 砸給使用者。"""
    c = ak.RotatingClient(["A", "B"], factory=_factory({"A": Quota(), "B": Quota()}))
    with pytest.raises(RuntimeError, match="額度都用完"):
        c.models.generate_content(model="m")


def test_no_key_at_all_says_how_to_set_one(clean_env):
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        ak.make_client(factory=_factory())


def test_more_keys_means_more_quota(clean_env, monkeypatch):
    """★★ 這整個模組存在的理由:多一把金鑰 = 多一份額度。

    四把裡前三把都用完了,第四把仍然出得了圖。"""
    monkeypatch.setenv("GEMINI_API_KEYS", "A,B,C,D")
    dead = {"A": Quota(), "B": Quota(), "C": Quota()}
    c = ak.make_client(factory=_factory(dead))
    assert c.key_count == 4
    assert c.models.generate_content(model="m") == "ok:D"
