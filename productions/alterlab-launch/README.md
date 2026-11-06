# AlterLab launch film (M2 acceptance)

Declarative definition of the M2 gate production: AlterLab's SaaS launch film at
the `standard` quality profile, from brief to delivery, with 15 s and 30 s
cut-downs in 16:9, 9:16, 1:1 and 4:5 cut from the same production.

- `brief.json` is the AlterLab brief (`spectracut.spec.brief.Brief`): personas,
  claim sources, proof points traceable to those sources, offer and CTA. It is
  what `studio new` turns into the standard-profile production graph.
- `production.json` lists the production-graph nodes (kind, department, inputs,
  params), the owner gates (brief, style frames, animatic, final), the cut-down
  matrix and the external-usage rule (only Claude, ChatGPT/Codex and ElevenLabs
  voice-over).
- `tests/acceptance/test_m2_launch_film.py` runs `brief.json` through `studio new`,
  `studio run` and `studio approve` to delivery with the real departments, recorded
  Claude responses (`tests/acceptance/recorded/`), a fake Flamenco farm and fake
  audio tools, then checks the master and the 8 cut-downs on disk and the ledger.
  Its `e2e` case (`SPECTRACUT_E2E=1` and `SPECTRACUT_FLAMENCO_URL`, post-deploy
  check `m2-launch-film-farm-e2e`) runs the same loop live with nothing injected.

Run the real film on the control plane:

```sh
spectracut studio new productions/alterlab-launch/brief.json --profile standard \
  --brand-kit brands/alterlab/brand-kit.yaml --id alterlab-launch
spectracut studio run alterlab-launch
# approve each node the run reports as awaiting approval, then run again
spectracut studio approve alterlab-launch <node>
```

Repeat `studio run` and `studio approve` until delivery is clean; the delivery
node writes the master and the 15 s and 30 s cut-downs in 16:9, 9:16, 1:1 and 4:5.
