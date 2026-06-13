# DAG-Based Agentic Architecture with Browser Skill

A growing-graph orchestrator that decomposes user queries into a **directed acyclic graph (DAG)** of specialized skills, executes them in parallel where possible, and self-heals on failure through critic-gated recovery planning.

---

## 1. Objective

Traditional single-prompt agents struggle with multi-step queries that require fetching live web data, searching indexed knowledge, and structuring the output. This system solves that by:

- **Decomposing** any natural-language query into a DAG of skill nodes via an LLM Planner
- **Executing** independent branches in parallel (e.g., three browser fetches run concurrently)
- **Validating** outputs through auto-inserted Critic nodes that catch fabrication
- **Recovering** from failures automatically -- the orchestrator re-invokes the Planner with completed results and a failure report, so it can wire a new sub-graph that reuses prior work
- **Abstracting** LLM providers behind a unified gateway that routes requests across 7 providers with per-agent cost tracking

The result: a single query like *"Compare the top 3 trending models on Hugging Face"* fans out into browser fetches, distillation, critic validation, and formatted output -- all orchestrated as a self-growing graph.

---

## 2. Architecture

### High-Level Flow

```
                              USER QUERY
                                  |
                                  v
                        +------------------+
                        |     Planner      |   Decomposes query into a DAG
                        +------------------+   of skill nodes (NetworkX DiGraph)
                                  |
                  +---------------+---------------+
                  |               |               |
                  v               v               v
           +----------+   +----------+   +------------+
           | Browser  |   |Researcher|   | Retriever  |   Parallel execution
           | (4-layer |   | (search +|   | (FAISS     |   of independent
           |  cascade)|   |  fetch)  |   |  memory)   |   branches
           +----------+   +----------+   +------------+
                  |               |               |
                  +-------+-------+---------------+
                          |
                          v
                   +-------------+
                   |  Distiller  |   Extracts structured fields
                   +-------------+   from raw upstream data
                          |
                          v  (auto-inserted when distiller has critic: true)
                   +-------------+
                   |   Critic    |   pass/fail verdict
                   +-------------+   (fail triggers recovery Planner)
                          |
                          v
                   +-------------+
                   |  Formatter  |   Renders final user-facing answer
                   +-------------+
                          |
                          v
                     FINAL ANSWER
```

### System Components

```
9-AgenticArchitecture_BrowserAgent/
|
|-- BrowserAgent/code/           <-- Orchestrator + all skills
|   |-- flow.py                      Main loop: Graph + Executor
|   |-- skills.py                    Skill registry, prompt rendering, dispatcher
|   |-- recovery.py                  Failure classification + recovery policy
|   |-- memory.py                    FAISS vector memory with keyword fallback
|   |-- persistence.py               Session storage (graph + per-node JSON)
|   |-- mcp_runner.py                Multi-turn tool-use loop (MCP stdio)
|   |-- gateway.py                   Bridge to llm_gatewayV9 (auto-starts)
|   |-- schemas.py                   Pydantic models (AgentResult, NodeSpec, etc.)
|   |-- artifacts.py                 Content-addressable blob store (sha256)
|   |-- agent_config.yaml            Skill catalogue (10 skills)
|   |-- browser/                     Browser skill (4-layer cascade)
|   |   |-- skill.py                     Cascade orchestrator
|   |   |-- driver.py                    A11y + Vision drivers
|   |   |-- dom.py                       DOM enumeration via Playwright JS
|   |   |-- client.py                    httpx client for /v1/vision & /v1/chat
|   |   |-- highlight.py                Pillow-based Set-of-Marks annotation
|   |-- prompts/                     System prompts per skill (.md files)
|   |   |-- planner.md, researcher.md, browser.md, distiller.md,
|   |   |-- critic.md, formatter.md, retriever.md, summariser.md, ...
|   |-- state/                       Runtime data (sessions, memory, FAISS index)
|
|-- llm_gatewayV9/               <-- LLM Gateway (FastAPI service)
    |-- main.py                      /v1/chat, /v1/vision, /v1/embed, /v1/cost
    |-- providers.py                 7 LLM providers (Gemini, Groq, Ollama, etc.)
    |-- router.py                    Request routing + failover
    |-- embedders.py                 Fixed 768-dim embedding (nomic/gemini)
    |-- db.py                        SQLite cost ledger
    |-- agent_routing.yaml           Skill-to-provider pinning
    |-- client.py                    Python SDK (LLM class)
```

