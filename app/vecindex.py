"""File-based semantic vector index over everything the service produces.

A single ``sqlite-vec`` database at ``data/index/vectors.db`` (files-first;
Postgres stays optional and untouched). Documents — scrape artifacts, crawl
pages, research reports, and offline corpus RAG records (indexed by the corpus
pipeline) — are chunked, embedded via ``app.embeddings``, and stored with a
``kind`` tag so search can filter.

Single-process discipline, mirroring ``app/dedup.py``: one module-global
connection guarded by a ``threading.Lock``. sqlite-vec write serialization
matches the deploy's single uvicorn worker.

Everything is best-effort. If the ``sqlite-vec`` extension fails to load (the
documented top risk), ``_connect()`` disables the index and every operation
becomes a no-op — a scrape/crawl/research response is never affected.

Dimension safety: the index stamps the embedding model + dimension on first
write. A later upsert whose vector dimension disagrees is refused (skipped, not
crashed); ``scripts/build_embeddings.py --reindex`` rebuilds from scratch.
"""
import hashlib
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from app import embeddings
from app.storage import DATA_DIR

logger = logging.getLogger("vecindex")

INDEX_DIR = os.path.join(DATA_DIR, "index")
DB_PATH = os.path.join(INDEX_DIR, "vectors.db")

# Chunking defaults (character-based approximation of a ~200-1200 token window;
# generous for sentence-transformer context limits). S2 adds a structure-aware
# chunker in the corpus pipeline; the service can't import it, so this paragraph
# window is the service-side fallback the epic calls for.
MAX_CHARS = int(os.environ.get("EMBEDDINGS_CHUNK_CHARS", "1600"))
CHUNK_OVERLAP = 160
MAX_CHUNKS = 200
SNIPPET_CHARS = 320

_lock = threading.Lock()
_conn = None                       # sqlite3.Connection once loaded
_sqlite_vec = None                 # the sqlite_vec module (for serialize_float32)
_available: Optional[bool] = None  # None=untried, True=loaded, False=unavailable
_fts_available: Optional[bool] = None

FILTER_META_KEYS = {
    "namespace": "namespace",
    "bucket": "license_bucket",
    "tier": "quality_tier",
    "framework": "framework",
}


def _connect():
    """Return the shared connection, loading sqlite-vec once. Returns None (and
    disables the index for the process) if the extension can't be loaded."""
    global _conn, _sqlite_vec, _available
    if _available is False:
        return None
    if _conn is not None:
        return _conn
    try:
        import sqlite3

        import sqlite_vec  # type: ignore

        os.makedirs(INDEX_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " kind TEXT NOT NULL, ref TEXT NOT NULL, url TEXT,"
            " chunk_index INTEGER NOT NULL, content_hash TEXT NOT NULL,"
            " snippet TEXT, meta TEXT)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        if "text" not in columns:
            conn.execute("ALTER TABLE chunks ADD COLUMN text TEXT")
        # Existing indexes only retained snippets. Preserve keyword search on
        # upgrade; newly indexed rows store the complete chunk below.
        conn.execute("UPDATE chunks SET text=snippet WHERE text IS NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_ref ON chunks(kind, ref)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash)")
        _setup_fts(conn)
        conn.commit()
        _conn = conn
        _sqlite_vec = sqlite_vec
        _available = True
        logger.info("semantic index ready at %s", DB_PATH)
    except Exception as e:
        logger.warning(
            "sqlite-vec unavailable — semantic index disabled for this process: %s", e)
        _available = False
        _conn = None
    return _conn


def _setup_fts(conn) -> bool:
    """Create/backfill the optional keyword mirror without affecting vectors."""
    global _fts_available
    try:
        created = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone() is None
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
            "USING fts5(text, content='chunks', content_rowid='id')")
        if created:
            conn.execute(
                "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        _fts_available = True
    except Exception as e:
        logger.warning("SQLite FTS5 unavailable — using token LIKE search: %s", e)
        _fts_available = False
    return bool(_fts_available)


def available() -> bool:
    """True if the sqlite-vec index can be used (extension loaded)."""
    with _lock:
        return _connect() is not None


def _get_meta(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM vec_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def stored_dim(conn=None) -> Optional[int]:
    """The dimension the index was stamped with, or None if never written."""
    own = conn is None
    if own:
        conn = _connect()
        if conn is None:
            return None
    v = _get_meta(conn, "dim")
    return int(v) if v else None


def _ensure_vec_table(conn, dim: int, model: str) -> bool:
    """Create the vec0 table on first write (stamping model+dim). Returns False
    if an existing index has a different dimension (caller must skip)."""
    existing = stored_dim(conn)
    if existing is None:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{dim}])")
        conn.execute("INSERT OR REPLACE INTO vec_meta(key,value) VALUES('dim',?)", (str(dim),))
        conn.execute("INSERT OR REPLACE INTO vec_meta(key,value) VALUES('model',?)", (model,))
        conn.commit()
        return True
    return existing == dim


