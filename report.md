# loop-agent Research and Design Report — Loop Engineering and LoopAgent

> This report is the deliverable for the **research and design phase** of the loop-agent project. It does not implement anything. It summarizes (1) an in-depth investigation of Loop Engineering / LoopAgent, (2) an inventory and reuse assessment of claude-org-ja assets, and (3) the LoopAgent design, including comparison of multiple options, one recommended option, and a phased roadmap.
>
> - Version: v1.0 (2026-06-27)
> - Target repository: `https://github.com/happy-ryo/loop-agent`
> - SoT: this file, `report.md` (`report.html` is a single-file view of the same content)

---

## 0. Executive Summary

**Loop Engineering** means moving beyond "a human prompts an agent one move at a time" and instead **designing the system, or loop, that prompts, verifies, remembers, and reruns agents**. The concept spread rapidly through practitioner communities in June 2026, sparked by comments from Boris Cherny, Anthropic's head of Claude Code development[^le-def][^le-origin]. In the technical stack, it sits above **prompt engineering (single-turn instructions) -> context engineering (the full token composition visible to the model at inference time) -> loop engineering (the control layer for continuation, termination, and rerun across turns)**. It does not replace the two lower layers; it **wraps** them[^le-stack].

**Conclusion of this report (recommended design)**: the LoopAgent in loop-agent should:

1. Align the **innermost loop** with Anthropic's standard `gather context -> take action -> verify -> repeat` pattern, obtaining ground truth from the environment on every iteration[^anthropic-bea][^agent-sdk-loop].
2. Add an outer two-layer structure with **Reflexion-style cross-attempt memory and linguistic self-reflection**[^reflexion].
3. Implement termination conditions as a **dual structure of semantic judgment (verifiable goal / critic) and mechanical limits (iterations, tokens, time)**, which is the industry-standard pattern common to major frameworks[^framework-common].
4. **Keep state out of context and externalize it into an external SoT equivalent to `state.db`**[^harness].
5. **Limit human gates to irreversible, high-blast-radius actions**[^hitl][^verify-hitl].

Therefore, this report recommends the **"single control layer + shared state machine + phased integration of org assets" design (Option C below)**.

**Reuse of claude-org-ja assets**: the investigation found that most elements required by loop-agent already exist in high-quality implementations in claude-org-ja. The following are especially valuable for reuse:

| Element | claude-org-ja asset | Assessment |
|---|---|---|
| Loop state persistence (SoT) | `tools/state_db/` (SQLite + StateWriter transaction + post-commit snapshot) | reuse-as-is / adapt |
| Inter-loop notification and wake delivery | transport (renga/broker, push primary/pull fallback, at-most-once) | extract-pattern |
| Feedback (self-improving) | org-retro / org-curate / knowledge (raw -> curated, threshold-triggered) | adapt |
| Observation, human gates, runaway detection | attention-watcher / pr-watch / org-escalation / pending_decisions | reuse-as-is / adapt |
| Termination-condition and state-transition types | delegation-lifecycle / state-semantics contract | reference-only |
| Selection of iteration targets | work-discovery (two-layer separation of computation and delivery) | adapt |

**Roadmap**: progress in three phases: **PoC (minimal loop + hard limits) -> MVP (state machine + state.db SoT + two-layer termination conditions + observability) -> full system (autonomous LoopAgent integrating org feedback loops, transport, and human gates)**.

---

## 1. Background and Purpose

### 1.1 Project Objective

loop-agent is a design and implementation project for a **LoopAgent** that realizes full-scale **Loop Engineering**. As the starting point, this report:

- investigates the concepts, lineage, and design issues around Loop Engineering and LoopAgent through extensive web research;
- inventories and evaluates reusable assets from the existing claude-org-ja project; and
- presents a LoopAgent architecture design and phased roadmap based on that work.

This phase **does not include implementation**; it covers design only.

### 1.2 Terminology

| Term | Definition | Source |
|---|---|---|
| **agent** | An LLM that autonomously uses tools in a loop ("LLMs autonomously using tools in a loop"). This is the minimal core of an agentic system. | Anthropic[^ctx-eng] |
| **agentic loop** | An iterative execution cycle in which each iteration aggregates context, the LLM reasons and selects an action, the action is executed, the result is observed, and the observation is fed into the next iteration. | Oracle[^oracle-loop] |
| **prompt engineering** | Designing the wording of single-turn instructions. | [^le-stack] |
| **context engineering** | Structuring and curating the full set of tokens visible to the model during inference: instructions, tools, examples, history, retrieved documents, and related material. A natural evolution of prompt engineering. | Anthropic[^ctx-eng] |
| **loop engineering** | Designing the control layer that continues, terminates, and reruns agents across turns. It wraps prompt/context engineering. | [^le-def][^le-stack] |
| **LoopAgent** | The entity that embodies loop engineering: an autonomous execution agent that wraps an agentic loop with triggers, verifiable goals, and guardrails. This is the design target of this project. | Definition in this report |

---

## 2. In-Depth Investigation of Loop Engineering

> This chapter is based on web research: fan-out search, close reading of sources, and independent adversarial verification of claims. Source URLs are attached to major claims. Because Loop Engineering blog claims are an emerging practitioner-driven concept and are evolving quickly, design decisions are anchored as much as possible in official Anthropic docs and peer-reviewed or research-paper lineage such as ReAct and Reflexion.

### 2.1 What Loop Engineering Is: Definition, Origin, and Three-Layer Stack

**Definition**. Loop Engineering is "the practice of designing the system itself that prompts, verifies, remembers, and reruns agents"; it replaces manual prompt entry with **goal-based automation**[^le-def]. An agentic loop itself consists of two elements: **a `trigger` (event / schedule / human instruction) and a `verifiable goal` (a goal that can be checked)**. The agent cycles through start -> run -> goal-achievement check -> loop again if unmet, without human intervention. Unlike simple automation, which executes predetermined steps, the essential difference is that **decision-making, namely active evaluation of whether the goal has been reached, is embedded inside the loop**[^le-def].

> Independent verification result: this definition is supported almost verbatim by multiple primary and secondary sources, including SmartScope ("designing the system that prompts, checks, remembers, and re-runs AI agents"), Firecrawl, and MindStudio ("replaces manual prompting with goal-based automation"). No contradictory sources were found (verdict: **supported**)[^v-le-def].

**Origin**. This is not an academic term; it is a **practitioner-originated concept from 2025-2026**. In June 2026, a video clip of Boris Cherny (Anthropic, head of Claude Code development) saying *"I don't prompt Claude anymore. I have loops running that prompt Claude and figuring out what to do. My job is to write loops."* spread through interviews and social media, reaching about 700,000 views in 24 hours and later several million views, rapidly popularizing the idea[^le-origin]. Adoption was also influenced by Addy Osmani's framing of taking oneself out of the role of prompting agents and instead designing systems that do it, and by Peter Steinberger's loop-centric workflows[^le-origin].

**Three-layer stack**. Loop Engineering is the top "control layer" in the following three-layer stack[^le-stack]:

```text
┌─────────────────────────────────────────────┐
│ Loop Layer    : continuation, termination,   │ ← loop engineering (control layer)
│                 and rerun across turns       │
│   Failure mode: keeps pursuing the wrong      │
│                 direction                    │
├─────────────────────────────────────────────┤
│ Context Layer : all information visible to    │ ← context engineering
│                 the model at a given time     │
│   Failure mode: stale/bloated data            │
├─────────────────────────────────────────────┤
│ Prompt Layer  : single-turn instructions      │ ← prompt engineering
│   Failure mode: misunderstanding constraints  │
└─────────────────────────────────────────────┘
loop engineering does not "replace" the two lower layers; it "wraps" them
```

Anthropic itself defines agents succinctly as **"LLMs autonomously using tools in a loop"** and notes that agents running inside a loop continually generate data that may be relevant to later reasoning turns, making **periodic curation of context essential**[^ctx-eng].