### How the Orchestrator Grows the Graph

The DAG is not static. It **grows at runtime** through five mechanisms:

| Mechanism | Trigger | Example |
|-----------|---------|---------|
| **Planner seed plan** | Session start | Planner emits browser + researcher + distiller + formatter |
| **Dynamic successors** | Any skill's output | Researcher emits a follow-up distiller node |
| **Static internal_successors** | agent_config.yaml | Coder always chains to sandbox_executor |
| **Critic auto-insertion** | Skills with `critic: true` | Distiller's output is gated by a Critic |
| **Recovery re-planning** | Node failure or critic fail | Planner re-invoked with prior results + failure report |

### Recovery and Self-Healing

When a node fails or a Critic rejects its output:

1. `recovery.py` classifies the failure (transient / validation / upstream)
2. If recoverable, a **recovery Planner** node is queued with:
   - All previously completed node IDs as inputs (so it can reuse their data)
   - A failure report including the failed node's goal, URL, and error
3. The recovery Planner emits a new sub-graph that wires existing results and only re-runs what failed
4. Up to **3 critic-fail recovery cycles** per session (`MAX_CRITIC_RECOVERIES`)

---

## 3. How the Browser Skill Works

The Browser skill is a **4-layer cascade** that starts cheap (no LLM) and escalates only when needed:

```
Layer 1: HTML Extract (no LLM)
    |
    | Content too short or goal needs interaction?
    v
Layer 2a: Deterministic Selectors (optional, no LLM)
    |
    | No selectors provided or selector failed?
    v
Layer 2b: Accessibility Driver (text-only LLM via /v1/chat)
    |
    | Text-only context insufficient?
    v
Layer 3: Vision Driver (screenshot + LLM via /v1/vision)
```

### Layer Details

| Layer | LLM Cost | How It Works |
|-------|----------|-------------|
| **L1: Extract** | None | `httpx` fetch + `trafilatura` HTML-to-text extraction. Succeeds when content > 200 chars and goal doesn't require interaction (click, fill, sort, etc.). Detects gateway blocks (CAPTCHA, login walls, Cloudflare). |
| **L2a: Deterministic** | None | Runs Playwright actions from caller-provided CSS selectors (`metadata.selectors`). No guessing -- only fires if selectors are explicitly given. |
| **L2b: A11y Driver** | Low | Opens page in headless Chromium via Playwright. Enumerates interactive DOM elements (buttons, links, inputs) into a text legend. Sends legend to `/v1/chat` -- LLM picks actions (click, type, scroll) from a structured schema. No screenshot needed. |
| **L3: Vision (SoM)** | High | Takes a screenshot, annotates it with numbered bounding boxes (Set-of-Marks via Pillow), and sends the annotated image + element legend to `/v1/vision`. The VLM sees what the user would see and picks actions accordingly. |

### DOM Enumeration

Each turn, `dom.py` injects JavaScript via `page.evaluate()` that returns all visible interactive elements:
- Tags: `a[href]`, `button`, `input`, `textarea`, `select`, ARIA roles
- Each element: stable per-turn ID, tag, role, accessible name, bounding box
- Excludes: hidden, zero-size, off-screen elements
- Name resolution: aria-label -> innerText -> placeholder -> title -> alt

### Action Vocabulary

The LLM (in both A11y and Vision modes) outputs structured actions:

| Action | Description |
|--------|-------------|
| `click(mark)` | Click center of element by ID |
| `type(mark, value)` | Clear field and type text |
| `key(value)` | Press keyboard key (Enter, Tab, Escape) |
| `scroll(direction, amount)` | Scroll page (up/down/left/right) |
| `drag(from, to)` | Drag between coordinates (for canvas apps) |
| `wait(seconds)` | Pause for animations |
| `done(success, note)` | Terminate with result |

