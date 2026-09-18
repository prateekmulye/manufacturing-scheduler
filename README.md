# Manufacturing Scheduler

Plan a small set of manufacturing orders against machine calendars, operation sequences and deadlines. Compare an input-order baseline with a constraint-solved schedule, inspect the result, then export a planning proposal.

This is an independent project using synthetic or public inputs. It does not connect to SAP, MES or employer systems, and accepting a proposal does not dispatch work. Public hosting verification is still in progress.

## Run locally

Use Python 3.13. From this directory:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B server.py
```

Open **http://127.0.0.1:54186**. Use this address rather than `localhost`; requests must match the configured origin. A health check returns `{"status": "ok"}`:

```sh
curl --fail http://127.0.0.1:54186/health
```

Choose **Order sequence comparison**, validate the inputs, then select **Solve schedule**. Inspect the baseline and checked proposal, accept or reject it, and download the JSON. The editor and JSON import support the same versioned format as [`examples/`](examples/).

AI is optional for local development. Manual rule changes, solving and exports work without a model.

## What runs where

```text
Browser editor → validation → supervised OR-Tools CP-SAT process
                              ↓
                    independent assignment checker → human review → JSON

Optional AI → proposed rule diff → validation → human apply → solve again
```

- **Python and OR-Tools** enforce resource availability, nonoverlapping operations, route order, releases and hard deadlines. The objective minimizes total tardiness first, then makespan. A separate checker recomputes constraints and metrics from the returned assignments.
- **AI** proposes up to four supported rule edits or explains checked results. It does not solve the schedule, approve changes or establish optimality. Exact request quotations must support each edit; invalid or unsupported output is rejected.
- **Browser UI** uses plain JavaScript, HTML and CSS. Revisions and input hashes prevent stale responses from replacing current work. Acceptance is a planning decision stored in the browser workspace.
- **Hosted boundary** uses HAProxy on port 8080 and Waitress on loopback port 8081. One application process owns sessions and one solver child; this design does not support multiple replicas.

Supported AI requests use exact IDs and integer minutes, such as `Block M from minute 0 to minute 60.` Other supported changes set an order's release, soft due date or hard deadline, or remove its deadline. Staffing, material planning, priorities and open-ended scheduling instructions are unsupported.

## AI configuration

Default `AI_PROVIDER=local` expects Ollama at `127.0.0.1:11439` with `qwen3.5:9b`, digest `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`. A missing or different model reports unavailable; the application neither downloads models nor falls back remotely.

Hosted inference uses these **server-side** settings:

| Variable | Value |
|---|---|
| `AI_PROVIDER` | `workers-ai` |
| `AI_GATEWAY_URL` | `https://ai.prateekmulye.dev/v1/infer` |
| `AI_GATEWAY_SECRET` | Operator-provisioned scheduler credential; never put it in browser code |
| `PUBLIC_ORIGIN` | `https://scheduler.prateekmulye.dev` for the supplied container |

The private gateway fixes the model to `@cf/qwen/qwen3-30b-a3b-fp8`. Visitors do not supply keys. Its shared daily allowance can run out; AI then reports unavailable while manual planning remains usable. A configured model indicator is not a successful inference check.

## Data and limits

Use non-sensitive synthetic or public planning data. The application has no input database or content logs. Inputs and results exist in browser/server memory. Server sessions expire after 30 minutes of inactivity; server jobs expire 60 seconds after creation. Reloading clears the browser workspace and restarting the service loses server state. Download before leaving. Reset clears owned session work, not downloaded files, model buffers or operating-system memory.

Hosted AI actions send the planning request and relevant inputs or checked facts to Cloudflare Workers AI. Application storage limits do not establish the provider's retention policy.

| Boundary | Limit |
|---|---|
| Scenario | 20 orders, 60 operations, 10 resources, 200 calendar intervals |
| Horizon | 10,080 integer minutes, or 7 days |
| Input | 1 MiB; only the version 1 schema and declared fields |
| Solver | Up to 30 seconds for startup, then a 10-second solve budget plus 3-second watchdog grace; cleanup uses at most two 1-second joins |
| Service | 32 sessions, 16 retained jobs, one active solve and one AI action |

The browser stops waiting at 45 seconds, preserves inputs and permits retry. These are configured timeout bounds, not a guaranteed completion time; native hosting performance remains unverified.

Operations use one fixed resource and must fit entirely within one availability interval. They cannot span breaks. Capacity limits do not promise completion or optimality on every admitted input.

## Checks and recovery

```sh
.venv/bin/python -B -m unittest discover -s tests -v
node static/app.check.mjs
```

Checks cover constraints, independent result validation, malformed inputs, ownership, stale revisions and UI transitions. Offline model doubles test contracts, not live-model accuracy.

If a result expires, solve again. If inputs change, validate them again before solving. If AI fails or rejects a request, use the manual rule form. A busy response means the bounded service is occupied; retry after the current operation finishes.
