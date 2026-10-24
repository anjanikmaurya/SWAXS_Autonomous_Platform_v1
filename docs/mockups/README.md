# Mockups

Layout-only previews built from the real app templates. Nothing runs or saves.
Each version has its own builder; rebuild with `python docs/mockups/build_<app>_mockup_vN.py`.
A new version copies the latest builder to `_vN+1` and changes that copy only.

## Analyser (5107)

| Version | What changed |
|---|---|
| v4 (final) | Earlier versions v1 to v3 removed (October 2026). Summary: goal in the left column; tabs Setup · Fitting · Report · Knowledge · Guide; Setup steps 1 to 4 (folders, target and limits, fitting and next-recipe methods, knowledge for the campaign); Knowledge tab adds and lists cards. Audit fixes: score tolerance defined (diameter tolerance / 2, as the optimiser works in radius), ML regressor range stated as radius, AI Co-pilot and Pilot can be raced in Compare methods, tokens only for colours. AI Advisor suggestions apply automatically when inside the safety limits (else the AI is asked again, then the suggestion is dropped), or "ask me first"; measuring never stops for a review. Step 4 "Starting recipes": From knowledge 3 · Spread out 7 · Your own 0 (typed recipes), total 10 = `n_init` in `src/optimizer/campaign.py` (knowledge and own recipes are its seeds, Sobol fills the rest); warns below 5, above 20, or when fewer than half are spread out. Step 2 shows the search range read-only from `reactor/config.yml` (T 180 to 300 °C, F_tot 40 to 120 µL/min, fractions 0 to 0.3, sum ≤ 0.9); example flows corrected to lie inside it. Audit: `ANALYSER_MOCKUP_AUDIT.md` |

## Others

| File | What |
|---|---|
| `synthesis_flow_mockup.html` (`build_flow_mockup.py`, `_fake_reactor_backend.js`) | Synthesis app flow |
| `hub_layout_mockup.html` (`build_hub_mockup.py`) | Hub layout, on hold |
