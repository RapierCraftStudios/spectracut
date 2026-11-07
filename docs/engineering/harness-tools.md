# Harness tools

The harness MCP server (`src/spectracut/harness/tools/`) exposes typed build tools.
Each records one or more declarative recipe steps that the host bpy runtime
(`host/bpy_runtime/tools.py`, `TOOL_NAMES`/`TOOLS`) executes on a render host. A
recipe is JSON only: no arbitrary Python. Every runtime op checks its arguments
before touching `bpy`. Every op below is deterministic: the ops with randomness
(`object.animate` `shatter`/`assemble` and `particles.add`) take a `seed` and give
the same result for the same seed.

## Materials

`material.apply_preset` gives a mesh a Principled BSDF material.

| Preset | Character |
| --- | --- |
| `matte_plastic` | diffuse plastic, roughness 0.6 |
| `glossy_plastic` | glossy plastic with a light coat |
| `brushed_metal` | metal, anisotropic 0.6 |
| `polished_metal` | mirror-like metal |
| `glass` | clear glass (kept for existing recipes) |
| `rubber` | dark, rough rubber |
| `ceramic` | glazed ceramic |
| `clear_glass` | transmission 1, IOR 1.5, roughness 0 |
| `frosted_glass` | transmission 1, IOR 1.5, roughness 0.35 |
| `smoked_glass` | transmission 1, IOR 1.5, roughness 0.05, dark grey tint |
| `emissive` | emission colour = base colour, strength 10 |
| `anodised_metal` | metal, roughness 0.3, anisotropic 0.5, blue default |

Arguments (all optional except `object` and `preset`):

| Argument | Range | Socket |
| --- | --- | --- |
| `color` | scene-linear RGB/RGBA in [0, 1] (never hex/255) | Base Color |
| `brand_color` | `#rrggbb` brand-kit swatch; the harness linearises it (sRGB EOTF) and records it as `color` (not with `color`) | Base Color |
| `name` | material name | |
| `roughness` | [0, 1] | Roughness |
| `ior` | [1, 3] | IOR |
| `transmission` | [0, 1] | Transmission Weight |
| `emission_color` | RGB/RGBA in [0, 1] | Emission Color |
| `emission_strength` | [0, 1000]; with `brand_color` on `emissive`, default 1 and at most 1 | Emission Strength |
| `anisotropy` | [0, 1] | Anisotropic |
| `metallic` | [0, 1] | Metallic |

Overrides are applied after the preset, and the runtime report echoes them.

Brand emissives: an `emissive` preset with `brand_color` records
`emission_strength` 1 when none is given and refuses more than 1
(`BRAND_EMISSION_MAX`, `args.invalid`). Under the brand view (below) a linearised
swatch at strength 1 displays as its own hex; above 1 the brightest channel clips
and the hue drifts to cyan or white. A numeric `color` keeps the full strength range.

Surface detail (optional; builds a procedural node graph only when a strength is > 0):

| Argument | Range | Nodes built |
| --- | --- | --- |
| `bump_strength` | [0, 1] | Noise or Voronoi texture -> Bump -> BSDF Normal (micro-texture) |
| `bump_kind` | `noise` (default) or `voronoi` | pattern for `bump_strength` |
| `bump_scale` | (0, 1000], default 150 | texture frequency per object unit |
| `brushing` | [0, 1] | stretched noise streaks into the bump, Anisotropic raised to at least this |
| `brushing_angle` | [0, 360) degrees | streak direction; Anisotropic Rotation = angle / 360 |
| `scratches` | [0, 1] | stretched Voronoi edges: fine lines in the bump and rougher Roughness |
| `edge_wear` | [0, 1] | Bevel node (radius up to 3 cm) into the bump Normal: edge highlights, Cycles only |
| `seed` | integer [0, 2^31 - 1], default 0 | offsets every pattern's coordinates; same seed, same surface |

All height sources share one Bump node. The report echoes the detail arguments and the
seed used.

```json
{"tool": "material.apply_preset",
 "args": {"object": "Body", "preset": "anodised_metal", "brushing": 0.6, "brushing_angle": 90,
          "edge_wear": 0.4, "seed": 3}}
```

```json
{"tool": "material.apply_preset",
 "args": {"object": "Beam", "preset": "emissive", "color": [0.1, 0.4, 1.0], "emission_strength": 40}}
```

## Lighting & world

`light.add` adds one light: `kind` (point, sun, area, spot), `name`, `energy` (W,
[0, 1e7]), `location`, `rotation`, plus `size` (area only, (0, 100] m), `color`
(RGB in [0, 1]) and `spot_angle` (spot only, [1, 180] degrees).

