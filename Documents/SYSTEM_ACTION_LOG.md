# SYSTEM ACTION LOG
### Single Source of Truth — All Outstanding Fixes, Bugs, Errors, and Actions
### Maintained by: Tafakkur + Fareez | Created: 2026-04-02
### Last updated: 2026-04-02 16:35 EDT — Main round 2: P0-003/004 + P1-050→057 + P2-001→020 executed. 27 items AGENT-DONE.

---

## PROTOCOL

1. **Every** outstanding item lives here — one document, one truth
2. Agents dispatch from `OPEN` items. Agents initial + grade against this list.
3. Agent completes → status changes to `AGENT-DONE [agent] [date]`
4. Tafakkur or Fareez reviews → `VERIFIED` or reopened as `OPEN`
5. Swarm audits ADD new items and UPDATE existing — never delete
6. `VERIFIED` items move to archive at bottom

### Status: `OPEN` | `ASSIGNED [agent]` | `AGENT-DONE [agent] [date]` | `VERIFIED [who] [date]` | `FAREEZ-ACTION` | `WONTFIX` | `BLOCKED`
### Priority: `P0` critical/down | `P1` broken/vulnerable | `P2` degraded/debt | `P3` cleanup/nice-to-have
### Agents: `main` (Opus, systems) | `valkyrie` (Opus, security) | `nimah` (Opus, business) | `unicorn` (Qwen, read-only eval)

---
---

# CRITICAL (P0) — System Down, Data Loss, Immediate Action

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P0-001 | Harrier FAISS migration killed at 4.4% (281/6314) | OOM 07:09 | BLOCKED [FA-006] | — | Both llama-server processes killed. 44 orphaned FAISS vectors from partial writes. Run `~/.anaq/grading/faiss_cleanup.py` after migration completes. |
| P0-002 | MCE CPU 2 Bank 5 — L2 cache uncorrectable error | dmesg 22:06 | FAREEZ-ACTION | — | **RECURRING: count=3.** Core 2 needs isolation via `isolcpus=2` kernel param. Monitor: `grep -c "mce.*Bank 5" /var/log/kern.log` |
| P0-003 | CommitLimit=41GB causes ENOMEM under inference load | Swarm-infra | AGENT-DONE main 2026-04-02 | main | Fixed: /etc/sysctl.d/99-inference-memory.conf vm.overcommit_memory=1, vm.swappiness=10. Applied live. Commit 650cde5. |
| P0-004 | force_fans_v2.py writes "high" to DPM — CLOBBERS MCLK OC | Swarm-code NF-014 | AGENT-DONE main 2026-04-02 | main | Fixed: changed "high" → "profile_peak". Also fixed 2 bare except blocks. Commit 650cde5. |

---
---

