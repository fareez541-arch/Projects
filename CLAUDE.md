# CLAUDE.md

## Reasoning Protocol

These rules govern HOW you think. They apply to every domain — code, hardware, data, systems, builds.

### Session Continuity (MANDATORY — runs before any code change)
- On session start, if MEMORY.md references ongoing work:
  1. Read the relevant memory file. Print it verbatim. Do not summarize.
  2. Read `~/CHANGELIST.md` if it exists. Print it verbatim.
  3. Run `git log --oneline -10` in the active repo. Print output.
  4. State the documented next step in one sentence.
  5. Wait for user confirmation before ANY code change or file edit.
- If prior session logs exist and the user asks to review them:
  1. Read and quote the actual tool_use outputs (commands, errors, traces).
  2. Do not dispatch a summarizer agent. Print the raw content.
  3. Trace the chronological fix sequence — what was tried, what failed, what the exact error was.
- Violation of this protocol means the session's first action is wrong. Stop and restart.

### Systems-First Mandate
- Action without understanding is LOGIC CORRUPTION. Applies to ALL system processes: code, data, services, configs, pipelines, agents, hardware, builds.
- If you cannot describe how the component you are about to touch currently works — its inputs, outputs, dependencies, and existing automation — STOP. Read the architecture first.
- Before modifying ANY system component: trace the existing flow end-to-end. Read the source. Map: input → processing → output → downstream consumers. State it explicitly.
- If infrastructure already exists for the operation, use it. Never manually replicate what an existing process does.
- If the existing system is broken, fix the broken link. Do not build a parallel path.
- Detect and halt on these corruption signals:
  - "Let me just [do X] directly" → HALT. What system already handles X?
  - "Quick fix" / "for now" / "temporary workaround" → HALT. What is the permanent path?
  - Writing new code when existing code does the same job → HALT. Find it first.
  - Operating on any subsystem without reading its source → HALT. Read it.
  - Modifying configs without understanding what consumes them → HALT. Trace consumers.
  - Restarting services without understanding why they failed → HALT. Read the logs.
- Violation: state "LOGIC CORRUPTION: acted without understanding [component]. Correcting." Then stop, read the relevant source, restate the architecture, and get user confirmation before proceeding.

### Grounding Mandate
- NEVER answer questions about this system from training knowledge. Read the file, run the command, query the service. If you cannot verify from live data, state: "Unverified — based on training knowledge, not live system state."
- When pre-trained knowledge and live system data conflict, the live system is correct. Always.
- Extract the exact output first, then reason from it. Do not reason first and then look for confirming evidence.

### Objectivity Mandate
- When describing system state, performance, or code quality, use ONLY measurable, mechanistic language.
  - NOT "good performance" — state "508 t/s prompt processing, 80 t/s generation"
  - NOT "the system is healthy" — state "3/3 services responding, 0 error lines in last 100 log entries"
  - NOT "clean code" — state "passes linter, 0 type errors, all tests green"
- Banned in technical contexts: good, bad, robust, clean, elegant, simple, complex, nice, great, excellent, poor, solid, strong, weak, basically, typically, standard, usually, normally, often, generally. State the measurement or mechanism instead.
- For each assertion about system state, cite the specific command output or file content that supports it.

### Anti-Fixation Protocol
- When diagnosing problems, generate at least 3 competing hypotheses before investigating any. For each, identify what evidence would DISPROVE it. Investigate disproving evidence first.
- Do not start from the most obvious or commonly-discussed cause. Start from the actual error output and trace the causal chain mechanistically. User assumptions and "common causes" are hypotheses to test, not facts to confirm.
- Before finalizing any conclusion, ask: "What would someone who disagrees with me point to? What am I not seeing?"
- If a fix attempt fails twice with the same approach, STOP. Restate the problem from scratch. The mental model is wrong.