**Elevation of the human role**. The human role rises step by step: "write code -> write prompts -> design loops -> build factories that run loops"[^le-def]. The essence of Loop Engineering is the human's move away from continuous intervention and toward **up-front goal specification and guardrail design**. In implementation, the `while` loop body itself is the easy part; the hard parts are **`context` and `stop condition` (termination checks / cost budget / achievement target)**. For production operation, a **governed workspace (identity, scoped permissions, audit trail, fast rollback)** has also been identified as a sixth required element missing from standard frameworks[^le-stack].

### 2.2 Lineage of Agentic Loops: Classical Patterns

Classical agentic loops fall broadly into two families.

**(A) Reasoning-action loops that proceed through observation inside a single episode**

- **ReAct (Reason + Act, Yao et al., ICLR 2023)**: a loop that interleaves `Thought -> Action -> Observation`. Reasoning guides, tracks, and updates action plans and handles exceptions; action connects the model to external knowledge sources such as APIs or environments. It suppresses hallucination and error propagation from chain-of-thought by grounding the model through interaction with the environment. It was evaluated on HotpotQA / Fever / ALFWorld / WebShop, with absolute improvements of +34% on ALFWorld and +10% on WebShop. It is **the direct ancestor of modern tool-in-the-loop agents**[^react].
  > Independent verification: the description matches the original paper abstract almost verbatim ("reasoning traces help the model induce, track, and update action plans as well as handle exceptions" / "actions allow it to interface with ... external sources"). Verdict: **supported**[^v-react].
- **Plan-and-Execute**: an explicit loop in which a planner (LLM) generates a multi-step plan, an executor (another agent or tool) runs each step in isolation, and a re-plan prompt determines after completion whether the work is done or more planning is needed. Its advantages are (1) explicit long-horizon planning and (2) role separation, allowing cost optimization with a stronger model for the planner and a weaker/smaller model for the executor[^plan-exec].

**(B) Reflection loops that improve themselves across multiple attempts**

- **Self-Refine (Madaan et al., NeurIPS 2023)**: a test-time method in which a single LLM acts as generator, feedback provider, and refiner, iterating through `generate -> self-feedback -> refine`. It requires no additional training, teacher data, or RL. It achieved an average absolute improvement of about 20% across seven tasks[^self-refine].
- **Reflexion (Shinn et al., NeurIPS 2023)**: a loop that improves through **linguistic feedback (verbal reinforcement)** rather than weight updates. It uses three roles: Actor (policy LLM), Evaluator (judges trajectory success using another LLM, heuristics, or **external execution such as unit tests**), and Self-Reflection (generates a linguistic summary of failure). It converts environmental rewards into linguistic feedback, stores them in **episodic memory**, and improves the next attempt. It reached 91% HumanEval pass@1, exceeding GPT-4's then-current 80%[^reflexion].

The broader frame that contains both families is the military-origin **OODA loop (Observe -> Orient -> Decide -> Act, John Boyd)**, which maps well to Anthropic's "models using tools in a loop" definition. Schneier and others, however, point out stage-specific security risks: Observe = prompt injection, Orient = context pollution, Decide = reward hacking, Act = action hijacking. They argue for a **security trilemma in which speed, intelligence, and security cannot all be achieved simultaneously**. In loop design, input validation and human gates at each stage are essential[^ooda].

The difference in reflection granularity between **Self-Refine and Reflexion** is important for design: Self-Refine polishes the same output within the same episode as an immediate quality gate, while Reflexion leaves learning in memory across attempts as long-term improvement. As discussed below, claude-org's retro/curate feedback loops can be mapped to the latter.

### 2.3 First-Generation Autonomous Agents and Lessons Learned: AutoGPT / BabyAGI / AgentGPT

The first generation that emerged around April 2023, including AutoGPT / BabyAGI / AgentGPT, all used a naive `while True` loop around "task generation -> execution -> replanning." **Most struggled in practical use, and modern loop-design principles converged as the inverse of those failures**. This is a valuable catalog of mistakes that loop-agent must avoid.

- **BabyAGI (Yohei Nakajima, 2023/4)**: an **approximately 105-line PoC** that simply chained three LLM calls inside `while True`: Execution / Task Creation / Prioritization. It had no task dependencies, no completion criteria, and **no termination condition at all**; the infinite loop was intentional. In addition, the output of its Pinecone vector memory (ada-002 top-5 search) was **never passed into the execution prompt**. The lesson is that a memory mechanism does not work unless it is wired into decision-making[^babyagi].
  > Independent verification: "105 lines," "chain of three LLM calls," and "no termination condition" are all supported by multiple sources ("The loop never terminates. There is no completion condition."). Verdict: **supported**[^v-babyagi].
  > Across nine subsequent generations, BabyAGI gradually acquired dependencies, termination conditions (stop when all tasks are complete), persistence (SQLite knowledge graph), parallelism, context budgeting, and error recovery. The latest BabyAGI 3 is about 33,500 lines[^babyagi].
- **AutoGPT**: it ran goal decomposition and tool execution with GPT-4, but because it lacked termination logic and relied on natural-language evaluation for completion judgments, it biased toward "**more work is always needed**." The root causes were (1) ambiguous completion criteria without measurable metrics, (2) **lack of progress detection** comparing old and new plans, (3) perfectionism bias, and (4) **resource unawareness**, with no API/time/cost tracking and no circuit breaker. Concrete examples include a research spiral that made over 300 API calls and 8 iterations for an AI-history research task without producing a summary, repeatedly reorganizing a Downloads folder more than 15 times, and a 50-step small task that consumed the 8K context limit every time and cost about **$14.40**[^autogpt-fail].
- **AgentGPT (Reworkd)**: a productized browser-based autonomous loop. It used a loop limit as its only defense, suffered crashes/infinite loops when accumulated context exceeded the model window, forgot memory across sessions, and was cloud-only. Its GitHub repository was **archived and made read-only in January 2026**[^agentgpt].

**Core design lessons from the first generation**[^autogpt-fail][^babyagi][^simpler]:

1. Move from **"infinite reprioritization loops" to "finite task graphs with dependencies and explicit termination conditions."**
2. Add **hard limits and circuit breakers** for runaway prevention: iterations, cumulative tokens/cost, and elapsed time.
3. Build **progress detection and duplicate detection** into state management so repeated identical actions can be detected and stopped.
4. Verify not only that memory is retained, but also that it is **wired into decision-making**.
5. Build in **human gates and observability** from the beginning.
6. **`simpler loops win`**: a small number of reliable tools plus good context management beats elaborate multi-stage reasoning.

### 2.4 Production Agent Harness Loops

**Anthropic's design guidance** consistently centers on **starting with the simplest possible configuration and adding complexity only when necessary**, and on **distinguishing workflows (paths fixed by code) from agents (LLMs autonomously decide their own paths)**[^anthropic-bea].
> Independent verification: the phrases "finding the simplest solution possible, and only increasing complexity when needed," Workflows = "orchestrated through predefined code paths," and Agents = "dynamically direct their own processes" were all confirmed verbatim. Verdict: **supported**[^v-bea].

The core of an autonomous agent is "a loop in which an augmented LLM (retrieval/tools/memory) uses tools while receiving environmental feedback." It is decisively important to obtain **ground truth from the environment at every step (tool call results / code execution)**, and it is standard practice to build in stopping conditions such as a maximum number of iterations[^anthropic-bea].

**Claude Agent SDK / Claude Code** implements this loop as **`gather context -> take action -> verify work -> repeat`**. It advances by turns (tool-call round trips) and terminates when a response contains no tool calls. It has **`max_turns` / `max_budget_usd`** to prevent runaway behavior ("Setting a budget is a good default for production agents."). When context approaches the limit, automatic compaction is triggered through `compact_boundary` to summarize and compress it. Termination is distinguished by the `ResultMessage` subtype: `success` / `error_max_turns` / `error_max_budget_usd` / `error_during_execution`[^agent-sdk-loop][^agent-sdk-blog].