# P1 — SECURITY (Vulnerabilities, Exposure, Credentials)

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P1-001 | Claude Bridge passes full os.environ to CLI subprocess | Unicorn 010, Swarm-code | OPEN | valkyrie | `env={**os.environ, ...}` in claude_code_bridge.py exposes API keys, tokens, DB creds to subprocess. Filter to only ANTHROPIC_API_KEY + PATH + HOME. |
| P1-002 | SSH PasswordAuth ACTIVE for Tailscale subnet | Swarm-sec SEC-001 | OPEN | valkyrie | **UPGRADED from "unverified".** Live `sshd -T` with Match block evaluation confirms password auth IS enabled. Not just a config conflict — actively exploitable over Tailscale. |
| P1-003 | SSH tunneling unrestricted — AllowTcpForwarding + X11 | Swarm-sec SEC-002/003 | OPEN | valkyrie | AllowTcpForwarding=yes, PermitOpen=any, X11Forwarding=yes. Any SSH user can tunnel arbitrary traffic. |
| P1-004 | SSH has no AllowUsers/AllowGroups restriction | Swarm-sec SEC-004 | OPEN | valkyrie | Any system user can SSH. Restrict to `AllowUsers fareez541`. |
| P1-005 | fail2ban not installed | Swarm-sec SEC-005 | OPEN | valkyrie | No brute-force protection on SSH. Install + configure for sshd jail. |
| P1-006 | LINE credentials plaintext in openclaw.json | Audit, Swarm-sec | OPEN | valkyrie | channelAccessToken + channelSecret at lines ~457-458. Move to env/secrets file. |
| P1-007 | OpenClaw gateway accepts placeholder API key "no-key" | Swarm-sec SEC-006 | OPEN | valkyrie | Gateway validates API key but accepts literal "no-key". Reject empty/placeholder values. |
| P1-008 | OpenClaw gateway UI has zero device auth | Swarm-agents OC-003 | OPEN | valkyrie | `dangerouslyDisableDeviceAuth: true` in config. Port 18789 management UI open to anyone on network. |
| P1-009 | Agent Zero UI bound to 0.0.0.0:5000 | Unicorn 021 | OPEN | valkyrie | Exposes management UI to entire network. Should be localhost or behind auth. |
| P1-010 | Browser agent sandbox disabled | Unicorn 026 | OPEN | valkyrie | `chromium_sandbox=False` + `disable_security=True`. Vulnerable to malicious web content. |
| P1-011 | UFW SSH-LAN rule targets Docker bridge, not physical LAN | Swarm-sec SEC-007 | OPEN | valkyrie | UFW rule applies to wrong interface. Rework to target actual LAN subnet. |
| P1-012 | Port 8888 identified as nginx | Swarm-sec (update P1-003 old) | OPEN | valkyrie | Was "unidentified". Now confirmed nginx. Evaluate if needed; if not, disable. |
| P1-013 | vault_recovery.key stored in ~/.ssh | Swarm-sec SEC-008 | OPEN | valkyrie | Recovery key mixed with SSH keys. Move to separate encrypted location. |
| P1-014 | Sudoers file for non-existent user "gemini_supe" | Swarm-sec SEC-009 | OPEN | valkyrie | `/etc/sudoers.d/gemini_supe` — stale/orphaned. Remove. |
| P1-015 | Web fetch tools vulnerable to SSRF + prompt injection | Unicorn 026 | OPEN | — | External content returned to LLM context. Primary injection vector. |
| P1-016 | Tailscale DNS — can't reach configured servers | Audit, Swarm-sec | OPEN | valkyrie | Confirmed active. Diagnose with `tailscale netcheck`. |
| P1-017 | 6 services binding 0.0.0.0 that should be localhost | Swarm-svc SVC-006, Swarm-infra | OPEN | valkyrie | Ports 5000, 5500, 8001, 8189, 9500, 9600 all on 0.0.0.0. Bind to 127.0.0.1. |
| P1-018 | GNOME Remote Desktop daemon with TPM failure (credential fallback) | Swarm-sec SEC-010 | OPEN | valkyrie | RDP running with weakened credential storage. Disable if not used. |

---

# P1 — SYNLEARNS SECURITY

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P1-030 | Server-blind progress manipulation (SLS-002) | Unicorn 012 | OPEN | nimah | Progress can be manipulated client-side to unlock content without payment. Server-side validation needed. |
| P1-031 | Tier escalation — active user upgrade silently ignored | Unicorn 012, Swarm-sls | OPEN | nimah | webhooks.py:33-44 skips active users. A paying upgrade gets no tier change. Add: if active and new tier > existing, update. |
| P1-032 | Pretest tier calc overwrites Stripe-paid tier | Swarm-sls, Audit-0402 | OPEN | nimah | assessment.py:325-328 unconditionally sets tier from pretest score. $149 user who scores poorly = downgraded. **NEEDS FAREEZ PRODUCT DECISION.** |
| P1-033 | No server-side logout endpoint | Audit-0402 | OPEN | nimah | No POST /auth/logout. JTI not invalidated. Access token valid until exp. |
| P1-034 | Admin route uses AuthGuard not AdminGuard | Audit-0402 | OPEN | nimah | App.tsx:87 — any authenticated user can mount AdminPage (backend 403s, no data leak, but component renders). One-line fix. |
| P1-035 | No rate limiting on auth endpoints | Audit-0402 | OPEN | nimah | /auth/login, /auth/register, /auth/refresh — zero rate limiting. Add slowapi or CF rules. |

---

