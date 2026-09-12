# Auto Watch (watchdog) — audit

Date: 2026-09-11. Scope: `watchdog/app.py`, `watchdog/config.yml`,
`watchdog/templates/index.html`, `src/watchdog/{policy,settings,transport,messages,expectations,probes,diagnose}.py`,
and the app's entry in `apps.yml`. Read at commit `0b9e772`, including the
uncommitted diagnose/latency work in the tree.

Findings are ranked by expected loss, in the style of
[OPEN_DEFECTS.md](OPEN_DEFECTS.md). IDs are `W*` so they don't collide with
the existing `N*`/`O*`/`R*`/`D*` registers.

## Status (2026-09-11)

**Fixed:** W1, W2, W3, W4, W5, W6, W7, W8, W9, W13, W24, plus the three chart
data bugs recorded under "Charts" below. Regression tests in
`tests/test_watchdog_app.py` (24) and `tests/test_watchdog_safety_path.py` (13);
`watchdog/app.py` went from 0 tests to covered on every path listed here.

**Still open:** W10 (dead `campaign` category — needs the analyzer to publish
campaign-lifecycle events, so it is a separate change with its own event-contract
implications), W11, W12, W14, W15, W17, W18, W19, W20, W21, W22, W23, W25.

---

## Charts — why some plots were blank or wrong

Four separate causes, none of them in Plotly:

| What | Cause | Fix |
|---|---|---|
| **All four charts blank on a beamline PC** | `index.html` loaded Plotly from `cdnjs.cloudflare.com`, while `analysis/` and `assistant/` vendor their own copy under `static/vendor/`. With no outbound internet — or a proxy that blocks it — `Plotly` was undefined, the first `Plotly.react()` threw, and `startSSE`'s `try/catch` logged it as a non-fatal "SSE parse error". The rest of the page worked, so only the plots looked broken. | New `/vendor/<name>` route serves the copy already in the repo (no fourth 4.4 MB duplicate), redirecting to the CDN only if no local copy is found. The chart areas now say so explicitly when the library is missing. |
| **Stage latency: "average" permanently read "no data yet"** | `average/app.py` records `input_files=[folder]` — the Reduction *directory*, not the frames it consumed. A directory's mtime changes whenever anything lands in it, so `latency = out_mtime - dir_mtime` came out ~0 or negative and the `0 <= latency < 24h` sanity window discarded it. | `_input_mtime()` resolves a directory input to the newest `.dat` inside it that predates the output — the frame whose arrival completed the batch. |
| **Run outcomes: a row of points sitting on 0 nm, in arbitrary order** | `"size": res.get("diameter", 0) or 0` turned a failed fit's `None` into a real 0 nm datapoint, which reads as "the loop is making 0 nm particles". And the sort key was `results["seq"]`, which the analyzer never writes (see its summary dict), so every run sorted equal. | Sizeless fits are omitted server-side; runs are ordered by the manifest's `updated_at`; hover now shows recipe, diameter, PDI and confidence. An empty set renders "no completed fits yet" instead of empty axes. |
| **Pipeline funnel: "Analysed" could exceed "Subtracted"** | `len(manifest["analyses"])` counts the Data Analysis app's guinier/porod/peak/model records too, not just the analyzer's fits. | Counts distinct `file_path` among `type == "nanoparticle"` records only. |

Two further chart robustness fixes: `responsive: true` plus a debounced
`window.resize` handler (`redrawCharts` was only wired to `switchView`, so
crossing the 1400 px one-column breakpoint left every plot at its old width),
and a `safeReact()` wrapper so one failing trace no longer prevents every
chart *after* it in `updateCharts()` from drawing — which is why plots failed
in groups rather than individually.

---

## The shape of it

The five pure modules are in good order: policy, settings, expectations,
diagnose and transport carry **81 tests** between them, the
safety-cannot-be-silenced invariant is genuinely enforced in three
independent places (`load_settings`, `save_notify_settings`,
`should_send`), the webhook URL never touches `config.yml`, and the new
Layer-2 AI reading is properly fenced — advisory-only, append-only, `None`
on any failure, off by default.

