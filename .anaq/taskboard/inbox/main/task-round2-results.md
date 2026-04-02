# Task Round 2 Results — Main Agent
## Date: 2026-04-02 16:30 EDT
## Session: P0 + P1 Reliability + P2 Code Bugs

---

## P0 CRITICAL — COMPLETED

| ID | Status | Fix | Commit |
|----|--------|-----|--------|
| P0-001 | BLOCKED on FA-006 | FAISS migration only 4.4% complete. faiss_cleanup.py exists at ~/.anaq/grading/faiss_cleanup.py. Cannot clean orphans until migration finishes. Fareez must restart migration. | — |
| P0-002 | ESCALATED | MCE Bank 5 count = 3 (recurring). Needs `isolcpus=2` kernel param if continues. Fareez should monitor `dmesg | grep mce` | — |
| P0-003 | DONE | Created /etc/sysctl.d/99-inference-memory.conf: vm.overcommit_memory=1 (was 0 with 41GB cap), vm.swappiness=10 (was 60). Applied live. | 650cde5 |
| P0-004 | DONE | force_fans_v2.py line 37: "high" → "profile_peak". Preserves MCLK OC. Also fixed 2 bare except blocks (P2-005). | 650cde5 |

---

## P1 RELIABILITY — COMPLETED

| ID | Status | Fix | Commit |
|----|--------|-----|--------|
| P1-050 | DONE | OOMScoreAdjust on 7 services: -500 (bridge, memory, embedding, heartbeat), +500 (agent-zero), -200 (telegram) | e7db18c |
| P1-051 | DONE | MemoryMax on 7 services: A0=8G, bridges=2-4G, TG=1G, heartbeat=512M | e7db18c |
| P1-052 | DONE | StartLimitBurst=5 + IntervalSec=300 on 6 services (agent-zero, claude-code-bridge, memory-bridge, embedding, telegram-bridge, heartbeat) | e7db18c |
| P1-053 | DONE | grading-proxy: After=memory-bridge.service added, Wants=memory-bridge.service added | e7db18c |
| P1-054 | ALREADY FIXED | Embedding service MAX_SEQ_LENGTH=2048 with truncation already implemented (commit b2fea44). Token overflow at 4772 cannot occur. | — |
| P1-055 | DONE | Heartbeat was dead from clean SIGTERM. Restarted successfully. Now reports "All systems nominal". PATH fixed to include conda agent0 bin (also P2-014). | e7db18c |
| P1-056 | DONE | repair_agent.py: RuntimeError on missing TG_SYSTEM_TOKEN replaced with warning log. send_telegram() returns False gracefully. | 8e14b34 |
| P1-057 | DONE | Root cause: memory-bridge crashed on startup from duplicate content_hash rows in SQLite preventing UNIQUE index creation. Fixed with dedup-before-index logic. Bridge now running. Sync daemon will pick up on next 15min cycle. | af03a53 |

---

## P2 CODE BUGS — COMPLETED

| ID | Status | Fix | Commit |
|----|--------|-----|--------|
| P2-001 | DONE | FAISS pending_sync status now logged at INFO level with doc_id and index name | eed5bd1 |
| P2-002 | DONE | memory_bridge dedup: auto-deduplicate on startup before creating unique index | af03a53 |
| P2-003 | DONE | httpx AsyncClient _get_client() now uses asyncio.Lock to prevent duplicate client leak | af03a53 |
| P2-004 | DONE | _write_conditioning() wrapped in asyncio.to_thread() at both call sites in grading_proxy | 66197a2 |
| P2-005 | DONE | 2 bare except blocks in force_fans_v2.py replaced with (OSError, ValueError). Searched .anaq/bridge/, hardware_control/, vllm_workspace/services/ — no other bare excepts found. | 650cde5 |
| P2-006 | DONE | TOCTOU narrowed: _check_duplicate() called before FAISS embed in ingest(). Remaining tiny race logged and handled by nightly cleanup. | d06c22f |
| P2-007 | DONE | embedding_service _search_sqlite: conn.close() moved to finally block. Corrupted embedding rows skipped with try/except. | 87f4ff5 |
| P2-008 | WONTFIX | No sqlite3.connect() exists in grading_proxy.py. Original ticket may have meant memory_bridge.py, which uses per-call connections by design (lightweight, WAL mode). | — |
| P2-009 | DONE | Duplicate SSE [DONE]: reordered if-check before unconditional append so sentinel emits only once | 2ca5c71 |
| P2-010 | BLOCKED | Telegram returns HTTP 400 Bad Request. WhatsApp returns Connection Refused (OpenClaw down). Both transports broken — needs TG token/chat_id verification (external config issue). | — |
| P2-011 | DONE | repair_agent: resolve_model_alias() return assigned to 'is_model' (was confusingly 'model') | a074d70 |
| P2-012 | BLOCKED | vllm-omni talker GPF in libamdhip64.so — stale from pre-crash. Requires HIP runtime debugging on fresh model load. Cannot fix without reproducer. | — |
| P2-013 | DONE | A0 BehaviourPrompt.execute: mutable default list=[] → None with lazy init | 484cba9 (agent-zero repo) |
| P2-014 | DONE | Heartbeat PATH now includes conda agent0 bin (fixed in P1-055 service update) | e7db18c |
| P2-015 | DONE | asyncio.Lock at module level → lazy init via _get_retry_lock(). 4 call sites updated. | 2ca5c71 |
| P2-016 | DONE | ANAQ_FEEDBACK.md: replaced read+write with tempfile+os.replace atomic write | e5db0d2 |
| P2-017 | DONE | embedding_service do_POST: json.loads wrapped in try/except, returns 400 on malformed body | 87f4ff5 |
| P2-018 | DONE | turn_sync.py: conn.close() moved to finally block from inside try | bdd7670 |
| P2-019 | BLOCKED on P0-001 | 44 orphaned FAISS vectors need faiss_cleanup.py post-migration (FA-006) | — |
| P2-020 | DONE | asyncio.create_task for _log_score now tracked in _background_tasks set with done callback | 2ca5c71 |

---

## BONUS FIXES (encountered during execution)

| ID | Fix | Commit |
|----|-----|--------|
| P3-005 | vm.swappiness=10 (was 60) — applied with P0-003 sysctl config | 650cde5 |
| P3-021 | heartbeat ValueError on NFS df "-" output — added try/except | 4387673 |
| P2-014 | heartbeat PATH fixed (done with P1-055) | e7db18c |

---

## SYSTEM STATE AFTER FIXES

### Services
- memory-bridge: RUNNING (was crashed)
- anaq-heartbeat: RUNNING (was dead 12+ hours)  
- embedding-service: RUNNING
- claude-code-bridge: RUNNING
- All services now have OOMScoreAdjust + MemoryMax + StartLimitBurst

### Kernel
- vm.overcommit_memory = 1 (persistent via /etc/sysctl.d/)
- vm.swappiness = 10 (persistent)
- MCE Bank 5 count = 3 (needs monitoring)

### Total: 24 items resolved, 4 items blocked on external deps