def chunk_text(text: str, *, max_chars: int = MAX_CHARS,
               overlap: int = CHUNK_OVERLAP, max_chunks: int = MAX_CHUNKS) -> List[str]:
    """Paragraph-window chunker: pack blank-line-delimited paragraphs into
    windows up to ``max_chars``, hard-splitting any oversized paragraph with a
    small overlap. Pure function; the service-side fallback for S1."""
    text = (text or "").strip()
    if not text:
        return []
    paras = re.split(r"\n\s*\n", text)
    chunks: List[str] = []
    cur = ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            step = max(1, max_chars - overlap)
            for i in range(0, len(p), step):
                chunks.append(p[i:i + max_chars])
                if len(chunks) >= max_chunks:
                    return chunks[:max_chunks]
            continue
        if cur and len(cur) + len(p) + 2 > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
        if len(chunks) >= max_chunks:
            return chunks[:max_chunks]
    if cur:
        chunks.append(cur)
    return chunks[:max_chunks]


def _snippet(text: str) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s[:SNIPPET_CHARS]


def content_hash_indexed(content_hash: str) -> bool:
    """True if any chunk with this content hash is already indexed. Used by the
    backfill script for content-hash skip (resumable reindex)."""
    with _lock:
        conn = _connect()
        if conn is None:
            return False
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE content_hash=? LIMIT 1", (content_hash,)).fetchone()
        return row is not None


def ref_indexed(kind: str, ref: str) -> bool:
    """True if a (kind, ref) document already has chunks indexed."""
    with _lock:
        conn = _connect()
        if conn is None:
            return False
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE kind=? AND ref=? LIMIT 1", (kind, ref)).fetchone()
        return row is not None


def upsert(kind: str, ref: str, url: Optional[str], texts: List[str],
           vectors: List[List[float]], *, meta: Optional[Dict[str, Any]] = None,
           model: str = "") -> int:
    """Replace all chunks for (kind, ref) with the given texts+vectors. Sync,
    guarded by the module lock. Returns the number of chunks written (0 if the
    index is unavailable or the dimension disagrees)."""
    import json
    if not texts or not vectors or len(texts) != len(vectors):
        return 0
    with _lock:
        conn = _connect()
        if conn is None:
            return 0
        dim = len(vectors[0])
        if not _ensure_vec_table(conn, dim, model or embeddings.model()):
            logger.warning(
                "embedding dim %d != index dim %s for %s/%s — skipping "
                "(run build_embeddings.py --reindex to rebuild)",
                dim, stored_dim(conn), kind, ref)
            return 0
        # Replace any prior chunks for this document (idempotent reindex).
        try:
            conn.execute("BEGIN")
            old = conn.execute(
                "SELECT id,coalesce(text,snippet,'') FROM chunks WHERE kind=? AND ref=?",
                (kind, ref)).fetchall()
            for rid, old_text in old:
                conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (rid,))
                if _fts_available:
                    conn.execute(
                        "INSERT INTO chunks_fts(chunks_fts,rowid,text) "
                        "VALUES('delete',?,?)", (rid, old_text))
            conn.execute("DELETE FROM chunks WHERE kind=? AND ref=?", (kind, ref))
            n = 0
            for i, (t, v) in enumerate(zip(texts, vectors)):
                if len(v) != dim:
                    continue
                ch = hashlib.sha256(t.encode("utf-8")).hexdigest()
                cur = conn.execute(
                    "INSERT INTO chunks(kind,ref,url,chunk_index,content_hash,snippet,meta,text)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (kind, ref, url, i, ch, _snippet(t),
                     json.dumps(meta or {}, ensure_ascii=False), t))
                conn.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES(?,?)",
                    (cur.lastrowid, _sqlite_vec.serialize_float32(v)))
                if _fts_available:
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid,text) VALUES(?,?)",
                        (cur.lastrowid, t))
                n += 1
            conn.commit()
            return n
        except Exception as e:
            conn.rollback()
            logger.warning("index upsert failed for %s/%s: %s", kind, ref, e)
            return 0