`watchdog/app.py` has **no test file at all**, and that is where almost
every finding below lives. Two themes recur:

1. **Auto Watch is the most expensive process on the machine it is
   monitoring** (W1). It recomputes everything from disk, once a second,
   per open browser tab.
2. **Auto Watch fails open and fails quiet.** A malformed config sends
   everything (W3); no project folder sends everything (W4); a formatting
   error on an E-stop sends nothing (W5); a failed delivery is reported to
   the operator as a success (W7). For the one app whose whole job is to
   tell you when something is wrong, each of these is a defect in kind, not
   just in degree.

---

## HIGH

| ID | What | Where | Consequence / fix |
|---|---|---|---|
| **W1** | **The SSE stream recomputes every metric from disk at 1 Hz, per client.** `stream_metrics.generate()` calls `_compute_metrics()` each tick, which calls `_read_manifest()` (full `json.loads` of the whole manifest), `_stage_latency()` (for each of 3 stages, a pass over **every** `manifest["files"]` entry with a `stat()` on the output and on each `input_files` entry, plus a fresh `list(_recent_events)` scan per entry via `_duration_ms_for`), and `_throughput_last_24h()` (glob + `stat()` every `.dat` in both `Reduction/` folders). | `watchdog/app.py:1172-1181`, `:742-750`, `:161-218`, `:289-312` | At 20 000 reduced files this is tens of thousands of `stat()` calls and megabytes of JSON parsing **per second, per open tab** — no cache, no shared tick. By hour six the monitoring app is starving reduction and the analyzer it exists to watch. Same class as N7 and O3. **Fix:** one background thread computes the snapshot on a 5 s cadence into a cache (the `_probe_cache` pattern is already in this file); every SSE client and `/api/metrics` reads the cached dict. Index the manifest pass by `(path, mtime, size)` while there. |
| **W2** | **Stall detection is blind to any stall that began before Auto Watch started.** `_recent_events` is memory-only and empty at boot, and `which_stage_is_overdue` returns `None` on an empty list. | `watchdog/app.py:57`, `:928-934`, `src/watchdog/expectations.py:53-54` | The one time an operator restarts Auto Watch is when something is already wrong — and a stalled pipeline emits no events, so the window never refills. The stall is never reported. **Fix:** seed `_recent_events` from `manifest["events"]` at boot and on `set_project`. That is already a persisted rolling 100-event window with the same `type`/`timestamp` shape (`src/manifest.py:593-606`), so this is a read, not a new contract. Directly analogous to the analyzer re-fit bug fixed in `0b9e772`. |
| **W3** | **A malformed `config.yml` fails OPEN.** `_load_config` catches `ValidationError`, logs it, and returns a fallback with `slack_enabled: True` and every category `True`. | `watchdog/app.py:891-900` | `src/watchdog/settings.py`'s own docstring promises "a malformed config fails loudly at load time, not silently at 3 a.m." — it logs one line, then overrides the operator's `slack_enabled: false` and their category choices and floods Slack. For a master OFF switch the safe failure is closed. **Fix:** keep the last known-good settings if there are any, otherwise fall back to `slack_enabled: False`, and surface the parse error in the UI rather than only on stdout. |
| **W4** | **No project folder means no config at all.** `_load_config` returns `{}` when `_project_root` is falsy — even though `_config_path()` already falls back to the app's own `config.yml`. | `watchdog/app.py:884-890`, `:874-881` | Auto Watch needs no project root to probe apps or detect stalls. An operator who never selects a folder runs with `_settings == {}` forever: `should_send` then reads `slack_enabled` default `True`, `categories` default all-`True`, `quiet_hours` `None`. Their `slack_enabled: false` is silently ignored. **Fix:** delete the guard and load `_config_path()` unconditionally. |
| **W5** | **A formatting error on a safety event sends nothing.** `event_to_message` wraps every formatter in `except Exception: return None`. | `src/watchdog/messages.py:146-151` | A `reactor.estop` whose `failed_to_idle` arrives as a string rather than a list produces **no Slack message whatsoever** — the single most important message the app sends, dropped silently because a `join()` raised. **Fix:** on formatter failure, emit a degraded fault — event type plus a truncated `repr` of the payload — never `None`, for anything `category_for_event` calls `safety`. |
| **W6** | **The transport has no priority lane and drops on overflow.** One `queue.Queue(maxsize=200)` drained by a single worker that sleeps `min_interval_s` (3 s) between sends; `send()` drops on `queue.Full` with a log line. | `src/watchdog/transport.py:53`, `:77-87`, `:120-124` | 3 s throttle = 20 messages/minute, so a full queue is **10 minutes deep**. An E-stop queued behind a burst of progress messages is delayed by minutes or dropped outright, and the caller cannot tell. **Fix:** a separate fault queue drained first and exempt from the throttle; or one bounded ring that evicts `info` before `fault`. |