# P1 — RELIABILITY (OOM, Crashes, Service Stability)

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P1-050 | No OOMScoreAdjust on user services | Unicorn, Swarm-svc | AGENT-DONE main 2026-04-02 | main | Fixed: -500 (bridge,memory,embedding,heartbeat), +500 (A0), -200 (telegram). Commit e7db18c. |
| P1-051 | No MemoryMax on user services — A0 hit 46.8GB | Swarm-svc SVC-004 | AGENT-DONE main 2026-04-02 | main | Fixed: A0=8G, bridges=2-4G, TG=1G, heartbeat=512M. Commit e7db18c. |
| P1-052 | Restart=always with no StartLimitBurst on 6 services | Unicorn, Swarm-svc | AGENT-DONE main 2026-04-02 | main | Fixed: StartLimitBurst=5 + IntervalSec=300 on all 6. Commit e7db18c. |
| P1-053 | Missing After=/Wants= dependencies — grading-proxy → memory-bridge | Unicorn, Swarm-svc | AGENT-DONE main 2026-04-02 | main | Fixed: After=memory-bridge.service + Wants=memory-bridge.service added. Commit e7db18c. |
| P1-054 | Embedding service crashes on sequences >4772 tokens | Swarm-svc SVC-003 | AGENT-DONE main 2026-04-02 | main | Already fixed: MAX_SEQ_LENGTH=2048 with truncation (commit b2fea44). Cannot overflow. |
| P1-055 | Heartbeat service dead 12+ hours, not auto-restarting | Swarm-svc SVC-002 | AGENT-DONE main 2026-04-02 | main | Fixed: restarted, PATH includes conda bin, reports "All systems nominal". Commit e7db18c. |
| P1-056 | repair_agent crashes on import if env vars missing | Swarm-code NF-007 | AGENT-DONE main 2026-04-02 | main | Fixed: RuntimeError → warning log, send_telegram() returns False gracefully. Commit 8e14b34. |
| P1-057 | claude-code-sync syncing 0 entries every 15 min | Swarm-svc SVC-009 | AGENT-DONE main 2026-04-02 | main | Root cause: memory-bridge crashed on SQLite dedup. Fixed dedup-before-index. Bridge running. Commit af03a53. |

---
---

