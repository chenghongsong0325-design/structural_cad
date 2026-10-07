from types import SimpleNamespace as NS
import hashlib
import json
import numpy as np
import pytest
from src.knowledge import onnx_e5 as onnx, rag


def test_masked_pooling_prefix_and_input_contract(tmp_path, monkeypatch):
    engine = onnx.OnnxE5Embedder(tmp_path)
    seen = []
    class Tokenizer:
        def encode(self, text):
            seen.append(text)
            return NS(ids=[1, 2], attention_mask=[1, 0], type_ids=[0, 0])
    class Session:
        def get_inputs(self):
            return [NS(name='input_ids'), NS(name='attention_mask')]
        def run(self, _, inputs):
            assert inputs['input_ids'].dtype == np.int64
            assert set(inputs) == {'input_ids', 'attention_mask'}
            hidden = np.zeros((1, 2, 384), dtype=np.float32)
            hidden[0, 0, :2] = [3, 4]
            hidden[0, 1, :2] = [900, 100]  # masked padding must not change the vector
            return [hidden]
    monkeypatch.setattr(engine, '_load', lambda: None)
    engine._tokenizer, engine._session = Tokenizer(), Session()
    result = engine.encode(['住宅'], query=True)
    assert result.shape == (1, 384) and result.dtype == np.float32
    assert np.allclose(result[0, :2], [.6, .8])
    assert np.linalg.norm(result[0]) == pytest.approx(1)
    engine.encode(['車庫'])
    assert seen == ['query: 住宅', 'passage: 車庫']


def test_bad_download_does_not_replace_existing_model_or_publish_manifest(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(onnx, 'FILES', {'model.onnx': ('model.onnx', hashlib.sha256(b'expected').hexdigest())})
    (tmp_path / 'model.onnx').write_bytes(b'old model')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, _): yield b'corrupt model'
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match='checksum'):
        onnx.prepare(tmp_path)
    assert (tmp_path / 'model.onnx').read_bytes() == b'old model'
    assert not (tmp_path / 'rag_model.json').exists()
    assert not list(tmp_path.glob('*.partial'))


def test_backend_changes_signature_and_does_not_reuse_torch_index(tmp_path, monkeypatch):
    monkeypatch.setenv('RAG_BACKEND', 'onnx')
    monkeypatch.setenv('RAG_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('RAG_MODEL_DIR', raising=False)
    engine = rag.get_retriever()
    assert engine.embedder.signature == onnx.SIGNATURE
    assert engine.embedder.signature != rag.MODEL_SIGNATURE
    assert engine.embedder.model_dir == tmp_path / 'model-onnx'
    (tmp_path / 'rag_model.json').write_text(json.dumps({'signature': rag.MODEL_SIGNATURE}))
    with pytest.raises(ValueError, match='signature'):
        onnx.OnnxE5Embedder(tmp_path).check_ready()


def test_sentencepiece_alignment_special_tokens_whitespace_and_limit(tmp_path, monkeypatch):
    import sys
    model = tmp_path / '分詞模型.model'
    model.write_bytes(b'checked model')
    class Processor:
        def __init__(self, *, model_proto):
            assert model_proto == b'checked model'
        def vocab_size(self): return 250000
        def encode(self, text, out_type):
            assert out_type is int
            return [0, 3] if text.strip() else []
    monkeypatch.setitem(sys.modules, 'sentencepiece', NS(SentencePieceProcessor=Processor))
    tokenizer = onnx.SentencePieceTokenizer(model)
    encoded = tokenizer.encode('unknown <mask> ')
    assert encoded.ids == [0, 3, 4, 6, 250001, 6, 2]
    assert encoded.attention_mask == [1] * 7
    assert encoded.type_ids == [0] * 7
    assert tokenizer.encode('<s><pad></s><unk>').ids == [0, 0, 1, 2, 3, 2]
    tokenizer.processor.encode = lambda *a, **k: [3] * 900
    assert tokenizer.encode('long').ids == [0] + [4] * 510 + [2]