---

## MEDIUM

| ID | What | Where | Consequence / fix |
|---|---|---|---|
| **W7** | **A failed delivery is reported to the operator as a success.** `send()` returns `None`; `_post` logs a warning and swallows. `_sent_messages` is appended the moment the message is *queued*, and `/api/settings` hands that list to the UI as sent history. | `src/watchdog/transport.py:77-87`, `:114-116`; `watchdog/app.py:952-957`, `:1071` | A revoked or mistyped webhook gives a healthy-looking send log while nothing reaches Slack — and there is no other channel that would tell you. **Fix:** have the worker record per-message outcome and expose a `last_delivery_error` / `n_failed` on `/api/settings`, rendered on the Alerts page. |
| **W8** | **`close()` can drop queued messages at shutdown.** `_alive = False` is set *before* the sentinel is queued, so the worker's `while self._alive` can exit without draining. | `src/watchdog/transport.py:89-97`, `:103` | The final E-stop or stall message is lost on a clean shutdown. **Fix:** drain until empty or the join timeout expires, then stop. |
| **W9** | **The per-project config override resolves to a sibling of the project folder.** `Path(_project_root).parent / "watchdog" / "config.yml"`. | `watchdog/app.py:878` | Every other app uses `Path(_project_root) / "config.yml"` (e.g. `calibration/app.py:71`); the `.parent` form is unique in the repo and undocumented, so the override effectively never resolves. Consequence: UI toggles write to the repo's own tracked `watchdog/config.yml` — which is why `git status` shows it dirty after using the Alerts page. **Fix:** decide the intended location, document it, and stop writing into a git-tracked file from the UI. |
| **W10** | **The "campaign" category is dead.** It is in `CATEGORIES`, validated by `/api/settings`, persisted to `config.yml` and rendered as a toggle — but `category_for_event` maps nothing to it, no caller passes it to `should_send`, and no `campaign.*` event is published anywhere in `src/`. | `src/watchdog/policy.py:18`, `:23-30`; `watchdog/templates/index.html:1055`; `watchdog/app.py:949`, `:1014` | An operator control that does nothing. **Fix:** map the analyzer's campaign-lifecycle events to it, or remove it from `CATEGORIES` and the UI. |
| **W11** | **A low-confidence fit is a "fault", so it overrides quiet hours and snooze.** `format_fit_complete` returns `level="fault"` when `suspect`; app.py turns that into `kind="fault"`; `should_send` returns `True` before the quiet-hours and snooze checks. | `src/watchdog/messages.py:114-124`; `watchdog/app.py:946`; `src/watchdog/policy.py:74-75` | A poor fit pages the operator at 03:00, and the only way to stop it is to silence the whole `results` category — which also kills the good fit notifications. A suspect fit is a data-quality signal, not a safety fault. **Fix:** keep `kind="progress"` with a distinct title, or give suspect fits their own category. |
| **W12** | **`summary: "hourly"` is accepted, validated, persisted and exposed — and never read.** `should_send` never looks at `cfg["summary"]`. | `src/watchdog/settings.py:103-105`, `:215`; `watchdog/app.py:1067`; `src/watchdog/policy.py:9` | `config.yml`'s inline comment is honest ("placeholder for later"), but `policy.py`'s docstring claims progress messages "respect quiet hours, snooze, and summary batching" and the API reports the setting as live. **Fix:** implement the digest, or reject `"hourly"` in `load_settings` until it exists. |
| **W13** | **When enabled, the AI fallback can block stall detection for minutes.** `_ai_log_reading` → `src/ai/loop_advice._ask_json` → `client.messages.create` with **no `timeout`** (SDK default 10 min), called inline in `_stall_check_loop`. | `src/watchdog/diagnose.py` (`_ai_log_reading`), `src/ai/loop_advice.py:53-57`, `watchdog/app.py:1001-1004` | A hung gateway delays the very alert it is annotating, and the next 5-minute tick with it. **Fix:** pass a short timeout (≤15 s), and prefer computing the reading off the alert path so the alert never waits on it. |
| **W14** | **Ports are hard-coded in three places** — `probes.MONITOR_APPS`, `probes.ANALYZER_PORT`/`REACTOR_PORT`, and `app.LOOP_APPS` — duplicating what CLAUDE.md calls "THE app registry". | `src/watchdog/probes.py:21-29`; `watchdog/app.py:330-337` | The 5000→5100 move is exactly the change these tables miss silently: every probe returns "down" and the dashboard reads as a dead platform. **Fix:** read the ports from `apps.yml`, as the hub does. |
| **W15** | **The localhost probes do not bypass a configured proxy.** `urllib.request.urlopen` honours `http_proxy`/`https_proxy` from the environment. | `src/watchdog/probes.py:38-45`, `:57-65`, `:74-81`, `:90-97` | On a beamline control PC with a site proxy exported, every probe is routed out and fails, so all six apps read as down and every stall diagnosis is wrong in the same direction. **Fix:** build one opener with `ProxyHandler({})` and use it for all probes. |
| **W16** | **`watchdog/app.py` has no test file.** The five pure modules have 81 tests; the app module has none. | `tests/test_watchdog_*.py` | `_stage_latency`, `_loop_state`, `_on_bus_event`, `_compute_metrics`, `_config_path` and `_load_config` are entirely unexercised — and W1–W5, W9 and W11 all live in them. **Fix:** a `tests/test_watchdog_app.py` covering at minimum: config fallback behaviour (W3, W4), event→send policy wiring (W5, W11), and `_stage_latency` against a small synthetic manifest. |