# P2 — BUGS & CODE QUALITY

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P2-001 | FAISS ingest failures silently swallowed | Unicorn, Swarm-agents | AGENT-DONE main 2026-04-02 | main | Fixed: pending_sync logged at INFO with doc_id + index. Commit eed5bd1. |
| P2-002 | FAISS ID collision on embedding failure | Swarm-code NF-001 | AGENT-DONE main 2026-04-02 | main | Fixed: auto-dedup on startup before unique index creation. Commit af03a53. |
| P2-003 | httpx AsyncClient race — duplicate client leak | Swarm-code NF-002 | AGENT-DONE main 2026-04-02 | main | Fixed: asyncio.Lock in _get_client(). Commit af03a53. |
| P2-004 | Sync file I/O in async handlers (5 specific locations) | Unicorn, Swarm-code | AGENT-DONE main 2026-04-02 | main | Fixed: _write_conditioning() wrapped in asyncio.to_thread() at both call sites. Commit 66197a2. |
| P2-005 | Bare except blocks hiding diagnostic info | Unicorn, Swarm-code NF-013 | AGENT-DONE main 2026-04-02 | main | Fixed: force_fans_v2.py bare excepts → (OSError, ValueError). No others found in bridge/hw/services. Commit 650cde5. |
| P2-006 | TOCTOU race — DB fixed but FAISS orphan remains | Unicorn 025, Swarm-code SC-002 | AGENT-DONE main 2026-04-02 | main | Fixed: _check_duplicate() before FAISS embed narrows race window. Nightly cleanup handles remainder. Commit d06c22f. |
| P2-007 | DB connection leak in embedding_service.py _search_sqlite | Swarm-code NF-009 | AGENT-DONE main 2026-04-02 | main | Fixed: conn.close() in finally block. Corrupted rows skipped. Commit 87f4ff5. |
| P2-008 | DB connection opened per graded response (no pooling) | Unicorn 022 | WONTFIX | main | No sqlite3 in grading_proxy.py. memory_bridge.py uses per-call connections by design (lightweight, WAL). |
| P2-009 | Duplicate SSE [DONE] in buffered stream | Swarm-code NF-004 | AGENT-DONE main 2026-04-02 | main | Fixed: reordered if-check before append — sentinel emits once. Commit 2ca5c71. |
| P2-010 | checkin-telegram now failing with Connection Refused | Swarm-svc SVC-007 | BLOCKED | main | TG HTTP 400 (token/chatid issue) + WA Connection Refused (OpenClaw down). External config needed. |
| P2-011 | repair_agent command dispatch — type mismatch at line 372 | Swarm-code NF-008 | AGENT-DONE main 2026-04-02 | main | Fixed: bool return renamed 'is_model' for clarity. Commit a074d70. |
| P2-012 | vllm-omni talker GPF in libamdhip64.so | Task 001 | BLOCKED | main | Stale from pre-crash. Requires fresh HIP runtime debugging. |
| P2-013 | Shared mutable default bug in A0 extensions | Unicorn 020 | AGENT-DONE main 2026-04-02 | main | Fixed: list=[] → None + lazy init. Commit 484cba9 (agent-zero repo). |
| P2-014 | Heartbeat PATH missing conda agent0 bin | Unicorn, Swarm-svc | AGENT-DONE main 2026-04-02 | main | Fixed with P1-055 service update. Commit e7db18c. |
| P2-015 | asyncio.Lock() created before event loop in grading_proxy | Swarm-code NF-006 | AGENT-DONE main 2026-04-02 | main | Fixed: lazy init via _get_retry_lock(). 4 call sites updated. Commit 2ca5c71. |
| P2-016 | Non-atomic write to ANAQ_FEEDBACK.md | Swarm-code NF-012 | AGENT-DONE main 2026-04-02 | main | Fixed: tempfile + os.replace() atomic write. Commit e5db0d2. |
| P2-017 | Unhandled JSONDecodeError in embedding_service | Swarm-code NF-010 | AGENT-DONE main 2026-04-02 | main | Fixed: try/except around json.loads, returns 400. Commit 87f4ff5. |
| P2-018 | turn_sync.py DB connection not closed on exception | Swarm-code NF-011 | AGENT-DONE main 2026-04-02 | main | Fixed: conn.close() in finally block. Commit bdd7670. |
| P2-019 | 44 orphaned FAISS vectors in AGENTS index | Swarm-agents FAISS-001 | BLOCKED [FA-006] | main | Cleanup script at ~/.anaq/grading/faiss_cleanup.py. Run post-migration. |
| P2-020 | Fire-and-forget asyncio task not tracked | Swarm-code NF-017 | AGENT-DONE main 2026-04-02 | main | Fixed: tracked in _background_tasks set with done callback. Commit 2ca5c71. |
| P2-021 | CODEBASE and MEDICAL FAISS indices empty (dead config) | Swarm-agents FAISS-003 | OPEN | main | Header only, 0 vectors. Either unpopulated or misconfigured. |
| P2-022 | Taskboard dispatch daemon not running, inbox unmonitored | Swarm-agents TB-002 | OPEN | main | Inbox dirs exist but nothing watches them. |
| P2-023 | Grading proxy upstream LLM down — pipeline offline | Swarm-agents GRAD-001 | OPEN | main | Port 8000 not serving. Grading pipeline non-functional. |
| P2-024 | pearl.sqlite.corrupt.bak — Pearl memory DB corruption, unlogged | Swarm-agents ORPHAN-001 | OPEN | main | 10.8MB corrupted 2026-03-20. Data loss extent unknown. |
| P2-025 | Dual embedding spaces — OC GemmaEmbed vs ANAQ nomic-embed | Swarm-agents OC-002 | OPEN | main | Not cross-searchable. Queries hit wrong index depending on entry point. |
| P2-026 | ANAQ model routes through Claude Bridge undocumented | Swarm-agents OC-001 | OPEN | main | anaq_bridge/claude-opus-4-6 usage outside bridge mandate. |
| P2-027 | Registration expiry hardcoded 180 days regardless of tier | Swarm-sls ACT-014 | OPEN | nimah | All tiers get same expiry. May need tier-based duration. |
| P2-028 | Unknown Stripe price ID silently defaults to tier 3 | Swarm-sls ACT-015 | OPEN | nimah | Safe for paying users but could mask config errors. Log a warning. |

