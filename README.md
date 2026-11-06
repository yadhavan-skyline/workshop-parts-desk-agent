# Workshop Parts Desk - Agentic AI agent (HackMatrix 1.0, Team F60)

An AI agent that reads natural-language parts requests, spends a limited tool budget on the most useful
checks, and either dispatches a compatible in-stock part or escalates to a human - never a wrong part.

**Result:** 100/100 official validation before and after the twist, 0 unsafe dispatches. Final rank: 4th.

## Folders
- `agent/` - `solution.py` (final v3.3 controller, Python 3.12 standard library) and the submitted ZIP
- `gateway/` - Cloudflare Worker code that connects the agent to Groq / Gemini (v3 = final, v1 = first)
- `docs/` - team guide and presentation (PDF)
- `tests/` - gateway speed / stress / debug scripts
- `results/` - local test results through the real gateway

## How it works
1. Read the latest approved revision of every request and part.
2. A rule-based parser and the LLM both extract voltage, connector and watt range; trusted only if they agree.
3. Second LLM opinion on disagreement, accepted only if consistent with the text.
4. Expected-value planner picks inspect / verify / clarify calls (max 13 units).
5. Safety gate checks voltage, connector and watt range before every dispatch.

## Secrets
No API keys or tokens are stored here. Gateway keys were Cloudflare secrets (`GEMINI_KEY`, `GROQ_KEY`, `GATEWAY_TOKEN`).