### Gateway Block Detection

Conservative pattern matching detects pages that refuse automation:
- CAPTCHA: hCaptcha, reCAPTCHA, "Let's confirm you are human"
- Cloudflare: challenge pages, browser verification
- Login walls: "Sign in to continue", "You must be logged in"

When detected, the skill returns `error_code="gateway_blocked"` so the Planner can route to a different source.

---

## 4. Setup and Run

### Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** package manager
- **API keys** for at least one LLM provider (Gemini recommended)
- **Ollama** (optional, for local models and embeddings)

### Step 1: Create Virtual Environments and Install Dependencies

`uv sync` creates an isolated `.venv/` inside each project directory and installs all dependencies into it. `uv run` automatically activates that environment before executing a command.

```bash
# 1. Set up the LLM Gateway (creates llm_gatewayV9/.venv/)
cd llm_gatewayV9
uv sync

# 2. Set up the BrowserAgent (creates BrowserAgent/code/.venv/)
cd ../BrowserAgent/code
uv sync

# 3. Install Playwright browsers (needed for Browser skill)
uv run playwright install chromium
```

> **Without uv:** If you prefer standard virtualenv, create and activate one manually in each directory, then install with pip:
> ```bash
> cd llm_gatewayV9
> python -m venv .venv
> .venv\Scripts\activate        # Windows
> # source .venv/bin/activate   # macOS/Linux
> pip install -r requirements.txt  # or: pip install -e .
>
> cd ../BrowserAgent/code
> python -m venv .venv
> .venv\Scripts\activate
> pip install -e .
> playwright install chromium
> ```

### Step 2: Configure API Keys

Both directories ship a `.env.example` with all supported variables. Copy and fill in your keys:

```bash
# LLM Gateway -- at least one provider key required (Gemini recommended)
cp llm_gatewayV9/.env.example llm_gatewayV9/.env
# Edit llm_gatewayV9/.env and set GEMINI_API_KEY, GROQ_API_KEY, etc.

# BrowserAgent -- needed for the Researcher skill's web search
cp BrowserAgent/code/.env.example BrowserAgent/code/.env
# Edit BrowserAgent/code/.env and set TAVILY_API_KEY
```

> See each `.env.example` for the full list of optional provider keys, model overrides, and embedding configuration.

### Step 3: Start the LLM Gateway

```bash
cd llm_gatewayV9
uv run main.py
# Gateway starts on http://localhost:8109
# Verify: curl http://localhost:8109/health
```

> **Note:** The BrowserAgent auto-starts the gateway if it isn't running, but starting it manually lets you see its logs.

### Step 4: Run a Query

```bash
cd BrowserAgent/code

# Simple query (researcher + formatter)
uv run python flow.py "What is the mass of the Earth?"

# Multi-item fan-out (parallel browser fetches)
uv run python flow.py "Compare 3 laptops under 80,000 INR on Amazon India"

# Browser skill (interactive page)
uv run python flow.py "What are the top 3 trending models on Hugging Face this week?"

# Resume a failed session
uv run python flow.py --resume s8-a1b2c3d4 "original query"
```

### Step 5: Inspect Results

Each session is persisted under `BrowserAgent/code/state/sessions/<session-id>/`:

```
state/sessions/s8-a1b2c3d4/
  query.txt          # Original user query
  graph.json         # Full DAG with node statuses and results
  nodes/
    n_001.json       # Per-node detail (skill, inputs, prompt sent, result)
    n_002.json
    ...
```

Generate a human-readable report:

```bash
uv run python report.py s8-a1b2c3d4
```

---

## 5. Skill Catalogue

All skills are defined in `agent_config.yaml`. Each skill is a YAML entry + a prompt template in `prompts/` -- there is no Python class per skill.

