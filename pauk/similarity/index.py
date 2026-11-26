"""Build an immutable dense index over the prepared text of a single corpus."""

import io
import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

import numpy as np

from pauk.storage.atomic import atomic_write_bytes

MODEL = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
DIMENSIONS = 1024
DEFAULT_MAX_TOKENS = 512


def _json(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Document:
    id: str
    text_sha256: str


def _read_corpus(path: Path) -> tuple[dict, list[Document], dict[str, str], int]:
    payload = path.read_bytes()
    corpus = json.loads(payload)
    if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
        raise ValueError("unsupported corpus schema")
    snapshot = corpus.get("snapshot_sha256")
    cleaner = corpus.get("cleaner_version")
    if not isinstance(snapshot, str) or not re.fullmatch("[0-9a-f]{64}", snapshot) or type(cleaner) is not int:
        raise ValueError("invalid corpus provenance")
    rows = corpus.get("repositories")
    if not isinstance(rows, list):
        raise ValueError("corpus requires a repositories array")
    provenance = {"corpus_sha256": sha256(payload).hexdigest(), "snapshot_sha256": snapshot, "cleaner_version": cleaner}
    seen = set()
    documents = []
    texts = {}
    for row in rows:
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or not rid.strip() or rid in seen:
            raise ValueError(f"invalid or duplicate repository ID: {rid!r}")
        seen.add(rid)
        text = row.get("text")
        if not isinstance(text, str):
            raise ValueError(f"missing prepared text for {rid}")
        if not text.strip():
            if not row.get("skip_reason") or row.get("text_sha256") is not None:
                raise ValueError(f"inconsistent skipped document: {rid}")
            continue
        digest = sha256(text.encode()).hexdigest()
        if digest != row.get("text_sha256") or row.get("skip_reason") is not None:
            raise ValueError(f"inconsistent text or digest for {rid}")
        documents.append(Document(rid, digest))
        texts[digest] = text
    documents.sort(key=lambda doc: doc.id)
    return provenance, documents, texts, len(rows) - len(documents)


def _load_encoder(max_tokens: int):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("install the embedding dependencies with 'uv sync --extra similarity'") from exc
    try:
        model = SentenceTransformer(MODEL, revision=REVISION, trust_remote_code=False, local_files_only=True)
    except OSError:
        model = SentenceTransformer(MODEL, revision=REVISION, trust_remote_code=False)
    model.max_seq_length = max_tokens
    return model


def _normalise(batch: np.ndarray, size: int) -> np.ndarray:
    batch = np.asarray(batch)
    if batch.shape != (size, DIMENSIONS) or not np.issubdtype(batch.dtype, np.floating):
        raise ValueError(f"encoder returned {batch.shape} {batch.dtype}, expected {size} x {DIMENSIONS} floats")
    norms = np.linalg.norm(batch.astype(np.float64), axis=1, keepdims=True)
    if not np.isfinite(norms).all() or np.any(norms == 0):
        raise ValueError("encoder returned a zero or non-finite vector")
    return (batch / norms).astype("<f4")


def _cached_vector(path: Path) -> np.ndarray | None:
    """Verify the digest as well as the shape before reusing a batch checkpoint."""
    try:
        entry = path.read_bytes()
    except FileNotFoundError:
        return None
    digest, body = entry[:32], entry[32:]
    if len(body) != 4 * DIMENSIONS or sha256(body).digest() != digest:
        return None
    vector = np.frombuffer(body, dtype="<f4")
    return vector if np.isfinite(vector).all() and np.isclose(np.linalg.norm(vector), 1.0) else None


def _vectors(texts: dict[str, str], directory: Path, batch_size: int, max_tokens: int) -> tuple[dict, int]:
    vectors = {}
    missing = []
    for digest in texts:
        vector = _cached_vector(directory / digest)
        if vector is None:
            missing.append(digest)
        else:
            vectors[digest] = vector
    if missing:
        model = _load_encoder(max_tokens)
        for i in range(0, len(missing), batch_size):
            keys = missing[i:i + batch_size]
            raw = model.encode([texts[k] for k in keys], batch_size=batch_size,
                               convert_to_numpy=True, show_progress_bar=False)
            for key, vector in zip(keys, _normalise(raw, len(keys)), strict=True):
                body = vector.tobytes()
                atomic_write_bytes(directory / key, sha256(body).digest() + body)
                vectors[key] = vector
    return vectors, len(missing)


def _fork_texts(source: Path, documents: list[Document]) -> tuple[list[Document], dict[str, str]]:
    """Construct the fork's name/description/topics/first 4000 README characters."""
    root = source.parent.parent if source.parent.name == "versions" else source.parent
    rows = {row["id"]: row for row in json.loads(source.read_bytes())["repositories"]}
    transformed = []
    texts = {}
    for doc in documents:
        row = rows[doc.id]
        readme = row.get("readme") or {}
        blob = readme.get("blob")
        content = ""
        if blob:
            if not isinstance(blob, str) or not re.fullmatch(r"blobs/[0-9a-f]{64}", blob):
                raise ValueError(f"invalid README blob for {doc.id}")
            body = (root / blob).read_bytes()
            if sha256(body).hexdigest() != blob.removeprefix("blobs/"):
                raise ValueError(f"README blob digest mismatch for {doc.id}")
            content = body.decode("utf-8", errors="replace")[:4000]
        parts = [row.get("name") or "", row.get("description") or "", " ".join(row.get("topics") or []), content]
        text = ". ".join(part.strip() for part in parts if part and part.strip())
        if not text:
            raise ValueError(f"empty fork-style text for {doc.id}")
        digest = sha256(text.encode()).hexdigest()
        transformed.append(Document(doc.id, digest))
        texts[digest] = text
    return transformed, texts


def build_index(corpus: Path, out: Path, *, batch_size: int = 8, max_tokens: int = DEFAULT_MAX_TOKENS,
                text_variant: str = "cleaned") -> dict:
    """Commit a manifest only after all rows are available, in canonical repository ID order."""
    if batch_size < 1 or not 3 <= max_tokens <= 8192:
        raise ValueError("batch-size must be positive and max-tokens must be in [3, 8192]")
    if text_variant not in {"cleaned", "fork"}:
        raise ValueError("unknown text variant")
    source = corpus / "manifest.json" if corpus.is_dir() else corpus
    if (out / "manifest.json").resolve() == source.resolve() or out.resolve() == source.parent.resolve():
        raise ValueError("index output must be separate from the corpus")
    provenance, documents, texts, skipped = _read_corpus(source)
    if text_variant == "fork":
        documents, texts = _fork_texts(source, documents)
    try:
        runtime = {name: package_version(name) for name in ("sentence-transformers", "transformers", "torch")}
    except PackageNotFoundError:
        runtime = {"sentence-transformers": "uninstalled", "transformers": "uninstalled", "torch": "uninstalled"}
    encoder = {"model": MODEL, "revision": REVISION, "max_tokens": max_tokens, "runtime": runtime,
               "text_variant": text_variant,
               "dimensions": DIMENSIONS, "dtype": "float32", "normalization": "l2"}
    cache_key = sha256(_json({"encoder": encoder, "cleaner_version": provenance["cleaner_version"]})).hexdigest()
    vectors, encoded = _vectors(texts, out / "cache" / cache_key, batch_size, max_tokens)
    matrix = np.empty((len(documents), DIMENSIONS), dtype="<f4")
    for i, doc in enumerate(documents):
        matrix[i] = vectors[doc.text_sha256]
    stream = io.BytesIO()
    np.save(stream, matrix, allow_pickle=False)
    data = stream.getvalue()
    digest = sha256(data).hexdigest()
    matrix_path = f"matrices/{digest}.npy"
    atomic_write_bytes(out / matrix_path, data)
    manifest = {"schema_version": 1, **provenance, "encoder": encoder, "matrix": matrix_path,
                "matrix_sha256": digest, "rows": [asdict(doc) for doc in documents], "skipped": skipped}
    payload = _json(manifest)
    version = sha256(payload).hexdigest()
    atomic_write_bytes(out / "versions" / f"{version}.json", payload)
    atomic_write_bytes(out / "manifest.json", payload)
    return {"version": version, "rows": len(documents), "skipped": skipped, "encoded_texts": encoded,
            "cached_texts": len(texts) - encoded}