### Adversarial Review (TPHSA)
- For multi-step debug or optimization (3+ files, unknown root cause): maintain 4 TPHSA panel agents (Theoretical HW, Physical HW, Theoretical SW, Physical SW) as persistent reviewers.
- Do not apply fixes without adversarial review from at least 2 agents.
- Agents must quote file paths, line numbers, and exact values — no unsupported assertions.
- When dispatching agents to review logs or code: require quoted content, not summaries.

### Diagnostic Format
When diagnosing issues, use this structure:
```
OBSERVATION: [exact output/error text]
MECHANISM: [what component produced this and why]
EVIDENCE STRENGTH: [STRONG|MEDIUM|WEAK] — [why]
HYPOTHESES: [H1, H2, H3 with disproving criteria for each]
ACTION: [specific change]
EXPECTED DELTA: [State A → State B, measurable]
FALSIFIER: [metric that would prove this wrong]
```

### Execution Loop
```
1. Read actual system state (files, logs, configs, tool output) AND prior session state (MEMORY.md, CHANGELIST.md, git log)
2. Generate 3+ hypotheses from observations
3. Test disproving evidence for top hypothesis
4. Atomic root cause analysis on confirmed failure
5. Generate precise patch, state expected delta
6. Apply and verify actual delta matches expected
7. If delta mismatch → restate problem, do not retry same approach
8. Determine if further optimizations exist → LOOP to step 1
```

## Data Secrecy

- Never read `.env` files, API keys, tokens, or credentials unless the user explicitly asks. To verify a service, test the endpoint — do not dump credentials.
- Never include proprietary logic, personal data, business context, or system IP in commit messages, PR descriptions, or any externally-visible output.
- Do not summarize, describe, or comment on the purpose or content of agent personas, creative outputs, personal files, or business documents. Scope is system optimization and code.
- All proprietary systems (CCRN formula system, SLS Boolean proof architecture, ANAQ agent configurations, Antigravity pipeline) are trade secrets. Never reference their internal logic in external output.
- Use `$ENV_VAR` references in commands, never inline credential literals.
- Read only the sections of files directly needed for the task. Do not speculatively read files that might contain sensitive content.

## Work Preservation

### Checkpoint Commits
- Commit after every working change. Do not batch fixes.
- One logical change per commit: `fix: patch triton num_warps for wave32` not `fix: various changes`.
- After 3+ file changes without a commit, proactively ask: "I've modified N files — checkpoint commit now?"

### Change Manifest
- For 3+ file edits, create/update `~/CHANGELIST.md`:
  ```
  ## Session: YYYY-MM-DD HH:MM — [description]
  - [file]: [what changed and why]
  ```
- Delete entries once committed and pushed.

### Session Handoff
- Before ending: verify `git status` clean, CHANGELIST.md updated, memory file saved for complex work.
- Before starting continued work: print MEMORY.md entry, CHANGELIST.md, and `git log --oneline -10` verbatim. State documented next step. Get user confirmation before proceeding.

### Branch Discipline
- Multi-file debug/dev work goes on a branch (`git checkout -b <name>`), not `main`.
- Merge to `main` only when confirmed working.

## Hard Constraints

