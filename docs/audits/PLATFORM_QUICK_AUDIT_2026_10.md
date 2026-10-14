# Platform quick audit, October 2026

> **Status, 2026-10-14: all findings OPEN.** This is a quick, read-only pass over every app
> and the shared platform code. Every finding was confirmed by reading the code,
> except the ones marked *suspected*. Items already in [OPEN_DEFECTS.md](OPEN_DEFECTS.md)
> and the reactor items in [REACTOR_AUDIT.md](REACTOR_AUDIT.md) are not repeated
> here. IDs: **Q** = this audit.

Severity: **High** can give wrong science, lose data or expose the machine.
**Medium** misbehaves in a plausible situation. **Low** misleads or is cosmetic.

## Summary

| ID | Sev | Area | Finding |
|---|---|---|---|
| Q1 | High | Assistant | Listens on every network interface (`0.0.0.0`) |
| Q2 | High | Assistant | `run_python` sandbox can read files, reach the network, run shell commands |
| Q3 | High | Reduction | A SAXS frame counts as reduced when its WAXS twin's `.dat` exists |
| Q4 | High | Reduction | CSV metadata matched by substring: `scan10` can take `scan1`'s i0/bstop |
| Q5 | High | Reduction | Stop then Start within one interval runs two monitor loops |
| Q6 | High | Reduction | Run now and Watch can run together (docs say they cannot) |
| Q7 | High | Analysis | q unit in the file header is ignored: Å⁻¹ files give results 10× off |
| Q8 | High | Background | One q-window and rebin grid applied to both SAXS and WAXS |
| Q9 | High | Analyser | An expired condition stays queued; its real result is discarded |
| Q10 | High | Analyser | ML regressor fitter gives confident sizes for noise |
| Q11 | Medium | Background | Stop then Start within one interval runs two monitor loops |
| Q12 | Medium | Average | Shared output folder: WAXS batches overwrite SAXS batches of the same name |
| Q13 | Medium | Average | Fresh start after changing N re-averages the whole folder |
| Q14 | Medium | Analysis | Saving an analysis rewrites the Subtracted `.dat` in place |
| Q15 | Medium | Analysis | Auto Guinier range does not converge to q·Rg ≤ 1.3 |
| Q16 | Medium | Quality | An LLM verdict on a borderline curve never expires after a threshold change |
| Q17 | Medium | Reactor | Abort or new campaign does not withdraw a recipe staged for its blank flush |
| Q18 | Medium | Analyser | Continue re-proposes seed conditions already issued |
| Q19 | Medium | Analyser | After a restart, a measurement that landed during downtime is never fitted |
| Q20 | Medium | Analyser | Analyser and reactor can watch different Conditions folders, silently |
| Q21 | Medium | Reactor | Sample/background tags are free text; the parsers know only fixed names |
| Q22 | Medium | Platform | `SWAXS_HUB_PORT` override cuts every app off the event bus |
| Q23 | Medium | Platform | Hub and assistant accept cross-site POSTs from any web page |
| Q24 | Medium | Watch | Losing the bus is never reported; a dead watchdog looks like a quiet run |
| Q25 | Medium | Watch | Messages show as "sent" when no webhook is configured |
| Q26 | Medium | Hub | App log handles never closed; on Windows a restart empties the crash log |
| Q27 | Low | Average | Batch `n_files` counts frames dropped by the I0 filter |
| Q28 | Low | Average | Placeholder and fallback defaults disagree with the backend |
| Q29 | Low | Average | Seed sorts by frame index across scans *(suspected impact)* |
| Q30 | Low | Quality | Manifest quality flags are only added, never cleared |
| Q31 | Low | Quality | Resume replays an old pass threshold over the saved one |
| Q32 | Low | Background | Auto high-q scale silently falls back to 1.0 with fewer than 3 points |
| Q33 | Low | Optimiser | Search space ignores per-pump `sensor_min` / `max_flow` |
| Q34 | Low | Optimiser | Pareto front includes low-confidence fits |
| Q35 | Low | Hub | Windows Stop kills only the top process |
| Q36 | Low | Launchers | `start_platform.bat` banner error (`&`), blank Project line; 5107 named "Auto-Fit" |
| Q37 | Low | Docs | Background "restart resumes" needs `SWAXS_RESUME=1`; p(r) `_fit.dat` columns are q, I |