async def index_document(kind: str, ref: str, url: Optional[str], text: str,
                         meta: Optional[Dict[str, Any]] = None) -> int:
    """Best-effort chunk → embed → upsert for one document. Returns chunks
    indexed (0 if the backend is unset, the index is unavailable, or anything
    fails). Never raises — safe to call from a scrape/crawl/research hook."""
    try:
        if not embeddings.configured():
            return 0
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vectors = await embeddings.embed(chunks)
        if not vectors:
            return 0
        return upsert(kind, ref, url, chunks, vectors,
                      meta=meta, model=embeddings.model())
    except Exception as e:
        logger.warning("index_document failed for %s/%s: %s", kind, ref, e)
        return 0


def _filter_sql(alias: str, kind: Optional[str],
                filters: Optional[Dict[str, str]]) -> Tuple[str, List[Any]]:
    """Build exact-match predicates shared by vector and keyword search."""
    prefix = f"{alias}." if alias else ""
    filters = {key: str(value).strip() for key, value in (filters or {}).items()
               if key in FILTER_META_KEYS and str(value).strip()}
    clauses: List[str] = []
    params: List[Any] = []
    if kind:
        clauses.append(f"{prefix}kind=?")
        params.append(kind)
    elif filters:
        # Search facets describe corpus records. Do not let missing metadata on
        # scrape/crawl/research rows accidentally match values such as untiered.
        clauses.append(f"{prefix}kind=?")
        params.append("corpus")
    for name, value in filters.items():
        column = FILTER_META_KEYS[name]
        if name == "tier" and value == "untiered":
            clauses.append(
                f"coalesce(json_extract({prefix}meta, '$.{column}'),'')=''"
            )
        else:
            clauses.append(f"json_extract({prefix}meta, '$.{column}')=?")
            params.append(value)
    return " AND ".join(clauses), params


def _matches_filters(row_kind: str, meta: Dict[str, Any],
                     kind: Optional[str],
                     filters: Optional[Dict[str, str]]) -> bool:
    filters = {key: str(value).strip() for key, value in (filters or {}).items()
               if key in FILTER_META_KEYS and str(value).strip()}
    if kind and row_kind != kind:
        return False
    if filters and row_kind != "corpus":
        return False
    for name, value in filters.items():
        actual = str(meta.get(FILTER_META_KEYS[name]) or "")
        if name == "tier" and value == "untiered":
            if actual:
                return False
        elif actual != value:
            return False
    return True