- Zero CUDA/NVIDIA references — flag as logic corruption if detected in code or suggestions.
- gfx1100 (RDNA3). Always set `HSA_OVERRIDE_GFX_VERSION=11.0.0`.
- No xformers — breaks ROCm. If detected, remove.
- Tensor parallel = 2. Both GPUs used together.
- P2P: `NCCL_P2P_LEVEL=LOC` and `RCCL_ENABLE_DIRECT_GPU_TRANSPORT=1` required.
- Git: conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`). Main branch is `main`.

## System Reference

AMD RDNA3 workstation: dual RX 7900 XTX (48GB VRAM), Ryzen 9 5900XT (16C/32T), 64GB DDR4. ROCm 7.2.0, Ubuntu 24.04.4 LTS, kernel 6.17.0-1012-oem.

### Hardware
- **GPU 0:** ASRock XTX, gfx1100, PCIe 4.0 x8 @ 0c:00.0, 320W TDP
- **GPU 1:** PowerColor XTX, gfx1100, PCIe 4.0 x8 @ 0f:00.0, 303W TDP
- **Aggregate:** 192 CUs, 48GB VRAM, 192MB Infinity Cache, both direct to CPU root complex
- **Storage:** 2TB NVMe `/` (~45% used) | 4TB SSD `/media/fareez541/Extreme SSD` (cold storage, ComfyUI models symlinked) | 1TB USB `/media/fareez541/STASH`

### Software
- PyTorch 2.9.1+rocm7.0 (source build) | vLLM 0.17.1rc1 (editable, `~/vllm_source/`) | llama.cpp HIP (`~/llama/llama.cpp/build-hip/`) | Node.js v22.22.1 | Conda miniforge3

### Conda Envs
| Env | Py | Purpose |
|-----|----|---------|
| `vllm` | 3.12 | Primary inference + CCRN. torch 2.9.1+rocm7.0 |
| `comfy` | 3.11 | ComfyUI. torch 2.10.0+rocm7.0 |
| `vllm-omni` | 3.12 | Vision models. torch 2.10.0+rocm7.0 |
| `agent0` | 3.12 | Agent Zero + ANAQ (no torch) |
| `vllm-testbench` | 3.11 | Benchmarking. torch 2.10.0+rocm7.0 |

### Key Directories
| Dir | Purpose |
|-----|---------|
| `~/vllm_workspace/` | Inference server, launch scripts, models, services |
| `~/vllm_source/` | vLLM source (editable install) |
| `~/llama/llama.cpp/` | llama.cpp source + HIP build |
| `~/ccrn_workspace/` | CCRN exam generation pipeline |
| `~/comfy_repository/` | ComfyUI (models symlinked to SSD) |
| `~/hardware_control/` | GPU power/thermal/fan management |
| `~/synlearns-core/` | React+Vite+TS frontend (SynLearns.ai) |
| `~/.anaq/` | ANAQ Hive Mind (bridge, grading, FAISS) |
| `~/.openclaw/` | OpenClaw agent orchestration |

### Services
| Service | Port | Command |
|---------|------|---------|
| vLLM | 8000 | `bash ~/vllm_workspace/vllm-launch.sh <preset> start` |
| llama.cpp HIP | 8000 | `bash ~/vllm_workspace/bin/start_llama_hip.sh <preset>` |
| Agent Zero | 5000 | `systemctl --user restart agent-zero` |
| Claude Bridge | 5500 | `systemctl --user restart claude-code-bridge` |
| Embedding | 9500 | `systemctl --user restart embedding-service` |
| ANAQ Memory | 9600 | `systemctl --user restart memory-bridge` |
| OpenClaw | 18789 | `systemctl --user restart openclaw-gateway` |
| ComfyUI | 8188/8190 | `bash ~/comfy_repository/launch_tandem.sh` |

### Inference Presets
- **vLLM:** `qwen35`, `qwen35_fp8`, `qwen3`/`qwen3_coder`/`qwen3_reason`/`qwen3_vl`, `deepseek`, `savant`/`savant48`, `opus_v2`, `gemini_pro`, `medgemma`, `glm_flash`
- **llama.cpp:** `savant` (48B-A4B), `opus` (30B-A3B), `gemini` (30B-A3B), `heretic` (35B-A3B Q8_0) — 256K ctx, ~508 t/s PP, ~80 t/s TG

### Environment Variables
```
ROCM_HOME=/opt/rocm  HSA_OVERRIDE_GFX_VERSION=11.0.0  HIP_VISIBLE_DEVICES=0,1
GPU_MAX_HW_QUEUES=2  HSA_ENABLE_SDMA=1  HSA_FORCE_FINE_GRAIN_PCIE=1
TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1  NCCL_P2P_LEVEL=LOC  RCCL_ENABLE_DIRECT_GPU_TRANSPORT=1
```

### Tailscale
This machine: `100.74.193.111` | Phone: `100.97.232.7` | Tablet: `100.109.137.101`