**Three verification methods**: rule-based checks (lint/test/typecheck), visual feedback (screenshot), and LLM-as-judge. **The best pattern is to make rules explicit and return which rule failed and why**[^agent-sdk-blog].

**Long-running harnesses** should not be designed to finish everything in one run; they should **progress incrementally while leaving clean state after every session**. Assuming each session has no memory of the previous one, the design separates Initializer and Coding roles, makes the feature list (JSON, pass/fail), progress file, and git history into the **SoT**, and uses tests for self-verification. If not explicit, the agent may mark work complete without testing, so the instruction should say: "Self-verify all features. Only mark as 'passing' after careful testing"[^harness].

**Scheduling has three layers**[^scheduled]:

| Layer | Foundation | Minimum interval | Local files | Use case |
|---|---|---|---|---|
| cloud routines (`/schedule`) | Anthropic infrastructure | 1h | No (fresh clone) | Reliable unattended execution |
| Desktop scheduled task | Resident process on own machine | 1m | Yes | Periodic execution that needs local assets |
| In-session `/loop` (CronCreate/List/Delete) | Open session | 1m | Yes | Simple polling during a session |

`/loop` and Cron features are session-scoped. **Recurring tasks fire one final time seven days after creation and then delete themselves**, bounding how long a forgotten loop can continue running. Jitter prevents API concentration; there is no catch-up; `CLAUDE_CODE_DISABLE_CRON=1` can disable all scheduled tasks. In `/loop` prompt-only mode, Claude dynamically chooses an interval from 1 minute to 1 hour and terminates itself when the task is provably complete[^scheduled].

**Cursor / Devin** also use isomorphic `plan -> execute -> verify -> iterate` loops, using pass/fail from lint/test/type checks as self-correction signals. Cursor advances only after `typecheck && lint` passes, preventing error accumulation. Devin first creates a structured plan of around 50 steps and dynamically replans with full context such as test failures[^cursor-devin].

### 2.5 LoopAgent Syntax in Frameworks

Major frameworks implement loops through two design styles.

**(1) Declarative dedicated loop structures**

- **Google ADK `LoopAgent`**: a **deterministic** workflow agent that repeatedly executes `sub_agents` in sequence. Example: `LoopAgent(name=..., sub_agents=[critic, refiner], max_iterations=5)`. It has two termination paths: **(a) reaching `max_iterations`** and **(b) any sub-agent returning `escalate=True` through the `exit_loop` tool (`tool_context.actions.escalate = True`)**. The official docs clearly state that the **LoopAgent itself does not decide when to stop; the termination mechanism must be implemented by the user**[^adk].
  > Independent verification: repeated sub_agents, determinism, and the two termination paths (max_iterations + escalate) were confirmed in official docs. Verdict: **supported** (minor caveat: "declarative" is less precise than the official "template/deterministic" wording)[^v-adk].
- **AutoGen (v0.4+)**: stops a team (loop) using composable `termination_condition` objects. Conditions can be combined with **OR `|` / AND `&`**, as in `MaxMessageTermination(10) | TextMentionTermination("APPROVE")`. Other condition types include TokenUsage / Timeout / Handoff / External / Functional[^autogen].

**(2) Explicit graphs / state machines**

- **LangGraph**: rather than a dedicated loop structure, it builds cycles on a `StateGraph` using `add_conditional_edges` and recursive edges, or `Command(goto=...)`, with **`recursion_limit` (default 1000 super-steps)** as a safety net. State is passed by having each node update shared state. The docs caution that `recursion_limit` is not the primary control-flow mechanism and is not a substitute for well-designed graph logic[^langgraph].
- **CrewAI**: represents loop-back with the Flows `@router` decorator plus an iteration counter in state. Individual Agents have `max_iter` (default 25). There are known bug reports in which execution did not stop after reaching `max_iter`, which reinforces the need for **multi-layer safety nets**[^crewai].
- **OpenAI Agents SDK**: limits its internal run loop (model call -> tool execution -> handoff -> terminate on final_output) with `max_turns`, and can use `error_handlers` to convert `MaxTurnsExceeded` into a **controlled final output**. Its predecessor Swarm was replaced by the Agents SDK and deprecated in March 2025[^openai-sdk].

**Design principle common to all frameworks**: **separate "semantic termination judgment (LLM/critic/specific text)" from "mechanical limits (count, tokens, time)" and provide both**. ADK calls `max_iterations` a "critical safety net" and combines it with escalate; LangGraph positions `recursion_limit` as runaway prevention while using conditional edges for primary control, and so on[^framework-common].
> Independent verification: official docs confirm that each framework has this dual structure. Verdict: **supported**[^v-framework].

### 2.6 Loop Control and Safety: Operational Best Practices

Operational consensus has converged on **defense in depth**.

- **Termination conditions (multi-layer)**: use (1) natural completion (final response without tool calls) as the main path, (2) maximum iterations as a mandatory safety net, with a practical guideline of **5-10 iterations**, (3) wall-clock timeout, (4) no-progress / repeated-action detection, stopping after **three consecutive infeasible actions / three identical repeated actions / 20 rounds without progress**, and (5) unrecoverable errors[^term][^loop-detect].
- **Convergence judgment**: determine convergence by exceeding an evaluator-rubric threshold, a change measure such as entropy falling below a threshold (plateau), or an iteration limit. AWS's evaluator reflect-refine pattern states: "The loop repeats until the result meets a set of criteria, is approved, or reaches a retry limit"[^converge].
- **Cost control (five layers)**: per-request `max_tokens`, session/daily budget, turn counter such as `MAX_TURNS=25`, circuit breaker, and enforcement at the gateway layer. Estimates show that an unbounded loop can cost $15 in 10 minutes and around $2100/day at 100 concurrent loops per hour. The standard pattern is **automatic throttling plus owner alert when consumption rate exceeds 3x the recent average**[^cost].
- **Human gates (limited)**: restrict them to **irreversible, high-blast-radius actions only**. LangGraph's `interrupt()` pauses the graph inside a node, and human decisions have four forms: **approve / edit / reject / respond**. A checkpointer persists a StateSnapshot at each super-step, making pause/resume safe. The best practice is "interrupt on irreversible, high-blast-radius actions only — not on every step"[^hitl].
  > Independent verification (important correction): the naive claim that human intervention at every step is the standard is **closer to refuted (partly-supported)**. MindStudio explicitly describes human-in-the-loop as "optional for high-stakes scenarios only, not standard practice." Standard termination control is instead defense in depth through natural completion + max iterations + timeout + cost ceiling + loop detection. **Human intervention is not universal standard practice; it is conditional and limited to irreversible actions**[^verify-hitl].
- **Observability**: follow **OpenTelemetry GenAI semantic conventions**, making each LLM call / tool execution / retrieval a child span and recording standard `gen_ai.*` attributes (model, token counts, finish_reason) plus loop iteration number and termination reason. Observability is not just for troubleshooting; it is the **source of feedback loops that continuously learn and improve quality**. OTel compliance is recommended over vendor-specific SDKs[^otel].
- **Pitfalls of self-improving / eval loops**: Reflexion-style loops are the baseline, but repeated iterations can cause **output bloat and degradation** (examples where responses expand to four times their original size after three iterations), **reward hacking** (vague rewards make hedged phrases such as "it depends" technically never wrong and high-scoring), and **memory pollution from toxic feedback** (adversarial environments can inject false lessons). Mitigations are **early stopping, diverse evaluation, regular real-environment tests, and separation of performance measurement from production execution (dual-component)**[^self-improve].
- **LLM-as-judge bias**: position bias and self-preference bias have been demonstrated. **Prefer cheap ground-truth checks first (tests, exact string comparison)**, and limit judge use to rubric-based evaluation with human calibration[^judge].

### 2.7 Distilled Design Principles from the Research

