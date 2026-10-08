# Discovery development

The discovery agent is an optional background job. The dashboard remains usable when discovery dependencies are missing. Gemini is accessed through `langchain-google-genai`. Agent orchestration uses LangChain's `create_agent`, backed by the LangGraph runtime installed with LangChain. There is no hand-written ReAct loop or custom reasoner protocol.

| Module | Responsibility |
| --- | --- |
| `discovery/models.py` | Pydantic contracts, local range and URL validation |
| `discovery/network.py` | Active IPv4 interfaces, DNS-SD hints, cancellable Nmap TCP scan |
| `discovery/probe.py` | Pinned-IP HTTP transport, same-origin scope, bounded reads, redaction |
| `discovery/provider.py` | Standard LangChain Gemini model configuration |
| `discovery/prompts.py` | Explicit Reason → Act → Observe → Finish instructions |
| `discovery/tools.py` | `@tool` functions with Pydantic argument schemas |
| `discovery/agent.py` | `create_agent` construction, `ainvoke` and finding extraction |
| `discovery/middleware.py` | LangChain safety hooks: budgets, valid calls, progress, cancellation and errors |
| `discovery/verification.py` | Evidence references, identity checks and fresh verification |
| `discovery/budget.py` | Per-service and per-run limits |
| `discovery/storage.py` | Review memory, diagnostics and idempotent panel upserts |
| `discovery/runner.py` | Background job, cancellation and progress |
| `dashboard/agent_panel.py` | Start/stop controls, review and report export |
| `dashboard/persistence.py` | Shared file locking and atomic JSON replacement |
| `devtools/fake_service/` | Local HTTP fixture with selectable scenarios |

## Decision flow

After the initial HTTP observation, `create_agent` orchestrates model calls, tool execution and `ToolMessage` history. Gemini chooses `fetch_service` or `finish_service`; safety middleware validates the call before LangChain executes it. Gemini may propose any service name, but it cannot directly write the service file or declare a result verified. `finish_service` invokes the local verifier and uses `return_direct=True` to end the graph without an unnecessary final model call.

Automatic approval currently requires a matching HTML panel title and a different successful JSON resource identifying the same product. Generic `product`, `application`, or `name` metadata supports products outside a fixed catalog. There are additional identity-schema checks for Proxmox's version response and AdGuard Home's status response. The model must cite at least two actual successful observations with exact quotes. Both verification resources are fetched again before approval. Unsupported formats or authentication barriers go to review.

These are network fingerprint checks, not cryptographic identity checks. A faithful fake can pass them. A second model opinion is not an authenticity guarantee.

The action interface accepts only exact observed same-origin links or a small set of read-only identity paths. Query strings, credentials, external origins and obvious mutation paths are rejected. DNS is resolved once to a local IPv4 address and the HTTP transport pins that address; TLS SNI and the Host header retain the original hostname. Redirects become observations for another bounded step rather than automatic requests. No shell tool or arbitrary LLM-generated command is exposed.

## Learning the LangChain integration

Start with the actual agent construction in `discovery/agent.py`:

```python
from langchain.agents import create_agent

agent = create_agent(
    model=model,
    tools=tools,
    system_prompt=REACT_SYSTEM_PROMPT,
    middleware=[guard],
    name="glassscout_discovery",
)

result = await agent.ainvoke({"messages": [...]}, config={"recursion_limit": ...})
```

The full `langchain` package supplies the agent and middleware. `langchain-google-genai` supplies `ChatGoogleGenerativeAI`, and `langchain-core` supplies the underlying messages and model contracts. Install all dependencies with `python -m pip install -r requirements.txt`.

1. In `discovery/models.py`, `FetchServiceInput` and `FinishServiceInput` define allowed arguments with Pydantic. Unknown fields are rejected.
2. In `discovery/tools.py`, `from langchain.tools import tool` and `@tool(..., args_schema=...)` turn async Python functions into LangChain tools. Their docstrings explain their purposes to Gemini. The trusted `Probe` and budgets are captured locally, not supplied by the model.
3. In `discovery/provider.py`, `create_gemini_model` returns a normal `ChatGoogleGenerativeAI` instance. `create_agent` handles binding the tools and calling the model; there is no custom `decide` adapter.
4. In `discovery/middleware.py`, `DiscoveryGuard(AgentMiddleware)` adds policy through `abefore_model`, `awrap_model_call`, `aafter_model` and `awrap_tool_call`. These hooks add context, account for budgets, reject invalid batches before execution, and watch for lack of progress. `@hook_config(can_jump_to=["model"])` enables a bounded repair turn. The framework handles routing and preserves valid messages, including Gemini thought signatures.
5. A tool returns model-readable content plus a local `artifact` (`Observation` or `Finding`). Local artifacts are not sent as message content. Only independently verified findings can be auto-added by the persistence layer.

Invalid arguments, unknown tools and multiple calls in one turn are rejected without executing them and count toward the no-progress limit. Full message history and tool schemas count toward the input-character budget. Each service gets its own graph and middleware instance; no chat memory is shared between targets. A graph recursion limit provides an additional emergency bound. Low-level HTTP, scanning and persistence remain ordinary Python infrastructure; LangChain owns the complete agent reasoning/tool loop.

### ReAct: Reason + Act

`create_agent` owns the ReAct loop with native tool calls. `discovery/prompts.py` instructs the model to assess observations, choose one useful check, wait for its actual result, and then continue or finish. We do not parse a text-based `Thought/Action/Observation` transcript or implement a second execution loop.

