#!/usr/bin/env python3
"""
CPU-Only Embedding Service for OpenClaw Memory Search
Pinned to CPU cores 1,2,3 — zero GPU involvement
Serves embeddings via HTTP for ChromaDB ingestion + SQLite memory search
"""
import os
import sys

# Use GPU if HIP_VISIBLE_DEVICES is set via systemd env, else CPU
if not os.environ.get("HIP_VISIBLE_DEVICES"):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HIP_VISIBLE_DEVICES"] = ""
    os.environ["ROCR_VISIBLE_DEVICES"] = ""
    os.sched_setaffinity(0, {1, 2, 3})

import json
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from sentence_transformers import SentenceTransformer

# Use a small CPU-friendly model
MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
MAX_SEQ_LENGTH = 2048  # nomic-embed-text-v1.5 max context
BIND_HOST = "0.0.0.0"
BIND_PORT = 9500

_device = "cuda" if os.environ.get("HIP_VISIBLE_DEVICES") else "cpu"
print(f"[EMBED] Loading {MODEL_NAME} on {_device}...")
model = SentenceTransformer(MODEL_NAME, device=_device, trust_remote_code=True)
model.max_seq_length = MAX_SEQ_LENGTH
_tokenizer = model.tokenizer


def truncate_texts(texts: list[str]) -> list[str]:
    """Truncate input texts to MAX_SEQ_LENGTH tokens to prevent OOM/errors."""
    truncated = []
    for text in texts:
        token_ids = _tokenizer.encode(text, add_special_tokens=False)
        if len(token_ids) > MAX_SEQ_LENGTH - 2:  # reserve 2 for [CLS]/[SEP]
            token_ids = token_ids[:MAX_SEQ_LENGTH - 2]
            text = _tokenizer.decode(token_ids, skip_special_tokens=True)
        truncated.append(text)
    return truncated


print(f"[EMBED] Model loaded (max_seq_length={MAX_SEQ_LENGTH}). Serving on {BIND_HOST}:{BIND_PORT}")


class EmbeddingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "model": MODEL_NAME})
        elif self.path == "/v1/models":
            self._respond(200, {"data": [{"id": MODEL_NAME, "object": "model"}]})
        else:
            self._respond(404, {"error": "unknown endpoint"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if self.path == "/v1/embeddings":
            # OpenAI-compatible embedding endpoint for Agent Zero / LiteLLM
            input_data = body.get("input", [])
            if isinstance(input_data, str):
                input_data = [input_data]
            input_data = truncate_texts(input_data)
            embeddings = model.encode(input_data, normalize_embeddings=True).tolist()
            resp = {
                "object": "list",
                "data": [{"object": "embedding", "embedding": emb, "index": i} for i, emb in enumerate(embeddings)],
                "model": body.get("model", MODEL_NAME),
                "usage": {"prompt_tokens": 0, "total_tokens": 0}
            }
            self._respond(200, resp)

        elif self.path == "/embed":
            texts = body.get("texts", [])
            if isinstance(texts, str):
                texts = [texts]
            texts = truncate_texts(texts)
            embeddings = model.encode(texts, normalize_embeddings=True).tolist()
            self._respond(200, {"embeddings": embeddings})

        elif self.path == "/search":
            query = body.get("query", "")
            db_path = body.get("db_path", "")
            top_k = body.get("top_k", 5)
            if not query or not db_path:
                self._respond(400, {"error": "query and db_path required"})
                return
            results = self._search_sqlite(query, db_path, top_k)
            self._respond(200, {"results": results})

        else:
            self._respond(404, {"error": "unknown endpoint"})

    def _search_sqlite(self, query, db_path, top_k):
        """Search SQLite memory by embedding similarity — CPU only"""
        try:
            query_vec = model.encode(truncate_texts([query]), normalize_embeddings=True)[0]
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT id, content, embedding FROM memory_vectors")
            results = []
            for row in cursor:
                stored_vec = json.loads(row[2])
                sim = sum(a * b for a, b in zip(query_vec, stored_vec))
                results.append({"id": row[0], "content": row[1], "score": float(sim)})
            conn.close()
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:top_k]
        except Exception as e:
            return [{"error": str(e)}]

    def _respond(self, code, data):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        except BrokenPipeError:
            pass  # Client disconnected, ignore
        except ConnectionResetError:
            pass  # Client reset, ignore

    def log_message(self, format, *args):
        pass  # suppress per-request logs


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server — prevents single-request blocking."""
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadedHTTPServer((BIND_HOST, BIND_PORT), EmbeddingHandler)
    print(f"[EMBED] Ready on http://{BIND_HOST}:{BIND_PORT} (threaded)")
    server.serve_forever()
