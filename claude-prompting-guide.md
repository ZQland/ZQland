# The Claude Prompting Guide

Built from Anthropic's **Code w/ Claude 2026** sessions and Anthropic's official *Prompting best practices* docs.

**Where each part comes from**
- 🎤 **Talk**: "Tool, skill, or subagent? Decomposing an agent that outgrew its prompt" (Will Steuk, Anthropic). It's a hands-on workshop: you take over a 402-line inventory agent, break it apart live on Claude Managed Agents, and run evals after every change. Other Code w/ Claude talks that recaps describe are marked too.
- 📘 **Docs**: Anthropic's official prompting guide (platform.claude.com → *Prompting best practices*).
- 🏗️ **Field guide**: Anthropic's article *The anatomy of effective commerce agents* (claude.com), which covers the same tool/skill/subagent question with production data.

> I couldn't play the X video itself, so this guide is built from the session's official description, published recaps, and Anthropic's own written guidance on the same topics. Where a detail comes only from a third-party recap, it says so.

---

## Part 1 — The mindset

### 1.1 Treat Claude like a brilliant new hire 📘
- Claude is smart but knows nothing about your norms, goals, or workflow. Spell them out.
- **Colleague test:** if a coworker with no background would be confused by your prompt, Claude will be too.

### 1.2 Instructions don't add capability 🎤 (Margot van Laar's talk, per a Forbes recap)
- Telling Claude to "try harder" or "be accurate" doesn't make it better at math, lookups, or verification. **Give it a tool instead** (a calculator, search, or a test runner).
- Keep **role, rules, data, and task** in separate sections of the prompt.
- **Delete outdated or overfitted instructions.** Old workarounds written for weaker models now cause bad behavior.

### 1.3 Prompting is empirical 🎤📘
- Write it, test it, look at the failures, change one thing, and test again.
- Without evals you're guessing. With evals you can see which behaviors **flip** after each change. This was the core method of the decomposition workshop (see Part 7).

---

## Part 2 — Writing the prompt

### 2.1 Be clear and direct 📘
- Say exactly what output you want, its format, and its limits.
- When order or completeness matters, use **numbered steps**.
- If you want more than the minimum, **ask for it**. Claude doesn't guess at ambition.
  - ❌ `Create an analytics dashboard`
  - ✅ `Create an analytics dashboard. Include as many relevant features and interactions as possible. Go beyond the basics to create a fully-featured implementation.`

### 2.2 Explain *why* (context and motivation) 📘
- A rule with a reason works better than a bare rule, because Claude can **generalize from the reason**.
  - ❌ `NEVER use ellipses`
  - ✅ `Your response will be read aloud by a text-to-speech engine, so never use ellipses since it won't know how to pronounce them.`

### 2.3 Use examples (few-shot) 📘
- Examples are the most reliable way to steer format, tone, and structure.
- Make them:
  - **Relevant**: close to your real use case.
  - **Diverse**: include edge cases, so Claude doesn't copy surface patterns.
  - **Wrapped**: `<example>` for one, `<examples>` for several.
- **3–5 examples** works best. You can ask Claude to critique your examples or write more of them.
- Examples also work with thinking. Show the **problem → method → answer**.

### 2.4 Structure with XML tags 📘
- Put each kind of content in its own tag: `<instructions>`, `<context>`, `<input>`, `<documents>`.
- Use the same descriptive tag names every time, and nest them when there's a natural hierarchy (`<documents><document index="1">…`).

### 2.5 Give Claude a role 📘
- Even one sentence in the system prompt helps: `You are a helpful coding assistant specializing in Python.`

### 2.6 Long documents (20k+ tokens) 📘
- Put **long data at the top** and the **question at the end**. In tests this improved quality by up to 30%.
- Wrap each document with metadata:
  ```xml
  <documents>
    <document index="1">
      <source>annual_report.pdf</source>
      <document_content>{{REPORT}}</document_content>
    </document>
  </documents>
  ```
- **Ground the answer in quotes.** Ask Claude to pull relevant quotes into `<quotes>` first, then answer from those quotes in `<info>`.

### 2.7 Model identity (for apps) 📘
- `The assistant is Claude, created by Anthropic. The current model is Claude Opus 5.5.`
- If your app picks a model, give it the exact model string.

---

## Part 3 — Controlling the output

