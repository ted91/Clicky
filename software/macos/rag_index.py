"""Local semantic search over recordings/Obsidian/Notion content --
sentence-transformers embeddings + a local SQLite file, entirely on-device
(text never leaves the machine to generate an embedding), the same
local-first posture as voice_id.py's speaker embeddings. Reuses that
module's already-bundled torch runtime rather than adding a second heavy
ML dependency for a different model.

This exists to replace jarvis.py's old find_context()/_search_local_recordings()/
_search_obsidian_vault() literal substring matching, which missed anything
phrased differently from the source text. Exposed to the LLM as a callable
function (search()), not a hardcoded pre-step -- see jarvis.py's
search_memory() and decide_action's function-calling-style dispatch.

Scale assumption: one person's recordings/notes (hundreds, not millions of
documents) -- plain SQLite + a Python/numpy cosine-similarity scan at query
time is genuinely sufficient here; a dedicated vector-DB library would be
solving a problem this app doesn't have.
"""
import json
import logging
import os
import re
import sqlite3
import threading

import numpy as np

import paths

log = logging.getLogger("rag_index")

_DB_PATH = os.path.join(paths.APP_DATA_DIR, "rag_index.db")
_lock = threading.Lock()

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # small (~80MB), fast, well-established default
CHUNK_WORDS = 200  # ~1-2 paragraphs per chunk -- coarse enough to keep context, fine enough to keep matches focused
DEFAULT_TOP_K = 5
MIN_SCORE = 0.25  # below this a cosine match is noise, not a real hit -- judgment-call default, not measured/tuned

_model = None
_model_lock = threading.Lock()


def _load_model():
    """Lazy singleton -- only downloads/loads the embedding model the first
    time search/indexing is actually used, same pattern as
    voice_id._load_model()."""
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME, cache_folder=os.path.join(paths.APP_DATA_DIR, "rag_model"))
        return _model


def _connect():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,      -- 'recording' | 'obsidian' | 'notion'
            source_id TEXT NOT NULL,   -- content_hash / file path / page id
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            date TEXT,                 -- ISO date, for date_range filtering
            speaker TEXT               -- for speaker filtering, when known
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source, source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_date ON chunks(date)")
    return conn


def _chunk_text(text: str, max_words: int = CHUNK_WORDS) -> list:
    words = (text or "").split()
    if not words:
        return []
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


def _embed(texts: list) -> np.ndarray:
    model = _load_model()
    return np.asarray(model.encode(texts, convert_to_numpy=True, show_progress_bar=False))


def delete_source(source: str, source_id: str):
    """Removes all chunks for one source_id -- call before re-indexing an
    updated recording/note so edits don't leave stale chunks alongside new
    ones, and when a recording is deleted from the dashboard."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM chunks WHERE source = ? AND source_id = ?", (source, source_id))
        conn.commit()
        conn.close()


def index_text(source: str, source_id: str, text: str, date: str = None, speaker: str = None):
    """Chunks + embeds + stores `text` under (source, source_id) -- replaces
    any existing chunks for that source_id first, so this is safe to call
    again on an update (a rename, a re-summarize) without accumulating
    duplicates. No-ops on empty text."""
    chunks = _chunk_text(text)
    if not chunks:
        return
    embeddings = _embed(chunks)
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM chunks WHERE source = ? AND source_id = ?", (source, source_id))
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            conn.execute(
                "INSERT INTO chunks (source, source_id, chunk_index, text, embedding, date, speaker) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, source_id, i, chunk, emb.astype(np.float32).tobytes(), date, speaker),
            )
        conn.commit()
        conn.close()


def add_recording(record: dict):
    """Convenience wrapper for poller.py -- indexes a recording's transcript
    (the fullest available text) under source="recording", keyed by its
    stable content_hash. Speaker names attached per-recording (not per-
    segment) is a simplification -- fine for retrieval/filtering purposes,
    not meant to be exact per-utterance attribution."""
    text = record.get("transcript") or (record.get("summary") or {}).get("summary") or ""
    if not text.strip():
        return
    speakers = ", ".join((record.get("speaker_names") or {}).values()) or None
    index_text("recording", record["content_hash"], text, date=(record.get("created_at") or "")[:10], speaker=speakers)


def search(query: str, top_k: int = DEFAULT_TOP_K, date_start: str = None, date_end: str = None, speaker: str = None) -> list:
    """Semantic search across every indexed chunk. Returns up to top_k
    {"text", "date", "speaker", "source", "score"} dicts above MIN_SCORE,
    best-first. date_start/date_end/speaker are cheap exact pre-filters
    (SQL WHERE, not part of the similarity computation) applied before
    ranking -- precise for "last week"/"with Paul"-style constraints,
    complementing the embedding similarity rather than replacing it."""
    if not query or not query.strip():
        return []
    with _lock:
        conn = _connect()
        sql = "SELECT text, embedding, date, speaker, source FROM chunks WHERE 1=1"
        params = []
        if date_start:
            sql += " AND date >= ?"
            params.append(date_start)
        if date_end:
            sql += " AND date <= ?"
            params.append(date_end)
        if speaker:
            sql += " AND speaker LIKE ?"
            params.append(f"%{speaker}%")
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    if not rows:
        return []

    query_emb = _embed([query])[0]
    query_norm = np.linalg.norm(query_emb)
    if query_norm == 0:
        return []

    scored = []
    for text, emb_blob, date, spk, source in rows:
        emb = np.frombuffer(emb_blob, dtype=np.float32)
        denom = query_norm * np.linalg.norm(emb)
        score = float(np.dot(query_emb, emb) / denom) if denom else 0.0
        if score >= MIN_SCORE:
            scored.append({"text": text, "date": date, "speaker": spk, "source": source, "score": round(score, 3)})
    scored.sort(key=lambda r: -r["score"])
    return scored[:top_k]


def rank_by_similarity(query: str, candidates: list) -> list:
    """Embeds `query` and every string in `candidates` (one batched encode
    call, not one-at-a-time) and returns a same-length list of cosine
    similarity scores, in order. Used by poller.py's pre-meeting prep note
    to rank a small, already-filtered candidate list (past recordings
    involving the same attendees) by semantic relevance to the upcoming
    meeting's title -- replaces a literal title-word-overlap heuristic that
    missed anything phrased differently. Not a search() call: the
    candidates here are already known, not looked up from the index."""
    if not query or not candidates:
        return [0.0] * len(candidates)
    embeddings = _embed([query] + list(candidates))
    query_emb, candidate_embs = embeddings[0], embeddings[1:]
    query_norm = np.linalg.norm(query_emb)
    if query_norm == 0:
        return [0.0] * len(candidates)
    scores = []
    for emb in candidate_embs:
        denom = query_norm * np.linalg.norm(emb)
        scores.append(float(np.dot(query_emb, emb) / denom) if denom else 0.0)
    return scores


def is_enabled() -> bool:
    import settings
    return bool(settings.get_all().get("rag_enabled", True))