| Skill | Tools | Temperature | Role |
|-------|-------|-------------|------|
| **planner** | none | 0.4 | Decomposes queries into DAGs; handles recovery re-planning |
| **researcher** | web_search, fetch_url | 0.7 | Multi-step web research; returns findings text |
| **browser** | (own cascade) | 0.0 | Interactive web pages via Playwright (extract/a11y/vision) |
| **retriever** | search_knowledge | 0.2 | Searches FAISS memory index for indexed material |
| **distiller** | none | 0.1 | Extracts structured fields from raw text (critic-gated) |
| **critic** | none | 0.0 | Pass/fail verdict on upstream output; triggers recovery on fail |
| **formatter** | none | 0.3 | Renders final user-facing answer (terminal node) |
| **summariser** | none | 0.3 | Condenses long content |
| **coder** | none | 0.2 | Emits Python code (stub) |
| **sandbox_executor** | none | 0.0 | Runs code from Coder in a sandbox |

### Provider Routing

`agent_routing.yaml` pins each skill to a preferred LLM provider. The gateway falls back through its provider ladder if the pinned provider is unavailable:

```yaml
planner: gemini       # Needs strong reasoning
researcher: gemini    # Long-context for web pages
critic: groq          # Fast, deterministic
browser: gemini       # Vision-capable for Layer 3
```

---

## 6. Memory System

The agent maintains a persistent FAISS vector index (`state/memory.json` + `.faiss` files):

- **Writes**: Every query, tool outcome, and LLM-classified fact is embedded (768-dim via gateway) and indexed
- **Reads**: At session start, `memory.read(query)` returns the top-k cosine-similar hits (keyword fallback if FAISS is empty)
- **Scoping**: Memory hits are injected only into Planner, Researcher, and Retriever prompts -- downstream skills (Distiller, Critic, Formatter) see only their upstream inputs
- **Kinds**: `fact`, `preference`, `tool_outcome` (embedded), `scratchpad` (keyword-only)

---

## 7. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **NetworkX DiGraph** | Enables topological scheduling, parallel execution of ready nodes, and graph persistence via `node_link_data` |
| **Skills are YAML + prompt, not Python classes** | Adding a new skill = one YAML entry + one .md file. No code change unless the skill needs custom dispatch (like Browser or Sandbox) |
| **Tool-blindness** | Planner names skills, never tools. MCP tools are an implementation detail of each skill. This keeps the decomposition clean and provider-agnostic |
| **Critic auto-insertion** | Rather than asking the Planner to always emit critics, the orchestrator inserts them automatically on edges from `critic: true` skills. Prevents duplicate critics and ensures coverage |
| **Browser cascade** | Starting with HTML extraction (free) and escalating to vision (expensive) keeps cost proportional to page complexity. Most static pages resolve at Layer 1 |
| **Gateway abstraction** | Skills never import provider SDKs. The gateway handles routing, failover, retries, and cost tracking. Switching providers = editing `agent_routing.yaml` |
| **Content-addressable artifacts** | Large blobs (HTML pages, screenshots) are stored once by sha256 hash. Skills pass handles, not raw bytes, keeping prompts bounded |

---

## 8. Example Session Trace

Query: *"What are the top 3 most-liked open-source LLMs on Hugging Face?"*

```
session s8-f3a1b2c0  --  query: What are the top 3 most-liked open-source LLMs on Hugging Face?
==============================================================================
[n:1] planner            complete (2.1s)
  -> emitted: n:2 (browser), n:3 (distiller), n:4 (formatter)
[n:2] browser            complete (8.3s)
  -> Layer 1 extract: useful (HF trending page is static HTML)
  -> auto-inserted critic n:5 on distiller n:3
[n:3] distiller          complete (1.8s)
  -> extracted: model_name, likes, downloads for top 3
[n:5] critic             complete (0.9s)
  -> verdict: pass
[n:4] formatter          complete (1.2s)
==============================================================================
FINAL: 1. meta-llama/Llama-3.1-405B (12.3k likes) ...
==============================================================================
```

Total cost tracked per-agent via `/v1/cost/by_agent`.