Also noted, not counted: Analysis records Windows WAXS paths as SAXS (`"/WAXS/"`
test); batch fits read `Subtracted/`, not `Subtracted/Good`; ATSAS tools may
assume Å⁻¹ *(suspected)*; Background events report the typed scale, not the auto
scale applied.

## Details

### Q1 Assistant listens on every network interface (High)
`assistant/app.py:1062` runs `app.run(host="0.0.0.0", ...)`; every other app binds
localhost. Anyone on the beamline network can use `/api/chat` (and the gateway
token), browse any folder via `/api/browse`, clear memory, and with
`SWAXS_ALLOW_CODE_EXEC=1` approve `run_python` themselves.
**Fix:** `host="127.0.0.1"`.

### Q2 `run_python` sandbox escapes (High)
`src/ai/code_exec.py:33-62` is a name deny-list. `check_code_safety` accepts
`matplotlib.subprocess.getstatusoutput(...)`, `np.loadtxt("/abs/path")`,
`pd.read_csv("http://...")`, `np.savetxt("/tmp/x")` and `Path("/Users").iterdir()`.
Only reachable when code execution is switched on (off by default).
**Fix:** run in an OS sandbox (no network, read-only project), or block file and
network I/O in numpy/pandas and module attribute chains; correct the "no network,
no file writes" message in `assistant.py:3082`.

### Q3 SAXS frame skipped because the WAXS `.dat` exists (High)
`reduction/app.py:397-409` searches both `SAXS/Reduction` and `WAXS/Reduction` for
`{stem}*.dat`. With prefixes stripped both detectors share a stem, so whichever
detector finishes first marks the other as done. The skipped frame is never
reduced and no event says so. Also feeds `_seed_processed_from_disk`.
**Fix:** look only in this detector's folder, for exactly `{stem}_{DET}.dat`.

### Q4 CSV metadata matched by substring (High)
`src/reduction/process_metadata.py:76-87` keeps any CSV whose stem is *contained*
in the raw stem. While `..._scan10` is acquiring (its CSV written at the end),
`..._scan1.csv` matches, so frames get scan1's i0/bstop: wrong normalisation, no
warning, CSV-wait bypassed.
**Fix:** match on a token boundary (`== stem` or `endswith("_" + stem)`).

### Q5 / Q11 Two monitor loops after a quick Stop/Start (High / Medium)
Reduction (`reduction/app.py:844`, `:891`, `:1019`) and Background
(`background/app.py:1369`, `:1379`, `:1216`) stop by clearing a flag. A Start
within one sleep interval sets it again; the old thread wakes and continues with
the old settings next to the new one (double reduction into the same `.part`
files; double subtraction, events and grades). Same pattern as N5 in Average.
**Fix:** a per-run `threading.Event` or generation token; join or refuse while
the old thread lives.

### Q6 Run now and Watch together (High)
`/api/run` refuses while Watch runs, but `monitor_start` (`reduction/app.py:830`)
never checks `_run_active`. `run_pipeline` also mutates `_processed_files` without
`_processed_lock`. `reduction/knowledge.md:275` says only one can run.
**Fix:** return 409 from `monitor_start` while a run is active.

### Q7 Analysis ignores the q unit (High)
`analysis/app.py:62-66` (`_load`) treats every q as nm⁻¹. Background writes
`q_A-1` when Å⁻¹ is chosen, so Rg, Dmax, invariants, MW and d-spacings come out
10× off but labelled nm. SASView batch then divides by 10 again (`app.py:625`).
The Analyser already converts (`analyzer/app.py:1268-1304`).
**Fix:** read the header unit in `_load` and convert to nm⁻¹ with the shared helper.

### Q8 One q-window for both detectors (High)
`background/app.py:122` holds a single `_TRUNC` (default 0.3 to 6.0 nm⁻¹, 549
points) applied by `_write_dat` to SAXS and WAXS alike, so most of a WAXS curve
is cut away (only a log line says so).
**Fix:** a truncation setting per detector, or off for WAXS by default.

### Q9 Expired condition still runs, result discarded (High)
`analyzer/app.py:803-830` scores a condition as failed after 1 h but leaves its
file in `Conditions/`. The reactor still runs it (oldest first) and the late
measurement is discarded as an orphan; the replacement then waits an extra cycle
and may expire too. Overnight pauses burn one budget slot per hour.
**Fix:** move the file to `done/` on expiry, accept a late result in place of the
failure, and start the clock when the reactor starts the run.