The above findings distill into the following ten design principles for loop-agent.

1. Position **LoopAgent as a control layer that wraps "an LLM autonomously using tools in a loop" with triggers, verifiable goals, and guardrails**. It wraps, rather than replaces, prompt/context mechanisms.
2. Make the **innermost loop `gather -> act -> verify -> repeat`**. Do not create iterations without a verification signal, or ground truth.
3. Require the **dual termination condition of semantic judgment and mechanical limits**, implemented as composable condition objects.
4. Build **hard limits and circuit breakers** for runaway prevention, including iterations, cumulative tokens/cost, and time, as **invariants** of the engine.
5. Use **progress detection and duplicate detection** to detect and stop repeated identical actions and lack of progress.
6. **Do not accumulate state in context; externalize it to an external SoT** such as a feature list, progress file, or state DB. Compaction is a helper, not the SoT.
7. Use **two layers: inner ReAct + outer Reflexion** for single-episode execution and cross-attempt linguistic improvement. Ensure through evals that memory is wired into decision-making.
8. **Limit human gates to irreversible, high-blast-radius actions** using approve/edit/reject/respond, and persist state to make pause/resume safe.
9. Build **observability with OTel GenAI compliance** from the beginning. Emit machine-readable events for each loop stage, termination reason, and cost.
10. **`simpler loops win`**. Separate the PoC, with a roughly 200-line core loop, from production, which handles error recovery, concurrency, and persistence and may be tens of thousands of lines. Introduce capabilities in phases.

---

## 3. Inventory of claude-org-ja Assets: Reuse Assessment

> `/home/happy_ryo/work/org/claude-org-ja` was reviewed **read-only**, and assets reusable for Loop Engineering / LoopAgent were evaluated with concrete file references. Assessment legend: **reuse-as-is** (almost unchanged) / **adapt** (modify and reuse) / **extract-pattern** (extract the design pattern) / **reference-only** (use as reference) / **N-A** (not applicable).

### 3.1 Orchestration Loop: secretary / dispatcher / worker / curator

claude-org implements a four-stage loop: **Secretary -> Dispatcher -> Worker -> Curator**. It has a delegation flow and escalation path: Secretary (human contact) -> Dispatcher (resident monitoring) -> Worker (actual work) -> Curator (on-demand knowledge curation).

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| Handover/Resume pattern | `.claude/skills/{secretary,dispatcher}-{handover,resume}/SKILL.md`, `.dispatcher/references/worker-monitoring.md` | **reuse-as-is** | "State save and restore across turn boundaries" is exactly context management for long-running loops. It is a model for saving termination conditions and resuming. |
| Role Contract (responsibilities/boundaries of four roles) | `docs/contracts/role-contract.md`, each `CLAUDE.md` | **extract-pattern** | Skeleton for "who runs the loop and where responsibility boundaries sit." loop-agent is expected to use three layers: coordinator / loop-agent / eval-agent. |
| Delegation Lifecycle Contract (T1-T9 transitions, E1-E5 errors) | `docs/contracts/delegation-lifecycle-contract.md` | **reuse-as-is** (types) / **reference-only** (details) | Makes task-state termination conditions and error branches explicit. Provides formal types for review-feedback reloops and abort conditions. |
| Dispatcher monitoring loop (`/loop 3m`) | `.dispatcher/references/worker-monitoring.md` | **adapt** | Mechanizes "observe -> judge -> notify." Stall detection gives an operational definition of "no forward progress." |
| On-demand Curator (worker close trigger) | `.claude/skills/org-curate/SKILL.md`, `tools/check_curate_threshold.py` | **adapt** | Automatic trigger for "feedback -> improvement." A non-blocking async lifecycle launched only when a threshold is exceeded. |
| Escalation + pending-decisions register | `.claude/skills/org-escalation/SKILL.md`, `tools/pending_decisions.py` | **reuse-as-is** | Explicit lifecycle for inter-agent escalation and human gates. Ground truth for detecting relay gaps. |

### 3.2 Autonomous Loops / ScheduleWakeup / cron

claude-org implements a **time-driven monitoring loop via `/loop 3m`**, **async on-demand startup through conditional checks when a worker closes**, and **role-specific active poll cadences**. Notably, it explicitly avoids steady cron routines and instead uses event-driven behavior, one-shot checks, state-retention files, and single-flight guarantees.

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| Role-based passive polling cadence | `knowledge/curated/broker-transport.md`, `.dispatcher/references/worker-monitoring.md` | **extract-pattern** | Reentry for autonomous loops in a stateless CLI environment. Asymmetric design: dispatcher 3m / worker bounded / secretary turn-prologue. |
| Deterministic decision tool (exit-code branching) | `tools/check_curate_threshold.py`, `tools/work_discovery_scan.py` | **reuse-as-is** | Side-effect-free computation tool. Returns whether a condition holds through JSON stdout + exit code (0/10/2), delegating judgment to the delivery layer. Idempotent condition evaluation for loops. |
| Resume-safe loop state (cursor + metadata JSON) | `.dispatcher/references/worker-monitoring.md`, `.dispatcher/CLAUDE.md` | **reuse-as-is** | Avoids resume gaps and duplicates with event cursor / idle state / inflight marker. |
| Single-flight / coalesce (duplicate spawn prevention) | `.dispatcher/references/pane-close.md` | **reuse-as-is** | Prevents races when the same trigger fires in short intervals in an event-driven loop. Checks for an existing instance before spawn. |

### 3.3 transport: renga / broker, Push Primary / Pull Fallback

This is a **dual transport layer** for inter-agent messaging and state notifications. Default renga (in-band push) and broker (channel sidecar using approximately one-second claim -> push plus pull fallback) coexist and implement **at-most-once delivery** and tiered structured access control.

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| Transport Abstraction Seam | `tools/transport.py` | **extract-pattern** | Abstracts backend switching with the runtime descriptor as the only SoT. Handles multiple backends backend-agnostically. |
| Peer Message Delivery Bridge (best-effort) | `tools/peer_notify.py` | **reuse-as-is** | "Non-failing" async notification from CLI/background tasks to the main agent. Applicable to interrupt notifications from outside the loop. |
| Push primary / Pull fallback delivery model | `docs/contracts/backend-interface-contract.md`, `docs/operations/broker-dogfood-runbook.md` | **extract-pattern** | Push provides immediate response; pull fallback tolerates backend outages. Core pattern for inter-loop wake delivery. |
| Tier-Gated structured access control | `docs/contracts/backend-interface-contract.md` | **adapt** | Capability constraints based on `auth_role` (immutable). Children can be sandboxed with cap=detached agent based on caller tier at spawn time. |
| Message at-most-once semantics | same as above | **adapt** | drain means consumption is final and no redelivery occurs. The delivery contract assumes idempotent handlers at higher layers. |
| Error code vocabulary (machine-readable) | same as above | **reference-only** | Error-handling discipline using `[<code>] <message>` format and default-branch tolerance. |

### 3.4 State Management with state.db as the SoT