### 3.1 Formatting 📘
1. **Say what to do, not what to avoid.** ❌ "No markdown" → ✅ "Write in smoothly flowing prose paragraphs."
2. **Use XML output tags**, for example `<smoothly_flowing_prose_paragraphs>`.
3. **Match your prompt's style to the output you want.** A prompt full of markdown gets markdown back.
4. **Give detailed rules** when you care. For example, specify prose instead of bullets, lists only for truly separate items, and headings and code blocks only.

### 3.2 Length and verbosity 📘
- Recent models are direct and concise and may skip the summary after tool use. If you want one, ask: `After completing a task that involves tool use, provide a quick summary of the work you've done.`
- Some models, like Opus 5, run long. Ask for conciseness explicitly, because changing effort won't reliably fix length.

### 3.3 Plain math 📘
- Claude defaults to LaTeX. For plain text: `Do not use LaTeX… use "/" for division, "*" for multiplication, "^" for exponents.`

### 3.4 No more prefill 📘
Prefilling the last assistant turn now returns an error on Claude 4.6 and later. Use these instead:

| You used to prefill to… | Do this now |
|---|---|
| Force JSON/YAML | Structured outputs, or ask for the schema plus retries. For classification, use a tool with an `enum`. |
| Skip "Here is…" | `Respond directly without preamble.` Or use XML tags or tools, and strip any leftover preamble in code. |
| Avoid refusals | A clear user message is enough now. |
| Continue cut-off text | In the user turn: `Your previous response was interrupted and ended with […]. Continue from where you left off.` |
| Re-inject context | Put the reminder in the user turn, hydrate it through tools, or use compaction. |

---

## Part 4 — Tools

### 4.1 Act or suggest: say which 📘
- `Can you suggest changes…` → Claude only suggests.
- `Change this function…` → Claude makes the change.
- System-prompt switches:
  - **Proactive:** `<default_to_action>` means implement by default, and use tools to find missing details instead of guessing.
  - **Cautious:** `<do_not_act_before_instructions>` means research and recommend unless explicitly told to edit.

### 4.2 Don't shout 📘
- Newer models follow the system prompt closely. `CRITICAL: You MUST use this tool` now causes **overuse**. Write `Use this tool when…` instead.
- Remove `If in doubt, use [tool]`. Swap blanket defaults for targeted ones: `Use [tool] when it would enhance your understanding.`

### 4.3 Parallel tool calls 📘
- Claude already runs independent calls in parallel. You can push this to nearly 100% with a `<use_parallel_tool_calls>` block: run independent calls together, run dependent ones in order, and never guess parameters.
- To slow it down: `Execute operations sequentially…`

### 4.4 Designing good tools 🏗️
- **Wrap your real systems.** Don't reimplement their logic inside the tool.
- **Return lean results**: only the fields Claude reasons over. Drop noise like image URLs.
- **Reshape results inside the tool**, and add the next step when it isn't obvious.
- **Errors should say what to do next**, not just return a bare code.
- When domain logic starts piling up in a tool, **move it into one backend endpoint**.
- Use typed tools for UI components, not custom tags your code has to parse.
- Safety: the model **proposes** changes, and your system **approves and applies** them. Accept only server-issued IDs, and clean third-party text before it reaches the model.

---

## Part 5 — Thinking and effort

### 5.1 Adaptive thinking + effort 📘
- Current models decide for themselves when and how much to think. Control depth with the **`effort`** setting (`budget_tokens` is deprecated and returns an error on 4.7 and later).
- **General beats prescriptive.** "Think thoroughly" often outperforms a hand-written step-by-step plan.
- Steer reflection: `After receiving tool results, carefully reflect on their quality and determine optimal next steps before proceeding.`
- Less thinking: `Thinking adds latency… When in doubt, respond directly.` Or lower the effort.
- Stop flip-flopping: `Choose an approach and commit to it… You can always course-correct later.`

### 5.2 Self-check 📘
- `Before you finish, verify your answer against [criteria].` This catches errors in code and math.
- Exception: Opus 5 already verifies well on its own. Remove this instruction there, or you'll get over-verification.

### 5.3 Prompt chaining 📘
- Use separate calls when you need to inspect intermediate steps. The classic chain is **draft → review against criteria → refine**.

---

## Part 6 — Tool, skill, or subagent? 🎤 (the core of Will Steuk's talk)

