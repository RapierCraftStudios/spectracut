# AlterLab launch film (M2 acceptance)

Declarative definition of the M2 gate production: AlterLab's SaaS launch film at
the `standard` quality profile, from brief to delivery, with 15 s and 30 s
cut-downs in 16:9, 9:16, 1:1 and 4:5 cut from the same production.

- `production.json` lists the production-graph nodes (kind, department, inputs,
  params), the owner gates (brief, style frames, animatic, final), the cut-down
  matrix and the external-usage rule (only Claude, ChatGPT/Codex and ElevenLabs
  voice-over).
- `tests/acceptance/test_m2_launch_film.py` builds the graph from this file and
  checks cost accounting and that a late change re-runs only its dependants.
  Its `e2e` case (`SPECTRACUT_E2E=1`, post-deploy check
  `m2-launch-film-farm-e2e`) checks the stored farm production.

Run the real film with `spectracut studio run alterlab-launch` on the farm host;
the owner approves the gated nodes with `spectracut studio approve`.