---

## LOW

| ID | What | Where |
|---|---|---|
| **W17** | **The loop-closure edge is unmonitored.** `fit` is last in `_STAGE_ORDER` and `STAGE_TIMEOUTS` has no `collect` key, so once `fit.complete` lands nothing is expected of any stage until that event ages out of the 2 h `max_age_s` window. The reactor failing to start the next run — the loop quietly stopping *after* a successful cycle — raises no stall. `test_at_final_stage` documents this as intended, so it is a coverage gap rather than a regression, but it is the gap that matters most overnight. | `src/watchdog/expectations.py:19-24`, `:98-113` |
| **W18** | **Dead code.** `format_stall_message` is imported by `watchdog/app.py:45` and never called (superseded by `diagnose_stall`); `probes.check_monitor_alive` is unused. Note if either is ever revived: `format_stall_message` renders the *overdue delta* under the label "No update for N minutes", so a stage silent for 35 min with a 30 min timeout reports "No update for 5 minutes". | `src/watchdog/expectations.py:117-137`; `src/watchdog/probes.py:32-45` |
| **W19** | **`config.yml` is written non-atomically and unlocked.** `save_notify_settings` does `write_text` in place, unlike `src/manifest.py` and `src/runstate.py` which write a `.part` and `replace()`. A crash or two concurrent POSTs mid-write truncates a tracked file. | `src/watchdog/settings.py:246` |
| **W20** | **Docstring drift on `now`.** `policy.should_send` documents `now` as UTC but *requires* local time for quiet hours to mean the operator's night; `app.py` correctly passes `_now().astimezone()`. `expectations.which_stage_is_overdue` wants UTC. A future caller following the docstrings breaks quiet hours by the UTC offset — 7–8 h at SLAC, i.e. silence all afternoon and pages all night. | `src/watchdog/policy.py:50-51`; `watchdog/app.py:949`, `:1014` |
| **W21** | `_in_quiet_hours` treats `start == end` as "never quiet" rather than "always quiet", and `_parse_quiet_hours` accepts it without comment. | `src/watchdog/policy.py:108-111` |
| **W22** | `should_send`'s snooze check is `isinstance(snooze_until, float)` with a `0.0` fallback, so an `int` silently disables the snooze. Unreachable today (`_state` is memory-only) but a trap if snooze is ever persisted. | `src/watchdog/policy.py:87-91` |
| **W23** | `/api/test` bypasses `should_send`, so the Test button still sends with the master switch off. Defensible as an explicit operator action; undocumented. | `watchdog/app.py:1113-1123` |
| **W24** | `threading` is imported inside a `try` at line 1032 but referenced in an annotation at line 1029; only `from __future__ import annotations` keeps that from being a `NameError`. Move it to the top-level imports. | `watchdog/app.py:1029-1040` |
| **W25** | The Auto Watch card icon is `🐕` — the one app that did not come out of the scattering-icon pass every other card went through. | `apps.yml:120` |