### 6.1 The problem: the agent that outgrew its prompt
- The workshop agent ("StockPilot", a fictional inventory agent according to a recap) grew to a **402-line system prompt** with many tools and subagents. Its eval score **fell from about 83% to about 62%** (figures from a third-party recap).
- **Root causes found** (from the recap):
  1. **Tool gaps**: the agent was asked to do things it had no tool for.
  2. **Output-structure mismatches**: what one piece returned didn't match what the next piece expected.
  3. **Contradictions in the system prompt**: rules added over time that fight each other.
- **Method:** use Claude Code to sort the failing evals, find the root cause, refactor one piece, **re-run the evals**, and see what flipped. Repeat.
- A recap reports the system prompt shrank from **~400 lines to ~15**, and that moving a query-generation tool **up to the parent agent** removed confusing subagent behavior. These figures weren't checked against the recording.

### 6.2 Definitions
| | What it is | Lives where | Cost |
|---|---|---|---|
| **Inline prompt** | Instructions in the system prompt | Always in context | Free per call, but crowds the prompt |
| **Tool** | One action Claude executes (search, calculate, write) | A function call | Cheap |
| **Skill** | Packaged, reusable *know-how* for a multi-step job, loaded on demand | Loaded into the **same** agent, which keeps full history | Low (one model turn to load) |
| **Subagent** | A separate agent with its **own fresh context**, which returns a summary | Isolated context window | Highest (cold start, many more tokens, more latency) |