Each tool requires a nonblank `summary` of at most 240 characters: a short user-facing explanation of the action, not private chain-of-thought. Middleware emits `react_reason`, `react_act`, and `react_observe` events around actual framework tool execution. Finishing emits `react_finish`; invalid calls and exhausted limits emit `react_rejected` and `react_stop`. Observations are built from actual tool artifacts, not model claims. The finish observation records the independent verifier's decision.

The dashboard displays the latest 30 ReAct events under **Discovery activity & review**, with endpoint, step and timestamp. Events use the existing local, redacted, 500-entry diagnostic history and are included in exported reports. Raw model message content and private reasoning are not logged in this timeline. Summaries are model-written explanations, not proof of identity or a guarantee of correct reasoning. The transport, evidence verifier, budgets and user review remain authoritative.

Offline tests exercise the real LangChain graph with a model double whose next action changes when an actual `ToolMessage` contains HTTP 404. Tests also check phase order, rejected batches, redaction and timeline rendering. Live Gemini behavior still needs a manual API smoke test.

### Why Google SDK AFC is disabled

Automatic Function Calling (AFC) is the Google SDK's own function-execution loop. LangChain already executes tools under local safety limits, so model middleware supplies `automatic_function_calling={"disable": True}` through `request.override(model_settings=...)`. This disables SDK auto-execution, **not** Gemini's ability to request a tool. `tool_choice="any"` still requests a native tool call.

The warning recommending `AsyncChat.send_message` instead of `AsyncModels.generate_content` concerns applications using the SDK's AFC loop. Our application does not need that loop, so it keeps the LangChain model interface and explicitly disables AFC instead of hiding the warning or switching to a second agent loop.

## Budgets

Defaults are 6 model calls, 10 HTTP requests and 90 seconds per service; 60 model calls, 600,000 input characters, 64 endpoint candidates and 10 minutes per run. Each model response has a 2048-token output cap. Input characters are an application budget, not an estimate of Google's billable tokens. Provider retries are disabled; a provider failure saves evidence and pauses the run. Schema failures and repeated actions stop after two steps without progress. Two HTTP requests are reserved for fresh verification.

Requests read at most 64 KiB and keep at most 6000 characters of extracted text. Socket timeouts and a request deadline bound slow responses. Cancellation is checked between steps and while reading. An in-flight local request may take up to its remaining timeout to exit; Nmap is terminated separately.

The TCP subprocess uses a fixed argument list, never a shell. `-Pn` considers all selected addresses instead of excluding hosts that ignore ICMP, and the configured port range defaults to all TCP ports. The scan receives 40% of the run budget. XML for completed hosts survives cancellation; hosts without complete records may be absent in partial scans.

## Persistence and identity

`Manage services → Modify` applies an ID-keyed update under the same lock as discovery, keeping unrelated cards and background additions. Editing cannot change a card's ID or resurrect a concurrently removed card. Endpoint widget state is synchronized before rendering on the next full rerun.

The live progress fragment refreshes every second. `JobManager.snapshot()` calculates remaining run and TCP-scan time from the same monotonic deadlines enforced by the worker; it does not rewrite the report on each tick. Completed, stopped and restarted jobs do not show a live countdown. Timeout bars measure elapsed time budgets, not scan coverage; the identification bar uses actual processed candidate counts. Old reports without timing fields remain readable.

UI changes and discovery writes share a filesystem lock. Services are written with an atomic replace, and UI edits operate on the latest saved list to preserve concurrent additions. Corrupt saved data is not overwritten by discovery.

The canonical panel URL is the deduplication key, supplemented by an existing finding's saved service ID. Presentation edited by users is preserved on automatic updates. The agent does not guess that two differently addressed services are identical just because their product names match; address changes without a known correspondence require review. History lives in ignored `data/discovery/state.json`; the latest 500 diagnostic events are retained.

Review decisions are matched against a stable fingerprint of observed identities, paths, statuses and the proposed product. Changing timestamps or dynamic page text does not resurface rejected items. Material identity changes can bring a candidate back for review.

## Tests

From the project root, after installing the requirements:

```bash
python -m unittest discover -s tests -v
```

The service-board browser regression test requires Node.js and `playwright`:

```bash
node tests/service_board_browser.cjs
```

Use `NODE_PATH` for an external Playwright installation and `CHROME_PATH` for an installed Chrome executable if needed. The test uses an isolated iframe with fixture data, without starting discovery or changing saved services. It checks stable iframe sizing, repeated render messages, recovery from a collapsed frame, and desktop/mobile group panels.

Tests use a scripted `BaseChatModel` inside the real `create_agent` graph, so they need no Gemini key and incur no API charges. Offline tests run the production HTTP extraction, LangChain tool execution and middleware, verifier, background job and persistence with a simulated transport. An SDK-boundary test runs the full agent and real Gemini integration with a mocked HTTP response and checks that tool declarations remain enabled while SDK AFC is disabled. A separate live-localhost test runs a real fixture server and skips only if the environment forbids binding a socket.

A real Gemini smoke test is manual: run `python -m devtools.fake_service --port 0`, paste the printed URL into the sidebar with no subnets selected, then run discovery. Repeat with the conflict and unknown scenarios. This checks the chosen model's behavior and quota as well as SDK connectivity.