def search(query_vec: List[float], *, kind: Optional[str] = None,
           k: int = 10,
           filters: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """K-nearest chunks to ``query_vec``, optionally filtered to one ``kind``.
    Returns hits with kind, ref, url, chunkIndex, snippet, score, meta. Empty
    list if the index is unavailable/empty or the query dimension disagrees."""
    import json
    with _lock:
        conn = _connect()
        if conn is None:
            return []
        dim = stored_dim(conn)
        if dim is None or len(query_vec) != dim:
            return []
        q = _sqlite_vec.serialize_float32(query_vec)
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not total:
            return []
        filtered = bool(kind or filters)
        candidate_k = min(total, max(k * 10, 50)) if filtered else min(total, k)
        while True:
            try:
                knn = conn.execute(
                    "SELECT rowid, distance FROM vec_chunks"
                    " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (q, candidate_k)).fetchall()
            except Exception as e:
                logger.warning("semantic search failed: %s", e)
                return []
            out: List[Dict[str, Any]] = []
            for rid, dist in knn:
                row = conn.execute(
                    "SELECT kind, ref, url, chunk_index, snippet, meta "
                    "FROM chunks WHERE id=?", (rid,)).fetchone()
                if not row:
                    continue
                try:
                    meta = json.loads(row[5]) if row[5] else {}
                except Exception:
                    meta = {}
                if not _matches_filters(row[0], meta, kind, filters):
                    continue
                out.append({
                    "kind": row[0], "ref": row[1], "url": row[2],
                    "chunkIndex": row[3], "snippet": row[4],
                    "distance": dist, "score": 1.0 / (1.0 + dist),
                    "meta": meta,
                })
                if len(out) >= k:
                    return out
            if candidate_k >= total:
                return out
            candidate_k = min(total, candidate_k * 2)


def _row_hit(row, score: float) -> Dict[str, Any]:
    import json
    try:
        meta = json.loads(row[6]) if row[6] else {}
    except Exception:
        meta = {}
    return {
        "kind": row[1], "ref": row[2], "url": row[3],
        "chunkIndex": row[4], "snippet": row[5],
        "score": score, "meta": meta,
    }


def _query_tokens(query: str) -> List[str]:
    """Literal word tokens only; callers never get to supply MATCH syntax."""
    return re.findall(r"[^\W_]+(?:_[^\W_]+)*", query or "", flags=re.UNICODE)


def keyword_search(query: str, *, kind: Optional[str] = None, k: int = 10,
                   filters: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Search full chunk text with FTS5, or an AND-token LIKE fallback."""
    tokens = _query_tokens(query)
    if not tokens or k <= 0:
        return []
    with _lock:
        conn = _connect()
        if conn is None:
            return []
        params: List[Any] = []
        predicate, predicate_params = _filter_sql("c", kind, filters)
        if _fts_available:
            # Each token is quoted after tokenization, so punctuation cannot
            # become FTS operators or malformed MATCH input.
            match = " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)
            sql = (
                "SELECT c.id,c.kind,c.ref,c.url,c.chunk_index,c.snippet,c.meta,bm25(chunks_fts) "
                "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                "WHERE chunks_fts MATCH ?")
            params.append(match)
            if predicate:
                sql += " AND " + predicate
                params.extend(predicate_params)
            sql += " ORDER BY bm25(chunks_fts),c.kind,c.ref,c.chunk_index LIMIT ?"
        else:
            clauses = []
            for token in tokens:
                clauses.append("lower(coalesce(c.text,c.snippet,'')) LIKE ? ESCAPE '\\'")
                escaped = token.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.append(f"%{escaped}%")
            sql = (
                "SELECT c.id,c.kind,c.ref,c.url,c.chunk_index,c.snippet,c.meta,0.0 "
                "FROM chunks c WHERE " + " AND ".join(clauses))
            if predicate:
                sql += " AND " + predicate
                params.extend(predicate_params)
            sql += " ORDER BY c.kind,c.ref,c.chunk_index LIMIT ?"
        params.append(k)
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.warning("keyword search failed: %s", e)
            return []
        return [_row_hit(row, 1.0 / rank) for rank, row in enumerate(rows, 1)]


def chunks_for_refs(refs: List[str], *, kind: Optional[str] = None,
                    k: int = 100) -> List[Dict[str, Any]]:
    """Resolve DB artifact refs to stable file-index chunk identities."""
    refs = list(dict.fromkeys(ref for ref in refs if ref))
    if not refs or k <= 0:
        return []
    with _lock:
        conn = _connect()
        if conn is None:
            return []
        placeholders = ",".join("?" for _ in refs)
        params: List[Any] = list(refs)
        sql = (
            "SELECT id,kind,ref,url,chunk_index,snippet,meta,0.0 FROM chunks "
            f"WHERE ref IN ({placeholders})")
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY kind,ref,chunk_index LIMIT ?"
        params.append(k)
        try:
            return [_row_hit(row, 0.0) for row in conn.execute(sql, params).fetchall()]
        except Exception:
            return []


def stats() -> Dict[str, Any]:
    """Index summary: total chunks, counts by kind, model+dim. Never raises."""
    with _lock:
        conn = _connect()
        if conn is None:
            return {"available": False, "total": 0, "byKind": {}, "model": None, "dim": None}
        try:
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            by_kind = dict(conn.execute(
                "SELECT kind, COUNT(*) FROM chunks GROUP BY kind").fetchall())
            return {
                "available": True, "total": total, "byKind": by_kind,
                "model": _get_meta(conn, "model"), "dim": stored_dim(conn),
            }
        except Exception:
            return {"available": True, "total": 0, "byKind": {}, "model": None, "dim": None}


def identity_inventory(filters: Optional[Dict[str, str]] = None) -> set:
    """Stable document aliases currently present in the file-based index."""
    import json
    from app.normalize import normalize_url

    identities = set()
    with _lock:
        conn = _connect()
        if conn is None:
            return identities
        try:
            rows = conn.execute(
                "SELECT DISTINCT kind,ref,url,meta FROM chunks").fetchall()
        except Exception:
            return identities
    for kind, ref, url, raw_meta in rows:
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except Exception:
            meta = {}
        filters = filters or {}
        requested_kind = filters.get("kind")
        metadata_filters = {
            key: value for key, value in filters.items() if key != "kind"}
        if not _matches_filters(kind, meta, requested_kind, metadata_filters):
            continue
        identities.add(f"{kind}:ref:{ref}")
        if url:
            identities.add(f"{kind}:url:{normalize_url(url)}")
        parent_hash = meta.get("parent_hash")
        if parent_hash:
            identities.add(f"{kind}:hash:{parent_hash}")
    return identities


def _reset_for_tests() -> None:
    """Drop the cached connection so a test can point DB_PATH at a tmp dir."""
    global _conn, _available, _sqlite_vec, _fts_available
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _sqlite_vec = None
        _available = None
        _fts_available = None