---

# P2 — SYNLEARNS BUSINESS

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P2-050 | Set 3 Stripe price IDs in production .env | Nimah 005 | FAREEZ-ACTION | — | STRIPE_PRICE_ID_FEEDBACK, _REFERRAL, _FULL from Stripe Dashboard. |
| P2-051 | Verify JWT_PRIVATE_KEY in Cloudflare Workers | Nimah 005 | FAREEZ-ACTION | — | `cd ~/synlearns-failover && npx wrangler secret list` |
| P2-052 | Create .env.d1sync and enable D1 sync timer | Nimah 005 | FAREEZ-ACTION | — | Copy example, fill creds, enable timer. |
| P2-053 | synlearns-failover has no git remote | Nimah 005 | FAREEZ-ACTION | — | Add remote for backup. |
| P2-054 | SLS-D1-SYNC EnvironmentFile missing from disk | Swarm-svc SVC-005 | BLOCKED [FA on P2-052] | — | Timer unit references env file that doesn't exist yet. |
| P2-055 | Admin page needs QA — blocked on P1-034 (AuthGuard fix) | Nimah 005 | BLOCKED [P1-034] | nimah | |
| P2-056 | Dead GEMINI_API_KEY in vite.config.ts | Nimah 005 | OPEN | nimah | Lines 13-16 still present. Remove. |
| P2-057 | Orphaned SLS_QUIZ_DATA KV binding in wrangler.toml | Nimah 005 | OPEN | nimah | Verify if namespace in use; if not, remove. |
| P2-058 | Stripe upgrade for active users silently ignored | Audit-0402 | OPEN | nimah | Duplicate of P1-031 at code level. webhooks.py:33-44. |
| P2-059 | Input validation — no max_length on password, fingerprint, click_history | Audit-0402 | OPEN | nimah | DoS via oversized fields. password max 1024, fingerprint max 128, click_history max 100. |
| P2-060 | Module detail returned regardless of lock status | Audit-0402 | OPEN | nimah | course.py:56-124. Structure exposed to locked users. |
| P2-061 | Asset coming_soon fires before access check | Audit-0402 | OPEN | nimah | content.py:109-116. Any auth user can confirm asset existence by UUID. |

---

# P2 — INFRASTRUCTURE

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P2-070 | NVMe SMART data unavailable | Swarm-infra INFRA-006 | OPEN | main | smartctl cannot read NVMe health. Install nvme-cli, check `nvme smart-log /dev/nvme0`. |
| P2-071 | mi300x_artifacts 306MB orphaned on NVMe | Swarm-infra INFRA-007 | OPEN | main | Stale MI300X files. Safe to remove. |
| P2-072 | Port 18790 listener — CLAUDE.md documents 18789 | Swarm-infra INFRA-008 | OPEN | main | Undocumented port. Identify and document or close. |
| P2-073 | file_manager.py on 0.0.0.0:8189 undocumented | Swarm-infra INFRA-002 | OPEN | main | Not in CLAUDE.md services table. Document or bind to localhost. |
| P2-074 | Nginx 3 workers for local-only use | Unicorn 039 | OPEN | main | Reduce to 1-2. |
| P2-075 | ANAQ grading log not going to journal (rotation gap) | Swarm-svc SVC-008 | OPEN | main | stdout/stderr → append file instead of journal. No rotation. |
| P2-076 | 4 FAISS indices have no .index.bak backup | Swarm-agents FAISS-004 | OPEN | main | BUSINESS, SOLUTIONS, CODEBASE, MEDICAL lack backup. Single point of failure. |

---
---

# P3 — CLEANUP & OPTIMIZATION

