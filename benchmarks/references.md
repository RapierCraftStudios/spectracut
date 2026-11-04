# Reference films

The owner's chosen quality bar (issue #47). SpectraCut's output is benchmarked against
these. They are studied for craft only: structure, pacing, look, type and sound.
Nothing from them is copied into output.

The videos are third-party footage, so they are **not committed**. They live in the
owner's local store `~/.spectracut/references/` (keyframes in `keyframes/`), and
`benchmarks/sets/references-v1/references` is a git-ignored link to those keyframes.
Each file is identified by the first 16 hex digits of its SHA-256.

Measured with ffmpeg: cuts via `scdet` (threshold 12), loudness via `ebur128`.

| Piece | Source | Format | Length | Cuts | Avg shot | Loudness | SHA-256 (16) |
|---|---|---|---|---|---|---|---|
| `base44-launch` | Base44 launch film (repost by Amn Designer) | 3840×2160, 24 fps | 50.1 s | 38 | 1.3 s | −15.0 LUFS, LRA 3.5, peak 0.0 dBFS | `56993e3070a564c5` |
| `elevenagents-architect` | ElevenLabs, "Introducing ElevenAgents Architect" | 1920×1080, 60 fps | 67.2 s | 12 | 5.2 s | −11.7 LUFS, LRA 4.1, peak +0.2 dBFS | `7ab3ea495160a5c0` |
| `nothing-headphone-xray` | Nothing, "Headphone (1) Pro" X-ray film | 1536×2048 (3:4), 30 fps | 33.0 s | 0 (one shot) | 33 s | −18.7 LUFS, LRA 2.6, peak −4.0 dBFS | `99fe0dad6b3ae969` |
| `apple-macbook-pro` | Apple, MacBook Pro (M5 Max) launch film | 1920×1080, 23.976 fps | 111.7 s | 42 | 2.6 s | −16.0 LUFS, LRA 4.0, peak −0.6 dBFS | `9f28eadafac63e92` |

## What each one sets the bar for

### `base44-launch`: SaaS launch film (the closest model for AlterLab)
- **Pace:** very fast, about 1.3 s per shot, with the edit carrying the energy.
- **Kinetic type as the spine:** single words ("Build"), split lines ("With your name on it"), a
  typing cursor, and huge cropped letterforms filling the frame as transitions.
- **Real UI as the hero:** product screens float in clean white space; a prompt is typed and an
  app appears; dashboards and settings shown legibly at full size.
- **Mixed media:** live-action people at work, cut-out photography and collage over UI.
- **Rhythm of light and dark:** white sections switch to dark UI sections and back.
- **Ending:** a single tagline ("Build it today") and a full-bleed brand-colour end card.

### `elevenagents-architect`: product walkthrough
- **Calm pace:** about 5 s per shot, letting each UI moment read.
- **Soft gradients as the world:** burgundy, blue and warm gradient fields behind floating UI cards.
- **UI storytelling with a cursor:** prompts filling in, Approve/Reject choices, proposals lists;
  each beat shows one action and its result.
- **Typed taglines** with partly dimmed words ("Elevate your customer experiences").
- **Grid and tile transitions** between sections; integrations shown as "Connect to …" cards.

### `nothing-headphone-xray`: CGI device film
- **One continuous shot, no cuts:** the whole film is a single camera move.
- **X-ray / wireframe look:** the product drawn as glowing edges on pure black, revealing
  internal parts.
- **Exploded view choreography:** parts separate, orbit and reassemble with precise timing.
- **Restraint:** no text needed; the engineering is the message.
- This is the bar for SpectraCut's hero-object and exploded-UI templates.

### `apple-macbook-pro`: cinematic device launch
- **Cinematography:** real locations (spiral staircase from above, forest, cliff at sunset),
  extreme high and low angles, fast dolly moves, shallow depth of field.
- **Product in life:** the device always in use by a person, the screen bright and readable.
- **Spec overlays:** one short claim at a time ("10,000,000:1 contrast ratio", "Up to 16TB SSD")
  set into the shot, not on a card.
- **Abstract CGI inserts:** playful 3D objects, the chip reveal.
- **Ending:** the closed device from above on a surface.

## Targets derived from the references

- **Pacing:** launch films 1–3 s per shot; walkthroughs 4–6 s; a single-shot CGI film is valid
  when the camera move carries it.
- **Loudness:** social and web masters around −16 to −12 LUFS integrated, LRA ≤ 4 LU, true peak
  ≤ −1 dBTP (two references clip at 0 dBFS or above; SpectraCut should not).
- **UI legibility:** every UI moment readable at its on-screen duration.
- **Type:** kinetic type carries the message; one idea per card.
- **Endings:** a single tagline, then a brand end card.

## Scoring

Set `benchmarks/sets/references-v1/` maps each reference to a rubric (`ad-30s`,
`app-walkthrough`, `device-launch`). Each piece's target is 0.8 until owner ratings calibrate it
(`spectracut bench rate references-v1 <piece> <score>`). A title-sequence reference and a
cinematic CGI reference are still to be added for M3 and M4.