---

## Recommended order

1. **W1** — it degrades everything else the platform does, and it gets worse the longer a run lasts.
2. **W2, W3, W4** — three independent ways the app is silent or wrong exactly when it is needed. All three are small, local changes.
3. **W5, W6** — the safety path: never drop a safety message, never queue it behind progress.
4. **W16** — write the app-level tests, then the rest of the MEDIUMs land with a net underneath them.
5. **W13** before the diagnose/latency work is committed — it is the only finding that the uncommitted branch introduces.

## What I checked and found sound

- The safety-cannot-be-silenced invariant, enforced independently in `load_settings`, `save_notify_settings` and `should_send`, with tests for each.
- The webhook URL is read only from the environment, never written to `config.yml`, and `.env` is gitignored.
- Timestamp handling in `expectations.py`: `src/events.py::_now()` emits `datetime.now(timezone.utc).isoformat()`, so `fromisoformat().timestamp()` is correct — no naive/aware mismatch.
- `_recent_events` concurrency: every reader takes `list(...)` before iterating, which is atomic under the GIL, and the `reversed()` comment at `app.py:97-99` shows the race was understood.
- `_probe_all_cached` correctly keeps up to 12 s of serial HTTP off the 1 Hz tick, and `probe.stale` uses `is None` so an age of `0.0` is not read as missing.
- `_throughput_last_24h` and `_stage_latency` both deliberately read durable disk state rather than the 100-event window — the right call for anything that must survive a restart (the cost of it is W1, not the choice).
- The Layer-2 AI reading's containment: append-only, `None` on missing credentials or bad output, fields truncated to 400 chars, log tail capped at 8 000, cannot change `is_stall` or the matched pattern, and `test_ai_fallback_never_used_for_named_patterns` pins that.
- Patterns A–G each report the numbers behind the verdict, and A and D are correctly classified as *not* stalls.

## Not a defect, but worth a decision

When `diagnosis.ai_fallback_enabled` is on, the tail of `logs/<app>.log` is sent to the Anthropic/SLAC gateway. Log lines carry project folder names, `.dat` paths and the operator ID from `SWAXS_USER_ID`. That is a reasonable trade for a 3am diagnosis and it is opt-in and off by default — but it is currently documented nowhere. It belongs in `SECURITY.md` (which is specifically about AI token and data handling) and in `docs/NOTIFICATIONS.md`, before the feature is committed.