| ID | Item | Source | Status | Agent | Notes |
|----|------|--------|--------|-------|-------|
| P3-001 | Shell scripts missing set -euo pipefail | Unicorn 023 | OPEN | main | hardware_control/ + vllm_workspace/bin/. |
| P3-002 | Unquoted variable expansions in GPU scripts | Unicorn 023 | OPEN | main | Word splitting risk in sysfs paths. |
| P3-003 | Missing health check endpoints on services | Unicorn 024 | OPEN | main | Several services lack /health. |
| P3-004 | cloud-init, bluetooth, anacron — disable | Unicorn 039, Swarm-svc | OPEN | main | Free resources. All confirmed still active. |
| P3-005 | vm.swappiness=60 — too aggressive | Unicorn 039, Swarm-infra | OPEN | main | 2.2GB swapped with 57GB free, kswapd0 at 0.4% CPU. Set to 10. |
| P3-006 | GPU 1 fan RPM reads 0 | Audit S4 | OPEN | — | Swarm-infra notes: may be normal at <50C idle. Physical check when convenient. |
| P3-007 | OpenClaw gateway disabled — re-enable when ready | Session | OPEN | — | anaq-grading plugin fixed. Can re-enable for cron dispatch. |
| P3-008 | avahi-daemon announcing on local network | Swarm-sec SEC-011 | OPEN | main | Disable if not used. |
| P3-009 | WiFi interface active parallel to ethernet | Swarm-sec SEC-013 | OPEN | main | wlp5s0 active alongside ethernet. Disable WiFi if not needed. |
| P3-010 | SCLK DPM state 1 = 0MHz on both cards | Swarm-infra INFRA-005 | OPEN | — | Cosmetic. State 1 shows 0MHz but functions normally. |
| P3-011 | OpenClaw-gateway-alpha healthcheck fires before ready | Swarm-svc SVC-010 | OPEN | main | ExecStartPost runs before service fully initialized. |
| P3-012 | Telegram bridge SSL handshake timeouts (recurring) | Swarm-svc SVC-012 | OPEN | main | Network instability pattern. May be ISP-related. |
| P3-013 | MCLK OC status — DPM state 3 still 1249 on both cards | Swarm-infra INFRA-001 | OPEN | — | OC_DEBUG shows od_UclkFmax=1400 but DPM doesn't follow. Known kernel limitation. |
| P3-014 | agent0 profile no memory_subdir — collision risk with general | Swarm-agents A0-001 | OPEN | main | Both profiles write to same memory dir. |
| P3-015 | 5 openclaw.json.bak files with no rotation | Swarm-agents OC-004 | OPEN | main | No timestamps. Clean up or add rotation. |
| P3-016 | launch_manager.pid stale — false "already running" | Swarm-agents ORPHAN-002 | OPEN | main | Contains PID 74260, long dead. Delete or add staleness check. |
| P3-017 | comfy-gallery WantedBy=multi-user.target in user service | Swarm-svc SVC-011 | OPEN | main | System target in user scope — [Install] has no effect. |
| P3-018 | anaq-grading-proxy StartLimitIntervalSec stale warning | Swarm-svc SVC-001 | OPEN | main | Journal noise from old unit cache. daemon-reload may clear. |
| P3-019 | assessment_type unconstrained str, should be Literal | Swarm-sls ACT-012 | OPEN | nimah | assessment.py — accept only known types. |
| P3-020 | wake_xtx.py no exception handling or tensor cleanup | Swarm-code NF-015 | OPEN | main | 2.5GB/GPU allocation with no error path. |
| P3-021 | heartbeat.py ValueError on NFS df output | Swarm-code NF-016 | OPEN | main | Line 153: int("-") crash on bindmount. |

---
---

# FAREEZ ACTION ITEMS

| ID | Action | Urgency | Notes |
|----|--------|---------|-------|
| FA-001 | Set Stripe price IDs in .env | Before payments | STRIPE_PRICE_ID_FEEDBACK, _REFERRAL, _FULL |
| FA-002 | Verify JWT_PRIVATE_KEY in Cloudflare | Before failover live | `npx wrangler secret list` |
| FA-003 | Create .env.d1sync + enable timer | When ready | Copy example, fill creds |
| FA-004 | Add git remote for synlearns-failover | Low | Backup tracking |
| FA-005 | Physical check GPU 1 fan | When convenient | RPM=0 may be normal at idle temps |
| FA-006 | Restart harrier FAISS migration | When GPUs free | Was 281/6314. Run faiss_cleanup.py after. |
| FA-007 | Product decision: pretest tier vs Stripe tier | Before launch | P1-032. Should pretest ever downgrade a paid tier? |