### 6.3 The decision framework
1. **Is it a deterministic action or lookup?** → **Tool.** Don't write prompt text telling Claude how to compute something a function can do.
2. **Is it critical everywhere (safety, legal, brand, key user facts)?** → **System prompt**, always.
3. **Is it relevant to about ⅓ or more of requests?** → **System prompt.**
4. **Is it domain know-how needed only sometimes?** → **Skill**, loaded on demand. (A single agent with skills beat both one giant prompt and one-subagent-per-domain in Anthropic's enterprise comparisons.)
   - If you can predict the skill from a known signal (like the page the user came from), **inject it before the first call**.
   - Load skills as **tool results**, not by editing the system prompt, so caching keeps working.
5. **Is it a narrow, self-contained job that needs its own big context and should return only a short answer** (like deep research, parallel independent work, or an independent review)? → **Subagent.**
6. **Should another agent own the whole conversation** (for example, its own compliance rules)? → Only then **hand off**. Otherwise keep the orchestrator in charge.

### 6.4 Subagent pitfalls 🏗️📘
- Handoffs **lose state**, cost **several times more tokens**, and add **seconds of latency**. Domains also overlap, so per-domain agents duplicate access or hand off mid-task.
- Recent models over-spawn subagents, for example to explore code when one `grep` would do. To damp this: `Use subagents when tasks can run in parallel, require isolated context, or involve independent workstreams… For simple tasks, sequential operations, single-file edits… work directly.`
- Give subagent tools clear descriptions, then let Claude decide when to delegate.
- Testing note: a subagent starts with a fresh context, so removing a skill from the main agent tells you nothing about what the subagent does with it.

---

## Part 7 — Evals: how you know it worked 🎤🏗️
- **Run evals after every change.** That's the workshop's method.
- **Grade the final state, not the path.** Set up the state, add the user message, let the agent run, and check the end state and output, including the arguments of the last write.
- **Simulated users** are good for *finding* cases. Turn each one into a fixed snapshot test.
- **Include messy starts**: long, contradictory, or dirty histories.
- **Every positive case gets a negative twin**: should act vs. should ask, should serve vs. should refuse.
- Cover core requests, context-dependent requests, safety and prompt injection, interface behavior (timeouts, empty results), and requests that use several capabilities at once.
- **Start with 50–100 cases per user flow**, built with domain experts and from real incidents.
- **Test across models and effort levels.** Measure **cost per completed task**, not cost per call.
- **CI:** run core cases, all safety cases, and the cases for whatever changed. Run the **full suite nightly**. Gate on pass rate over several trials.
- **Roll out to a small group first**, and keep a switch that turns off a single skill without a deploy.
- Other related sessions: "Evals for subjective, stateful agents" (Descript) and "Evals for Taste: Hill-Climbing a Slide-Generation Agent" (Anthropic).

---

## Part 8 — Long-running agents

### 8.1 Context limits 📘
- Tell Claude about compaction: `Your context window will be automatically compacted… do not stop tasks early due to token budget concerns… save progress and state to memory before the window refreshes.`

### 8.2 Work that spans several context windows 📘
1. Use a **different first prompt** that sets up the framework: tests and setup scripts.
2. Track **tests in a structured file** (`tests.json`). Add: `It is unacceptable to remove or edit tests.`
3. Create **convenience scripts** (`init.sh`) to start servers, tests, and linters.
4. **A fresh window often beats compaction.** Tell Claude to start with `pwd`, read `progress.txt`, `tests.json`, and `git log`, and run one integration test before adding anything new.
5. Give it **verification tools** (a browser or computer-use tool) so it can check UI work itself.
6. `Spend your entire output context… don't run out with significant uncommitted work.`

### 8.3 State management 📘
- **JSON** for structured state, **free text** for progress notes, **git** for history and checkpoints. Push for **incremental progress**.

### 8.4 Memory 🏗️ (also see the "Agents that remember" and "Memory and dreaming" sessions)
- Keep a small always-on set in context, pre-fetch facts from known signals, and put the rest behind a lookup tool.
- Write memory **asynchronously** with a separate extractor that reads only user and assistant text, never tool results.

### 8.5 Caching 🏗️
- Order context from **most stable to most volatile**: system prompt and tools (byte-identical, cache breakpoint), then session and history, then timestamps and the current page **at the very end**.
- A timestamp at the top of the prompt silently breaks the cache. Aim for **90–99% cache hits**. Cached reads cost about one tenth as much.
- Keep the history **append-only**. Editing earlier turns or rebuilding `system`/`tools` invalidates thinking blocks and the cache. Put changes in mid-conversation system messages instead.

---

## Part 9 — Coding-agent snippets 📘
| Problem | Add this |
|---|---|
| Risky actions | Ask before destructive, hard-to-reverse, or publicly visible actions (`rm -rf`, `push --force`, PR comments). Never bypass checks (`--no-verify`). |
| Overengineering | `Only make changes that are directly requested… no extra features, docstrings, defensive code, or one-off abstractions.` |
| Gaming tests | `Implement a general solution… don't hard-code test values… tell me if tests are wrong.` |
| Hallucinating code | `<investigate_before_answering>`: never speculate about code you haven't opened. |
| Leftover scratch files | `Clean up temporary files at the end of the task.` |
| Research tasks | Give success criteria, ask Claude to verify across sources, and have it track competing hypotheses with confidence levels in a notes file. |
| Generic-looking frontends | Ask for distinctive typography and color, motion, and atmospheric backgrounds, and avoid Inter or Roboto and purple-on-white gradients. |
| Vision | Give Claude a **crop or zoom tool**. It measurably improves image tasks. |

---

## Part 10 — Upgrading old prompts to new models 📘
1. Describe exactly the behavior you want.
2. Add quality modifiers ("go beyond the basics").
3. Ask for features like animations and interactivity explicitly.
4. Replace `budget_tokens` with **adaptive thinking + effort**.
5. Remove prefill (see 3.4).
6. **Tone down anti-laziness prompting.** New models are proactive, so old "be thorough, always use tools" text makes them overdo it.
7. Pass thinking blocks back unchanged and keep the history append-only.
8. Read the **model-specific page** for your model. Each one lists quirks (for example, Opus 5's verbosity, or Fable 5.1 giving fewer progress updates).

---

## One-page cheat sheet
1. Clear, specific, with the **why**.
2. 3–5 diverse examples in `<example>` tags.
3. XML tags to separate role, rules, data, and task.
4. Long data first, question last, quotes before answers.
5. Say what *to* do. Match your prompt style to the output you want.
6. "Change X" when you want action, not "can you suggest".
7. Calm language. No CRITICAL/MUST.
8. Capability comes from **tools**, not "try harder".
9. Always-relevant → prompt · deterministic → tool · sometimes-needed know-how → **skill** · isolated or parallel big job → **subagent**.
10. Keep the system prompt small. Delete stale and contradictory rules.
11. Evals after every change. Grade outcomes, add negative twins.
12. Control depth with effort, not budgets. Keep history append-only and cache-friendly.

---

### Sources
- Session page: https://claude.com/code-with-claude/session/ldn-ext-tool-skill-or-subagent-decomposing-an-agent-that-outgrew-its-prompt
- Agendas: https://claude.com/code-with-claude/san-francisco-extended · https://claude.com/code-with-claude/london-extended
- Prompting best practices: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
- Commerce agents: https://claude.com/resources/articles/the-anatomy-of-effective-commerce-agents
- Third-party recaps (not opened, seen in search results only): frontiermodels.cc session summary; youcanbuildthings.substack.com; Forbes (Jodie Cook, July 2026) on Margot van Laar's talk.