**SQLite `state.db`** is the single SoT for runs / org_sessions / events / worker_dirs / projects / workstreams. Markdown / JSON outputs are derived artifacts automatically regenerated from the DB by the snapshotter, and drift_check detects manual edits. This is the most important asset, directly reusable for persisting loop-agent loop state such as iteration, convergence history, and termination evaluation.

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| state.db schema and SoT definition | `tools/state_db/schema.sql`, `docs/contracts/state-semantics-contract.md`, `docs/org-state-schema.md` | **adapt** | Persist loop state by extending runs columns, such as iteration_count, is_converged, terminated_reason. Journal loop events in events. |
| StateWriter API and Transaction | `tools/state_db/writer.py`, `tools/state_db/__init__.py` | **reuse-as-is** | Atomic updates with `transaction()` plus post-commit hooks for regenerating markdown/JSON. Rollback on exception protects state on failure. |
| Query layer and State Predicates | `tools/state_db/queries.py` | **adapt** | Predicates such as TERMINAL_STATUSES map directly to termination-condition judgment. Add loop-specific predicates. |
| Journal Events Catalog (50+ types) | `docs/journal-events.md`, `tools/journal_append.{sh,py}` | **adapt** | Journal every loop cycle step: loop_cycle_begin/convergence_detected/termination_triggered. Complete audit trail. |
| WAL Journal Mode and concurrent access | `tools/state_db/__init__.py` | **reuse-as-is** | WAL + busy_timeout allows concurrent readers such as dashboards/observers to coexist with the loop writer. Enables observability. |
| Snapshotter (post-commit regeneration) | `tools/state_db/snapshotter.py` | **adapt** | Atomic human-readable dumps of loop observations to `.state/loop-state.md`. |
| State Semantics Contract (7 statuses, 4 predicates) | `docs/contracts/state-semantics-contract.md` | **reference-only** | Reference for designing the loop finite state machine, such as OBSERVING/THINKING/ACTING/CONVERGING/TERMINATED. |

### 3.5 work-discovery / triage

Autonomous **work-discovery** scans and triages the issue tracker, then proposes "next work candidates (N items + one recommendation)" to a human. It has a **two-layer structure: computation layer (read-only deterministic tool) and delivery layer (skill / dispatcher)**. This increases discovery autonomy while keeping the decision to start work behind a human gate. It corresponds to the Loop Engineering loop that selects what to iterate on next.

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| work_discovery_scan.py (computation layer) | `tools/work_discovery_scan.py` | **reuse-as-is** | Read-only, side-effect-free, same input -> same output. Multiple launch paths share the same tool. Input selection that does not affect loop state. |
| work-discovery-triage design (two layers / invariants / phased rollout) | `docs/design/work-discovery-triage.md` | **reference-only** | INV-1 through INV-5, especially "increase discovery autonomy while leaving judgment behind a human gate," directly support LoopAgent's human-centered design. |
| Connecting completion to the next iteration (post-merge / pane-close trigger) | `.claude/skills/org-pull-request/SKILL.md`, `.dispatcher/references/pane-close.md` | **extract-pattern** | Trigger point that detects when work becomes idle and automatically proposes the next candidate. Stops at proposal, preserving the human gate. |

### 3.6 Feedback Loops: org-delegate / org-retro / org-curate / knowledge

These implement the full **Delegation -> Retro -> Curate -> Skill adoption** cycle. The flow is: delegate -> retrospect on the delegation process after completion across five dimensions -> structure knowledge as raw/curated with four elements (fact/judgment/evidence/applicability) -> run skill-eligibility-check using five scored signals -> launch skill-audit when pending >= 5. This is a **two-layer design: automated flow without humans + human decision gate**. It directly maps to LoopAgent's self-improving / eval loop, and specifically to Reflexion's cross-attempt memory.

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| org-retro (five-dimension retrospective + skillization judgment) | `.claude/skills/org-retro/SKILL.md` | **adapt** | Evaluates the process itself after multi-turn execution and records improvements. Automatic extraction of agent patterns, equivalent to an eval-loop cycle. |
| org-curate (raw -> curated integration, threshold-triggered on-demand startup) | `.claude/skills/org-curate/SKILL.md`, `references/knowledge-standards.md` | **adapt** | Observation accumulation -> integration -> pattern extraction. Preserves immutable raw records with move-then-mark. |
| Knowledge four-element format (fact/judgment/evidence/applicability) | `org-curate/references/knowledge-standards.md` | **reuse-as-is** | Standard recording format for agent reasoning traces. Converts three or more similar pieces of knowledge into a pattern. |
| skill-candidates (status machine + batch gate, N=5) | `knowledge/skill-candidates.md` | **reuse-as-is** | Promotes pattern recommendations to skills through a human gate. Optimizes cognitive load through thresholded batch decisions. |
| work-skill template (standard format, origin record) | `org-retro/references/work-skill-template.md` | **reuse-as-is** | Traceability when turning a pattern into a skill; the genesis can be followed. |
| 15 curated knowledge files (delegation/broker-transport/codex, etc.) | `knowledge/curated/*.md` | **reference-only** | Preemptive sharing of failure modes and mitigations likely to appear in loop implementation, transferred as conceptual patterns. |

### 3.7 Observation, Human Gates, and Runaway Prevention: attention / pr-watch / escalation / suspend

Five integrated skills implement worker observation, escalation for judgment, a pending_decisions register, an event DB, and state preservation. They can be applied directly to LoopAgent observability, human gates, and runaway detection.