Or it adds a rig of area lights aimed at `target` (`distance`, `power` scale it):

| Rig | Lights |
| --- | --- |
| `three_point` | Key, Fill, Rim |
| `key_fill` | Key, Fill |
| `product_softbox` | Left, Right, Top |
| `rim_area` | Key, RimLeft, RimRight (two rims behind the subject) |

World tools:

- `world.set_hdri`: `path` (in the host assets dir), `sha256` (64 lowercase hex,
  checked before Blender reads the file), `strength` [0, 1000], `rotation`
  [-360, 360] degrees. It rebuilds the whole world tree.
- `world.set_gradient`: `bottom` and `top` colours (scene-linear RGB/RGBA in
  [0, 1]) or `brand_bottom` / `brand_top` (`#rrggbb` swatches, linearised and
  recorded as numeric `bottom` / `top`; exactly one of each pair), and `strength`
  [0, 1000]: a vertical gradient background. It keeps any haze.
- `world.set_haze`: `density` (0, 0.05], `color` (default white), `anisotropy`
  [-1, 1]. A Volume Scatter on the world volume only. The 0.05 ceiling is a
  Look-safe cap (clarity over atmosphere); the master is still gated by its Look's
  `haze_max` QA threshold. Call it after the background (`world.set_hdri` clears it).

Scene-built style frames and shots render under `BRAND_PIPELINE`
(`spectracut.finishing.color`): the `Standard` view transform on an `sRGB` display
with `view_settings.exposure` 0. `tier_recipe` appends it after the scene's own
calls, so it overrides any view transform or exposure the scene set. A brand void
(`brand_bottom`/`brand_top` at strength 1) renders as its swatch; palette QA
(`spectracut.qa.palette`) measures the frame against the brand swatches.

```json
[{"tool": "world.set_gradient", "args": {"bottom": [0.01, 0.02, 0.05], "top": [0.05, 0.08, 0.15]}},
 {"tool": "world.set_haze", "args": {"density": 0.01}},
 {"tool": "light.add", "args": {"rig": "rim_area", "target": [0, 0, 1]}}]
```

## Geometry

Geometry ops live in `host/bpy_runtime/ops/geometry.py`. New object names must
not already exist, and objects named in arguments must exist
(`scene.object_not_found` otherwise).

- `curve.add`: `points` (2 to 256 `[x, y, z]` control points), `kind` (`bezier`,
  the default, with auto handles, or `poly`), `bevel_depth` [0, 10] m (the tube
  radius; 0 is a bare line), `bevel_resolution` [0, 32], `resolution` [1, 64],
  `cyclic`, `name`, `location`. The caps are filled, which makes it suitable for
  beams and cables.
- `object.add_slab`: `dimensions` (x, y, z, each in (0, 1000] m), `bevel_width`
  [0, half the smallest dimension] m, `segments` [1, 16], `name`, `location`,
  `rotation`. The mesh is built at its true size (scale 1), so the Bevel
  modifier's width is in metres and even on every edge. A width of 0 adds no
  modifier.
- `object.array`: `object`, `count` [2, 1000] (total copies, including the
  original), `offset` (m), `mode`:
  - `modifier` (default): an Array modifier with a constant offset in the
    object's local space.
  - `linked`: `count - 1` linked duplicates named `<object>.001`, `.002`, ...,
    that share the original's mesh data and are placed at `i * offset` from it.
- `object.boolean`: `object`, `cutter` (both meshes, different), `operation`
  (`difference` default, `union`, `intersect`). It adds a Boolean modifier named
  `boolean.<cutter>`. The cutter is hidden from renders and drawn as a wire, and
  stays in the scene so the modifier still evaluates.
- `object.parent`: `child`, `parent`. The child keeps its world transform.
  Parenting an object to itself or to one of its descendants is refused.

```json
[{"tool": "object.add_slab", "args": {"name": "Tier1", "dimensions": [2, 2, 0.1], "bevel_width": 0.02}},
 {"tool": "object.array", "args": {"object": "Tier1", "count": 5, "offset": [0, 0, 0.3], "mode": "linked"}},
 {"tool": "curve.add", "args": {"name": "Beam", "points": [[0, 0, -1], [0, 0, 2]], "bevel_depth": 0.03}},
 {"tool": "object.add_primitive", "args": {"kind": "cylinder", "name": "Bore", "scale": [0.1, 0.1, 1]}},
 {"tool": "object.boolean", "args": {"object": "Tier1", "cutter": "Bore"}},
 {"tool": "object.parent", "args": {"child": "Beam", "parent": "Tier1"}}]
```

## Text