### Q10 ML regressor confident on noise (High)
`src/fitting/ml_regressor.py:74`, `:85-86`: confidence is relative, and sizes
outside the 1 to 18 nm library clamp to the edge without a penalty. Measured:
flat noise R = 4.1 nm at confidence 0.51 (passes `confidence_min` 0.5); a 30 nm
sphere reads 17.5 nm at 0.73. `ls_sphere` gives 0.0 and 30 nm at 0.93.
**Fix:** absolute-residual confidence plus railed-fit penalty, or mark the
fitter not ready for campaigns.

### Q12 to Q37
Short form, file and fix:

- **Q12** `average/templates/index.html:2157`, `src/plot_reduction.py:586`: names
  carry no detector; add it, or write to `<out>/<DET>/`.
- **Q13** `average/app.py:221-227`: the seed recognises only `*_{N}files_*`; use
  `src/backlog.partition_backlog` like Reduction and Background.
- **Q14** `src/analysis/io.py:88-120`: rewrites Background's file, retriggering
  Quality (and an LLM call); write a sidecar or atomic replace with a change check.
- **Q15** `src/analysis/core.py:177-190`: one refit only; iterate to
  q_max·Rg ≤ 1.3 and refuse to save otherwise.
- **Q16** `quality/app.py:262`, `:453`, `:465`: clear `llm_verdict` when the
  score or threshold moves the curve out of the borderline band.
- **Q17** `src/reactor/controller.py:336-356`, `sequence.py:93-113`:
  `withdraw_source` scans only `queue`; also check `_pending[0]`.
- **Q18** `analyzer/app.py:995`, `:1019`: set `_n_asked` from the highest issued
  r-number, not history length.
- **Q19** `analyzer/app.py:1508-1588`: exempt pending recipe_ids from
  `_startup_present`; reset `_pending_at` on restore.
- **Q20** `analyzer/app.py:85` vs `reactor/app.py:632-641`: read the reactor's
  folder or warn when they differ.
- **Q21** `src/reactor/collection.py:76-114` vs `src/loop_naming.py:18-21`:
  restrict tags to `ROLE_TAGS`/`BKG_TAGS`.
- **Q22** `src/events.py:60`, `assistant/app.py:140`: build the bus URL from
  `SWAXS_HUB_PORT`; the hub exports it to children.
- **Q23** `hub/app.py:794`, `:865`; `assistant/app.py:477`, `558`, `858`, `877`:
  drop `force=True`, check `Origin` on POST.
- **Q24** `watchdog/app.py:1373`, `:1490`: report `bus_connected`; alert after
  about 60 s disconnected.
- **Q25** `watchdog/app.py:1359`, `:1431`: record "queued" or "not configured"
  from `send()`'s result.
- **Q26** `hub/app.py:360`, `src/proc_lifecycle.py:370`: close `logf` after
  `Popen`.
- **Q27** `average/app.py:852`, `:907`: report `n_used`.
- **Q28** `average/templates/index.html:1075`, `:2154`: placeholder "(default:
  …/Averaged)"; interval fallback `||3`.
- **Q29** `average/app.py:248`, `:818`: sort by (scan, index) or mtime.
- **Q30** `src/manifest.py:1112-1124`: replace the flag list on each grade.
- **Q31** `quality/app.py:913`, `:658`: do not take the threshold from the
  replayed request.
- **Q32** `src/background/core.py:160-162`: warn when `n_points < 3`; label the
  window fields nm⁻¹.
- **Q33** `src/optimizer/space.py:44-48` vs `src/reactor/recipe.py:254-266`: add
  per-pump limits to `valid()`.
- **Q34** `src/optimizer/methods/pareto_ehvi.py:84-92`: filter by
  `confidence_min`.
- **Q35** `src/proc_lifecycle.py:233-239`: kill psutil children on Windows.
- **Q36** `start_platform.bat:69`, `:82`; `start_platform.sh:58`: `^&`,
  `!SWAXS_PROJECT!`, "Autonomous Analyser".
- **Q37** `background/knowledge.md:105`, `analysis/knowledge.md:301`,
  `src/analysis/io.py:128`: correct the docs; write the fit header unit from data.

## Checked and clean

Every route each UI calls exists with matching parameters; SFTP mapping and
atomic `.part` writes are Windows-safe; xraydb, thickness and air-normalised
transmission units are correct; the CSV row index matches `_0000` frames; every
third-party import is in a requirements file; all manifest writes go through the
lock (Windows no-lock is O18); no token or webhook value is logged; the
watchdog's `/vendor` route rejects paths.