| Asset | file | Assessment | Significance for the loop |
|---|---|---|---|
| org-attention-start/stop (OS notification watcher) | `.claude/skills/org-attention-{start,stop}/SKILL.md` | **adapt** | Actively detects approval waits, CI failures, and unexpected states with notification sounds. Uses a pane_id sidecar for duplicate-start prevention and orphan detection. Loop observation layer. |
| pr-watch-pane / pr_watch.py (external event monitoring) | `.claude/skills/pr-watch-pane/SKILL.md`, `tools/pr_watch.py` | **adapt**/**extract-pattern** | Idempotent spawn + identity verification + timeout -> escalation for long-running watchers such as CI/merge/webhook waiting. Deterministic exit code. |
| org-escalation (three-layer record: register + journal + markdown) | `.claude/skills/org-escalation/SKILL.md` | **reuse-as-is** | Escalates judgment requests and runaway detection to humans. Implements the autonomy boundary between what the agent may decide and what requires approval. |
| pending_decisions.py (human gate state machine) | `tools/pending_decisions.py`, `tests/test_pending_decisions.py` | **reuse-as-is** | append -> resolve(to_user) -> user reply -> resolve(to_worker). Deterministically detects forgotten relays. |
| org-suspend (graceful/force two-pass shutdown) | `.claude/skills/org-suspend/SKILL.md` | **adapt** | Deterministic capture of all agent state plus two-pass close. Loop suspend/checkpoint. |
| journal_append (canonical event log) | `tools/journal_append.{py,sh}`, `docs/journal-events.md` | **reuse-as-is** | Records all loop lifecycle events in the canonical log. Foundation for observability. |

### 3.8 Summary of Reuse Policy

Mapping research principles (§2.7) to assets (§3.1-3.7):

- **State SoT (Principle 6)** <- `tools/state_db/` (**most important; reuse-as-is/adapt**). Directly reusable for persisting loop iterations, convergence history, and termination evaluation.
- **Dual termination conditions + hard limits (Principles 3, 4)** <- state-semantics / delegation-lifecycle contract (reference the types) + predicates in `state_db.queries` (adapt).
- **Wake delivery and notification (Principles 2, 8)** <- transport (push primary/pull fallback, at-most-once) as **extract-pattern**.
- **self-improving (Principle 7)** <- org-retro/curate/knowledge as **adapt**, corresponding to Reflexion's cross-attempt memory.
- **Observation (Principle 9)** <- journal_append + attention-watcher (reuse-as-is/adapt). OTel GenAI mapping is new work.
- **Human gates (Principle 8)** <- org-escalation + pending_decisions (**reuse-as-is**).
- **Input selection (next iteration target)** <- work-discovery two-layer separation (adapt).

**Largest finding**: the capabilities loop-agent needs, including saving and resuming termination conditions, authoritative state, feedback-to-improvement, and observation-to-human-gate, **already exist in production-quality form in claude-org**. The shortest path is not to reinvent them, but to **extract and reuse them gradually as runtime-independent abstractions: state DB / transport / feedback / gate**.

---

## 4. LoopAgent Design

### 4.1 Requirements and Design Principles

Based on the research in §2 and the assets in §3, loop-agent's LoopAgent should satisfy the following requirements:

- **R1 Verifiable-goal driven**: avoid natural-language "completion judgments" and require measurable completion criteria such as tests green / lint / state-transition convergence / checklist.
- **R2 Dual termination conditions**: implement semantic judgment (critic/eval) and mechanical limits (iterations, tokens, time) independently and make them composable.
- **R3 Runaway-prevention invariants**: build per-call max_tokens, session/daily budget, turn counter, circuit breaker, and no-progress detection into the engine.
- **R4 External state SoT**: externalize loop state into a DB to enable resume, observation, and audit.
- **R5 Two-layer loop**: inner ReAct (execution) / outer Reflexion (cross-attempt improvement). Ensure through evals that memory is wired into decision-making.
- **R6 Limited human gates**: interrupt only irreversible, high-blast-radius actions with approve/edit/reject/respond.
- **R7 Observability**: emit OTel GenAI-compliant structured events at each loop stage.
- **R8 simpler loops win**: separate PoC and production, introducing capabilities in phases.

### 4.2 Comparison of Architecture Options

This report compares three options. Evaluation axes are **implementation cost / runaway resistance / observability / org-asset reuse / fit to scope**.

#### Option A: Single-Process Inline LoopAgent (Agent SDK Wrapper)

Thinly wrap the Claude Agent SDK agent loop (`gather -> act -> verify -> repeat`) with `max_turns` / `max_budget_usd` and a simple stop condition. State is a progress file plus git.

- **Pros**: minimal implementation ("simpler loops win"). Fully aligned with Anthropic's standard loop. Can start immediately.
- **Cons**: weak cross-attempt memory, convergence judgment, observability, and human gates. Insufficient for multi-agent coordination or long-running autonomy. Limited use of org assets.

#### Option B: Full Orchestration Model (Closely Following claude-org in Multi-Pane Form)

Reinterpret the four-pane secretary/dispatcher/worker/curator structure as loop coordinator/loop-agent/eval-agent and so on, adopting renga/broker transport, state.db, and all feedback loops wholesale.

- **Pros**: maximum reuse of org assets. Production-quality observability, human gates, and feedback are already present.
- **Cons**: **heavy**. Large runtime dependencies such as pane/tmux/renga/broker are excessive for a standalone loop-agent project. Human gates tend to be too organization-operation-centric. Poor fit for a PoC.

#### Option C: Single Control Layer + Shared State Machine + Phased Org Asset Integration (**Recommended**)

Use a LangGraph-like **state machine, with shared state placed in a `state.db`-equivalent SoT**, as the control layer. Run the **inner ReAct loop + outer Reflexion loop** within it. Termination conditions are **composable condition objects**: MaxIterations / TokenBudget / Timeout / GoalMet / NoProgress / HumanGate. Extract and integrate org assets gradually as **runtime-independent abstractions: state DB / transport / feedback / gate**. Use subagents for context isolation only when needed.

- **Pros**: most naturally satisfies all principles in §2.7. High reuse of state.db, feedback, gate, and transport while keeping runtime dependencies such as pane/tmux **loosely coupled**. Scales continuously under one architecture from PoC (equivalent to Option A) to full system (assets equivalent to Option B).
- **Cons**: higher initial design cost than Option A for the state machine and condition composition.

#### Comparison Table

| Evaluation axis | Option A (inline) | Option B (full orchestration) | Option C (control layer + state machine) ★ Recommended |
|---|---|---|---|
| Implementation cost (initial) | ◎ minimal | △ high | ○ medium |
| Runaway resistance | △ limits only | ◎ | ◎ dual termination + invariants |
| Observability | △ | ◎ | ○ -> ◎ (OTel + journal) |
| Org asset reuse | △ limited | ◎ maximum, but runtime-coupled | ◎ maximum as abstractions, loosely coupled |
| Cross-attempt learning (Reflexion) | △ | ○ | ◎ first-class feature |
| Fit to scope | PoC | organization operations | Continuous coverage from PoC to full system |
| runtime dependency (pane/tmux/renga) | low | high | low to medium (phased) |

### 4.3 Recommendation and Rationale

**This report recommends Option C.**

**Rationale**:

1. Option C is the only option that naturally satisfies all industry-standard patterns shown by the research under a **single architecture**: **dual termination conditions**[^framework-common], **inner ReAct + outer Reflexion**[^react][^reflexion], **external state SoT**[^harness], **composable conditions**[^autogen], and **limited human gates**[^hitl][^verify-hitl].
2. claude-org's most important asset, `state.db` (SoT, transactions, post-commit snapshots, WAL concurrent access), fits **almost directly** into Option C's shared state machine (§3.4).
3. **Continuity from PoC to full system**: Option A can be implemented as the minimal form of Option C, namely a one-node state machine plus hard limits only. Production capability can then be reached by gradually adding assets. No architectural rewrite is required, preserving both "simpler loops win" and phased adoption.
4. Runtime dependencies such as pane/tmux/renga/broker can be loosely coupled through the **transport abstraction seam** pattern in `tools/transport.py`, avoiding Option B's weight while still allowing production-quality notification and human gates to be added later.

**Reasons for rejecting the other options**: Option A structurally lacks cross-attempt learning, observability, and human gates, so it would need to be rebuilt for production. Option B is over-coupled to runtime for a standalone project and is not appropriate for a PoC.

### 4.4 Core Loop Structure

Recommended core control flow for LoopAgent (pseudocode; design skeleton, not implementation):

```text
LoopAgent.run(goal, guardrails):
  state = StateDB.load_or_init(run_id)          # R4: external SoT. Supports resume
  conditions = compose(                          # R2/R3: dual termination conditions (composable objects)
      GoalMet(verifier),                         #   semantic: verifiable goal (test/lint/rubric)
      MaxIterations(n), TokenBudget(b), Timeout(t),  # mechanical: hard limits (invariants)
      NoProgress(window=N, repeat=3),            #   no-progress/repetition detection
      HumanGate(on=irreversible_actions))        # R6: irreversible operations only
  emit(otel, "loop_begin", state)               # R7: observation

  while not conditions.any_triggered(state):
    # -- Inner: ReAct episode (gather -> act -> verify) ----------
    ctx   = curate_context(state)               # context engineering (reconstruct history from SoT)
    act   = model.decide(goal, ctx)             # Thought -> Action
    if act.is_irreversible and HumanGate.active:
        decision = human_gate(act)              # approve/edit/reject/respond (state persistence)
        if decision.rejected: state.record(decision); continue
    obs   = execute(act)                         # Action -> Observation
    signal = verify(obs)                         # R1: ground truth (test/lint/exit-code)
    state.append_step(act, obs, signal)          # R4: transaction + journal event
    emit(otel, "loop_step", {act, signal, cost})

    # -- Outer: Reflexion (linguistic self-improvement across attempts) ----------
    if episode_ended(signal):
        reflection = reflect(state.trajectory, signal)   # failure -> linguistic guidance
        state.memory.append(reflection)          # R5: episodic memory (wired into next ctx)

  reason = conditions.first_triggered(state)
  emit(otel, "loop_end", {reason, state.metrics})
  return finalize(state, reason)                 # graceful: reaching a limit is control output, not an exception
```

Key points:

- **Aggregate termination conditions in the while guard** and perform **graceful termination with a reason** when any condition fires, following OpenAI's error_handlers pattern[^openai-sdk].
- **Do not create a step without verify** (Principle 2). Verification should first use cheap rules such as test/lint, and LLM-as-judge should be limited; principle: prefer ground truth[^judge].
- **Run reflection only at episode boundaries**, and prevent bloat/degradation with iteration limits and "stop when improvement plateaus"[^self-improve].
- **Persist state in a transaction on every step**, providing the single basis for resume, observation, and audit.

### 4.5 Loop Control: Termination, Convergence, Budget, Human Gates, Runaway, Observation

| Control | Design | Source asset / research |
|---|---|---|
| **Termination conditions** | Evaluate GoalMet (semantic) + MaxIterations/TokenBudget/Timeout (mechanical) + NoProgress as OR over composable objects | §2.5, §2.6 / state-semantics contract |
| **Convergence judgment** | evaluator rubric threshold exceeded, score improvement below threshold (plateau), or iteration limit | §2.6 (AWS reflect-refine) |
| **Cost control** | per-call max_tokens + session/daily budget + turn counter + circuit breaker. Record cumulative usage in state.db so 3x spike detection can be added later | §2.6 / state_db |
| **Human gate** | Interrupt only irreversible, high-blast-radius actions. Four decisions: approve/edit/reject/respond. Persist state for pause/resume | §2.6 / org-escalation + pending_decisions |
| **Runaway prevention** | Stop after no-progress N or three repeated actions + hard limits + global stop switch equivalent to `CLAUDE_CODE_DISABLE_CRON` | §2.3, §2.4 |
| **Observability** | OTel GenAI spans (`gen_ai.*` + iteration number + termination reason) + journal_append events + attention watcher integration | §2.6 / journal_append + attention |

### 4.6 Policy for Using org Assets: Mapping Table

| LoopAgent component | org asset to adopt | Extraction form | Phase |
|---|---|---|---|
| SoT for shared state machine | `tools/state_db/` (schema + StateWriter + queries + snapshotter + WAL) | adapt as runtime-independent library, adding loop columns/events | MVP |
| Termination-condition and state types | state-semantics / delegation-lifecycle contract | reference; create new Loop State Semantics | MVP |
| Wake delivery and notification | transport (push primary/pull fallback, at-most-once), peer_notify | pattern extraction + transport seam | Full |
| self-improving | org-retro / org-curate / knowledge (four elements, threshold-triggered, skill-candidates) | adapt and connect to Reflexion memory + eval loop | Full |
| Observation and runaway detection | journal_append, attention-watcher, pr_watch | reuse-as-is / adapt + add OTel | MVP -> Full |
| Human gate | org-escalation + pending_decisions (state machine) | reuse-as-is, reinterpret roles | MVP |
| Input selection for next iteration | work-discovery (two-layer separation of computation layer and delivery layer, deterministic tool) | adapt computation layer; design new delivery layer | Full |
| State save and resume | handover/resume pattern, resume-safe loop state | reuse-as-is | MVP |

---

## 5. Phased Roadmap: PoC -> MVP -> Full System

### Phase 1: PoC — "Minimal Loop + Hard Limits"

- **Goal**: demonstrate that the minimal form of Option C, a one-node state machine, can run `gather -> act -> verify -> repeat` and **reliably stop on mechanical limits**.
- **Scope**: single agent, single process. One verification type, such as tests green. Termination uses only MaxIterations + TokenBudget + Timeout. State is minimal, such as a progress file or lightweight SQLite. No human gate; restrict to tasks that do not emit irreversible actions.
- **Assets used**: none to minimal, such as deterministic exit-code tool conventions and Agent SDK `max_turns`/`max_budget_usd`.
- **Success criteria**: (a) natural termination when the verifiable goal is met; (b) guaranteed stop on limits even if the goal is unmet; (c) sandbox confirmation that AutoGPT-style runaway behavior, such as infinite loops or cost explosion, is not reproduced.
- **Risk**: expanding scope too early breaks "simpler loops win." Restrict to one task type and one verify method.

### Phase 2: MVP — "State Machine + state.db SoT + Dual Termination Conditions + Observation"

- **Goal**: extend the PoC into the Option C skeleton. **Externalize state into state.db** and provide a practical loop with **resume, observability, dual termination conditions, and limited human gates**.
- **Scope**: establish inner ReAct loop. Convert termination conditions into composable objects: GoalMet + mechanical limits + NoProgress. Structure observation as journal events + OTel spans. Introduce a human gate through org-escalation + pending_decisions, limited to irreversible actions. Add state save/resume through handover/resume.
- **Assets used**: `tools/state_db/` (adapt: add loop columns/events), state/delegation contract (reference), journal_append (reuse), org-escalation + pending_decisions (reuse), handover/resume (reuse).
- **Success criteria**: (a) interrupt -> resume works without state loss; (b) all termination reasons remain in the journal for later analysis; (c) human gate fires for irreversible operations and approve/reject is reflected; (d) NoProgress detection can stop cycles.
- **Risk**: coupling when extracting state.db as runtime-independent. First extract a minimal schema for loops and keep it loosely coupled to the org body.

### Phase 3: Full System — "Autonomous LoopAgent Integrating Feedback Loops + transport + Input Selection"

- **Goal**: integrate the outer Reflexion loop, self-improving, wake delivery, and autonomous selection of the next iteration target into a LoopAgent that **runs long-duration, multi-task work autonomously**.
- **Scope**: connect outer Reflexion, or cross-attempt episodic memory, to org-retro/curate/knowledge with finite eval -> reflect -> re-evaluate cycles. Use transport (push primary/pull fallback) to duplicate wake/notification delivery. Use work-discovery to propose next iteration targets while maintaining the human gate. Use subagents for parallel subtasks with isolated context. Add dashboards for OTel observation and automatic throttling on 3x spikes.
- **Assets used**: transport + peer_notify (extract-pattern), org-retro/curate/knowledge (adapt), work-discovery (adapt), attention-watcher/pr_watch (adapt), suspend (adapt).
- **Success criteria**: (a) learning from failed trajectories is wired into the next loop's context and improvement is confirmed through eval; (b) delivery continues through pull fallback even when the backend is unavailable; (c) runaway behavior triggers automatic throttling plus human alert; (d) completion -> next iteration connection runs autonomously through the human gate.
- **Risk**: self-improving traps: output bloat, reward hacking, and memory pollution. Make **early stopping, diverse evaluation, separation of performance measurement from production execution (dual-component), and validation before memory ingestion** mandatory invariants[^self-improve].

```text
Phase 1 (PoC)      Phase 2 (MVP)              Phase 3 (Full)
─────────────      ─────────────              ──────────────
minimal loop    -> state machine+state.db SoT -> +Reflexion/feedback
hard limits        dual termination+observ.      +transport(wake)
(Option A equiv.)  limited human gate/resume      +work-discovery(input selection)
                   (Option C skeleton)            (Option C+full org assets/Option B-grade robustness)
```

---

## 6. Risks and Open Questions

- **Safety of self-improving**: reflection output bloat/degradation, reward hacking, and memory pollution, including false-lesson injection in adversarial environments[^self-improve][^ooda]. -> In Phase 3, make dual-component separation, early stopping, and validation before ingestion invariants.
- **Reliability of LLM-as-judge**: position/self-preference bias[^judge]. -> Use ground-truth verification (test/lint/string match) first, and limit judge use to rubric + calibration.
- **Extraction level of state.db**: coupling with the org body. -> Extract a minimal schema for loops and keep it loosely coupled. SQLite can be swapped, but transactional SoT properties must be preserved.
- **Runtime dependency of transport**: broker sidecars and similar pieces belong to the runtime and cannot be reused directly[^framework-common]. -> Extract only the patterns: push primary/pull fallback, at-most-once, role-specific cadence. Implement the actual delivery mechanism separately on the loop-agent side.
- **Fluidity of the concept**: Loop Engineering is a rapidly evolving practitioner concept as of 2026. -> Anchor design decisions in official Anthropic docs and the research-paper lineage, treating blog claims as secondary information, as this report does.
- **governed workspace**: production autonomy requires identity, scoped permissions, audit trails, and fast rollback as a sixth element[^le-stack]. -> From Phase 2 onward, include audit logs (journal) and rollback in the design.

---

## 7. Appendix

### 7.1 Glossary

- **ground truth**: objective verification signal obtained from the environment at each step, such as tool execution results or test/lint exit codes.
- **dual termination conditions**: a design that independently combines semantic judgment (critic/eval/specific text) and mechanical limits (iterations, tokens, time).
- **at-most-once**: a delivery guarantee where a message is consumed once drained and is not redelivered. The receiver is assumed to use an idempotent handler.
- **SoT (Source of Truth)**: the single authoritative copy of state. In claude-org, this is `state.db`; derived artifacts such as markdown/JSON are automatically regenerated.
- **Reflexion-style outer loop**: a loop that converts failure across attempts into linguistic feedback, stores it in episodic memory, and improves the next attempt.
- **escalate signal**: a loosely coupled termination protocol, derived from ADK, through which a sub-agent notifies the loop control layer to stop via shared state/events.

### 7.2 Main Sources

Main sources used in the research, corresponding to footnotes for each claim. Loop Engineering blogs are treated as practitioner-originated secondary information, while official Anthropic docs and arXiv papers are treated as primary anchors.

[^le-def]: Definition of Loop Engineering (goal-based automation / trigger + verifiable goal). https://www.mindstudio.ai/blog/what-is-loop-engineering-ai-coding-agents , https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/
[^le-origin]: Origin of the term (spread of Boris Cherny's comments, Addy Osmani / Peter Steinberger). https://www.productmarketfit.tech/p/stop-prompting-ai-and-start-building , https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/
[^le-stack]: Three-layer prompt -> context -> loop stack and governed workspace. https://www.puppyone.ai/en/blog/what-is-loop-engineering-5-building-blocks-missing-one
[^ctx-eng]: Anthropic, "Effective context engineering for AI agents" (agents = LLMs using tools in a loop). https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
[^oracle-loop]: Oracle, "What is the AI agent loop." https://blogs.oracle.com/developers/what-is-the-ai-agent-loop-the-core-architecture-behind-autonomous-ai-systems
[^react]: ReAct (Yao et al., ICLR 2023, arXiv:2210.03629). https://arxiv.org/abs/2210.03629 , https://react-lm.github.io/
[^reflexion]: Reflexion (Shinn et al., NeurIPS 2023, arXiv:2303.11366). https://arxiv.org/abs/2303.11366
[^self-refine]: Self-Refine (Madaan et al., NeurIPS 2023, arXiv:2303.17651). https://arxiv.org/abs/2303.17651
[^plan-exec]: Plan-and-Execute (LangChain/LangGraph). https://www.langchain.com/blog/planning-agents
[^ooda]: OODA loop and security trilemma (Schneier). https://www.schneier.com/blog/archives/2025/10/agentic-ais-ooda-loop-problem.html
[^babyagi]: BabyAGI lineage (105-line PoC, no termination condition, nine-generation evolution). https://babyagi.wiki/ , https://yoheinakajima.com/birth-of-babyagi/
[^autogpt-fail]: AutoGPT failure case study. https://github.com/vectara/awesome-agent-failures/blob/main/docs/case-studies/autogpt-planning-failures.md , https://en.wikipedia.org/wiki/AutoGPT
[^agentgpt]: AgentGPT (Reworkd). https://www.datacamp.com/tutorial/agentgpt , https://github.com/reworkd/agentgpt
[^simpler]: "simpler loops win" / notorious agent loops. https://techtalkwithsriks.medium.com/notorious-agent-loops-c4cc05b859b5 , https://www.ibm.com/think/topics/babyagi
[^anthropic-bea]: Anthropic, "Building Effective Agents." https://www.anthropic.com/engineering/building-effective-agents , https://www.anthropic.com/research/building-effective-agents
[^agent-sdk-loop]: Claude Agent SDK agent loop (gather -> act -> verify -> repeat, max_turns/max_budget_usd). https://code.claude.com/docs/en/agent-sdk/agent-loop
[^agent-sdk-blog]: Building agents with the Claude Agent SDK (three verification methods). https://claude.com/blog/building-agents-with-the-claude-agent-sdk
[^harness]: Anthropic, "Effective harnesses for long-running agents." https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
[^scheduled]: Claude Code scheduled tasks (three layers: cloud/Desktop//loop, seven-day expiration). https://code.claude.com/docs/en/scheduled-tasks
[^cursor-devin]: Autonomous loops in Cursor / Devin. https://cursor.com/blog/agent-best-practices , https://cognition.ai/blog/devin-annual-performance-review-2025
[^adk]: Google ADK LoopAgent. https://adk.dev/agents/workflow-agents/loop-agents/ , https://google.github.io/adk-docs/agents/workflow-agents/loop-agents/
[^autogen]: AutoGen termination conditions. https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/tutorial/termination.html
[^langgraph]: LangGraph graph API / recursion_limit. https://docs.langchain.com/oss/python/langgraph/graph-api
[^crewai]: CrewAI Flows / max_iter. https://docs.crewai.com/en/concepts/flows , https://github.com/crewAIInc/crewAI/issues/3847
[^openai-sdk]: OpenAI Agents SDK running agents (max_turns/error_handlers). https://openai.github.io/openai-agents-python/running_agents/
[^framework-common]: Dual termination structure common to frameworks. https://adk.dev/agents/workflow-agents/loop-agents/ , https://rajatpandit.com/ai-engineering/optimizing-langgraph-cycles/
[^term]: Termination strategies (natural completion / max iterations / goal achievement / error, 5-10 iteration guideline). https://www.mindstudio.ai/blog/what-is-an-agentic-loop-ai-coding-agents
[^loop-detect]: Preventing infinite conversations (thresholds for no-progress/repetition detection). https://dev.to/alessandro_pignati/stop-the-loop-how-to-prevent-infinite-conversations-in-your-ai-agents-ekj
[^converge]: AWS evaluator reflect-refine loop. https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-patterns/evaluator-reflect-refine-loop-patterns.html
[^cost]: Measures against runaway agent costs. https://relayplane.com/blog/agent-runaway-costs-2026 , https://www.truefoundry.com/blog/rate-limiting-ai-agents-preventing-llm-api-exhaustion
[^hitl]: LangGraph human-in-the-loop (interrupt, approve/edit/reject/respond). https://docs.langchain.com/oss/python/langchain/human-in-the-loop
[^otel]: OpenTelemetry GenAI observability. https://opentelemetry.io/blog/2025/ai-agent-observability/ , https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions
[^self-improve]: Pitfalls and mitigations for self-improving agents. https://www.buildmvpfast.com/blog/ai-agent-self-improvement-recursive-accuracy-production-2026 , https://datagrid.com/blog/7-tips-build-self-improving-ai-agents-feedback-loops
[^judge]: LLM-as-judge bias. https://arxiv.org/abs/2406.07791 , https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
[^v-le-def]: Independent verification (Loop Engineering definition, supported). https://smartscope.blog/en/generative-ai/methodology/loop-engineering-agent-loops-2026/ , https://www.firecrawl.dev/blog/loop-engineering
[^v-react]: Independent verification (ReAct, supported, verbatim match to original paper). https://arxiv.org/abs/2210.03629 , https://www.promptingguide.ai/techniques/react
[^v-babyagi]: Independent verification (BabyAGI 105 lines, no termination condition, supported). https://babyagi.wiki/ , https://github.com/yoheinakajima/babyagi
[^v-bea]: Independent verification (Anthropic minimal configuration + workflow/agent distinction, supported). https://www.anthropic.com/research/building-effective-agents
[^v-adk]: Independent verification (ADK LoopAgent two termination paths, supported). https://adk.dev/agents/workflow-agents/loop-agents/
[^v-framework]: Independent verification (dual termination common to frameworks, supported). https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/tutorial/termination.html
[^verify-hitl]: Independent verification (human gates are limited to irreversible operations, not standard for every step; correction of partly-supported claim). https://www.mindstudio.ai/blog/how-to-build-agentic-loop-claude-code , https://stevekinney.com/writing/agent-loops

---

*This report is the deliverable for the research and design phase of loop-agent (v1.0, 2026-06-27). The investigation was conducted through an ultracode workflow fan-out (19 agents: 7 subsystems for org asset inventory + 6 web-research subquestions + 6 independent adversarial verification tasks). Claims are supported by the sources above, and major claims were adversarially verified by independent agents.*