`text.add` (`host/bpy_runtime/ops/text.py`) adds world-space 3D text:

- `body`: 1 to 500 characters.
- `name`, `size` [0.001, 1000], `extrude` [0, 100], `location`, `rotation`.
- `mode`:
  - `depth` (default): extruded type.
  - `cutout`: engraved. The text becomes a hidden mesh cutter, and a Boolean
    difference named `cutout.<name>` is added to the mesh `plate`. `plate` is
    required for `cutout` and refused for `depth`.
- `font`: `{path, sha256}`, an OFL font from the brand kit in the host assets
  dir, loaded only after its sha256 matches. Without it, Blender's built-in font
  is used. Templates resolve a brand-kit family to this pinned font
  (`spectracut.templates.base.font_arg`).

```json
{"tool": "text.add",
 "args": {"body": "ONE BEAM", "mode": "cutout", "plate": "Tier1", "size": 0.2, "extrude": 0.05,
          "font": {"path": "fonts/Inter.ttf", "sha256": "<64 hex>"}}}
```

## Animation

Animation ops live in `host/bpy_runtime/ops/keyframe.py` and
`host/bpy_runtime/ops/animate.py`. Frames are integers in [0, 1048574].

- `object.keyframe`: `object`, `keys` (2 to 1000, strictly increasing `frame`),
  `easing`, `material`. Each key sets at least one channel:
  - `location`, `rotation` (radians), `scale`: `[x, y, z]` on any object,
    including a camera.
  - `lens` [1, 5000] mm: camera objects only.
  - `inputs`: an allowlist of Principled BSDF inputs on the object's material:
    `emission_strength` [0, 1000], `alpha` [0, 1], `roughness` [0, 1],
    `transmission` [0, 1]. `material` names one of the object's material slots
    (default: the active material) and is refused without `inputs`.

  `easing` is `linear`, `ease_in`, `ease_out` or `ease_in_out` (the default), the
  same vocabulary as `camera.move`. It applies to every key the op writes.
- `render.set_frame_range`: `frame_start`, `frame_end` (after `frame_start`). It
  sets the scene range for previews and saved scenes; a render job's own frames
  still decide what renders (the runner applies them after the recipe steps).
- `object.animate`: `object`, `preset`, `frame_start`, `frame_end`, plus fields per
  preset (others are refused):
  - `turntable`: `degrees` [-3600, 3600] (default 360) about `axis` (`x`, `y`,
    `z`, default `z`), linear.
  - `reveal`: `from` `scale` (default; grows from near zero) or `position` (slides
    in from `offset`, required).
  - `shatter`: split the mesh into `pieces` [2, 256] (default 12) that fly apart by
    `distance` [0, 100] m; `seed` fixes the split.
  - `assemble`: `shatter` reversed.

```json
[{"tool": "render.set_frame_range", "args": {"frame_start": 1, "frame_end": 96}},
 {"tool": "object.keyframe",
  "args": {"object": "Beam", "easing": "ease_out",
           "keys": [{"frame": 1, "inputs": {"emission_strength": 0}},
                    {"frame": 24, "inputs": {"emission_strength": 40}}]}},
 {"tool": "object.keyframe",
  "args": {"object": "Camera",
           "keys": [{"frame": 1, "location": [0, -6, 1.5], "lens": 35},
                    {"frame": 96, "location": [0, -4, 1.2], "lens": 50}]}},
 {"tool": "object.animate",
  "args": {"object": "Tier1", "preset": "reveal", "from": "position", "offset": [0, 0, -1],
           "frame_start": 1, "frame_end": 30}}]
```

## Particles

`particles.add` (`host/bpy_runtime/ops/particles.py`) adds a dust/emitter system
as a Particle System modifier on a mesh:

- `emitter`: an existing mesh object.
- `name` (default `Dust`): the modifier and settings name; it must be new on the
  emitter.
- `count` [1, 100000] (default 1000), `lifetime` [1, 10000] frames (default 50),
  `size` (0, 100] m (default 0.01), `velocity` [0, 100] (speed along the
  emitter's normals, default 0), `frame_start`/`frame_end` (emission window,
  default 1/1).
- `seed` [0, 2147483647] (default 0): the system's seed. The op draws no random
  numbers itself, so the same settings and seed give the same particles.
- `instance`: an object rendered per particle (not the emitter). Without it the
  particles render as halos.

```json
{"tool": "particles.add",
 "args": {"emitter": "Room", "count": 2000, "seed": 7, "lifetime": 240, "size": 0.004,
          "velocity": 0.05, "frame_start": 1, "frame_end": 96, "instance": "Mote"}}
```