---
---

# ARCHIVE — VERIFIED COMPLETE

| ID | Item | Fixed By | Verified By | Date |
|----|------|----------|-------------|------|
| — | Hardcoded tokens removed from 4 Python files | Main | — | 2026-04-02 |
| — | bridge.env created (chmod 600), 3 services wired | Main | — | 2026-04-02 |
| — | OpenClaw main workspace binding added | Main | — | 2026-04-02 |
| — | memorySearch enabled in openclaw.json | Main | — | 2026-04-02 |
| — | anaq-grading plugin registered | Main | — | 2026-04-02 |
| — | 5 dead vLLM presets commented out | Main | — | 2026-04-02 |
| — | vllm_env_lock.txt regenerated (325 packages) | Main | — | 2026-04-02 |
| — | udev 99-fan-control.rules cleaned (7 invalid lines) | Main | — | 2026-04-02 |
| — | 795MB disk reclaimed | Main | — | 2026-04-02 |
| P1-007 | SynLearns refresh token replay — bcrypt hash validation | Nimah (cc507ff) | Tafakkur | 2026-04-02 |
| — | Stripe 3-tier pricing wired (new/expired users) | Nimah (2096331) | Tafakkur | 2026-04-02 |
| — | JWT_PRIVATE_KEY documented in wrangler.toml | Nimah (303f0ee) | Tafakkur | 2026-04-02 |
| — | D1 sync script + systemd timer created | Nimah (2d29112) | — | 2026-04-02 |
| — | Admin frontend page created (AdminPage.tsx) | Nimah (71e9429) | — | 2026-04-02 |
| — | OpenClaw anaq-grading config crash fixed | Tafakkur | — | 2026-04-01 |
| P1-024 | 3 services restarted for bridge.env | Main | Swarm-svc | 2026-04-02 |
| P3-013 | Dead vLLM presets commented out | Main | — | 2026-04-02 |

---

## AUDIT HISTORY

| Date | Type | Agents | Findings | Report Location |
|------|------|--------|----------|-----------------|
| 2026-04-01 | Full system audit | 8 Sonnet | 33 critical, 72 warn, 41 info | ~/Documents/FULL_SYSTEM_AUDIT_2026-04-01.md |
| 2026-04-01-02 | Unicorn eval run 1 | 1 Qwen 3.6 (10 tasks) | See inbox | ~/.anaq/taskboard/inbox/unicorn/ |
| 2026-04-02 | Unicorn eval run 2 | 1 Qwen 3.6 (13 tasks) | See inbox | ~/.anaq/taskboard/inbox/unicorn/ |
| 2026-04-02 | Main + Nimah + Valkyrie dispatch | 3 Opus | 16 fixes, 1 OOM | ~/.anaq/taskboard/inbox/{main,nimah}/ |
| 2026-04-02 | Swarm audit #2 | 6 Sonnet | Merged above | ~/.anaq/logs/swarm-audit-0402/ |

---

## COUNTS

| Priority | Open | Agent-Done | Verified | Fareez-Action | Blocked | Wontfix | Total |
|----------|------|------------|----------|---------------|---------|---------|-------|
| P0 | 0 | 2 | 0 | 1 | 1 | 0 | 4 |
| P1 Security | 18 | 0 | 0 | 0 | 0 | 0 | 18 |
| P1 SynLearns | 6 | 0 | 0 | 0 | 0 | 0 | 6 |
| P1 Reliability | 0 | 8 | 0 | 0 | 0 | 0 | 8 |
| P2 Bugs | 0 | 16 | 0 | 0 | 3 | 1 | 20 |
| P2 SynLearns | 6 | 0 | 0 | 4 | 2 | 0 | 12 |
| P2 Infra | 7 | 0 | 0 | 0 | 0 | 0 | 7 |
| P3 | 20 | 1 | 0 | 0 | 0 | 0 | 21 |
| **TOTAL** | **57** | **27** | **0** | **5** | **6** | **1** | **96** |

*Archived (verified complete): 17 items (includes P3-013 dead presets)*
*Fareez personal actions: 8 items (added MCE monitoring)*
*Main round 2: 27 items AGENT-DONE, 3 BLOCKED, 1 WONTFIX*
