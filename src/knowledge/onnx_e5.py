"""Pinned E5 int8 CPU inference; downloads occur only during explicit preparation."""
import hashlib
import json
from pathlib import Path
import threading
import numpy as np

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
SIGNATURE = f"{MODEL}@{REVISION}:onnx-qint8:mean:query-passage:l2:512:v1"
FILES = {
    "model.onnx": ("onnx/model_qint8_avx512_vnni.onnx", "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
    "tokenizer.json": ("tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}


def file_digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def prepare(directory: Path):
    import requests
    directory.mkdir(parents=True, exist_ok=True)
    for name, (remote, digest) in FILES.items():
        path = directory / name
        if path.is_file() and file_digest(path) == digest:
            continue
        temporary = directory / (name + ".partial")
        try:
            with requests.get(f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}", stream=True, timeout=(20, 90)) as response, temporary.open("wb") as output:
                response.raise_for_status()
                size, checksum = 0, hashlib.sha256()
                for block in response.iter_content(1024 * 1024):
                    size += len(block)
                    if size > 150_000_000:
                        raise ValueError("model file exceeds expected limit")
                    checksum.update(block)
                    output.write(block)
            if checksum.hexdigest() != digest:
                raise ValueError("E5 model checksum mismatch")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    (directory / "rag_model.json").write_text(json.dumps({"signature": SIGNATURE}), encoding="utf-8")


class OnnxE5Embedder:
    signature = SIGNATURE

    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)
        self._lock = threading.RLock()
        self._session = None

    def _load(self):
        if self._session is not None:
            return
        manifest = json.loads((self.model_dir / "rag_model.json").read_text(encoding="utf-8"))
        if manifest.get("signature") != self.signature:
            raise ValueError("ONNX model signature mismatch; run prepare-onnx")
        import onnxruntime as ort
        from tokenizers import Tokenizer
        tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        session = ort.InferenceSession(str(self.model_dir / "model.onnx"), sess_options=options,
                                      providers=["CPUExecutionProvider"])
        self._tokenizer, self._session = tokenizer, session

    def check_ready(self):
        with self._lock:
            self._load()

    def encode(self, texts, *, query=False):
        with self._lock:
            self._load()
            vectors = []
            names = {item.name for item in self._session.get_inputs()}
            for text in texts:
                encoded = self._tokenizer.encode(("query: " if query else "passage: ") + text)
                inputs = {"input_ids": np.array([encoded.ids], dtype=np.int64),
                          "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
                          "token_type_ids": np.array([encoded.type_ids], dtype=np.int64)}
                hidden = self._session.run(None, {k: v for k, v in inputs.items() if k in names})[0]
                mask = inputs["attention_mask"][..., None]
                pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
                norm = np.linalg.norm(pooled, axis=1, keepdims=True)
                if not np.isfinite(pooled).all() or np.any(norm < 1e-8):
                    raise ValueError("invalid E5 embedding")
                vectors.append((pooled / norm)[0])
            return np.asarray(vectors, dtype=np.float32).reshape((-1, 384))
