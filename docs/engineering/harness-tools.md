# Harness tools

The harness MCP server (`src/spectracut/harness/tools/`) exposes typed build tools.
Each records one or more declarative recipe steps that the host bpy runtime
(`host/bpy_runtime/tools.py`, `TOOL_NAMES`/`TOOLS`) executes on a render host. A
recipe is JSON only: no arbitrary Python. Every runtime op checks its arguments
before touching `bpy`; ops with randomness take a `seed`. Every op below is
deterministic, so none takes a seed.

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
| `color` | RGB/RGBA in [0, 1] | Base Color |
| `brand_color` | `#rrggbb` brand-kit swatch; harness converts it to `color` (not with `color`) | Base Color |
| `name` | material name | |
| `roughness` | [0, 1] | Roughness |
| `ior` | [1, 3] | IOR |
| `transmission` | [0, 1] | Transmission Weight |
| `emission_color` | RGB/RGBA in [0, 1] | Emission Color |
| `emission_strength` | [0, 1000] | Emission Strength |
| `anisotropy` | [0, 1] | Anisotropic |
| `metallic` | [0, 1] | Metallic |

Overrides are applied after the preset, and the runtime report echoes them.

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
- `world.set_gradient`: `bottom` and `top` colours (RGB/RGBA in [0, 1]) and
  `strength` [0, 1000]: a vertical gradient background. It keeps any haze.
- `world.set_haze`: `density` (0, 0.05], `color` (default white), `anisotropy`
  [-1, 1]. A Volume Scatter on the world volume only. The 0.05 ceiling is a
  Look-safe cap (clarity over atmosphere); the master is still gated by its Look's
  `haze_max` QA threshold. Call it after the background (`world.set_hdri` clears it).

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

Filled in by a later sub-issue (#285).

## Particles

Filled in by a later sub-issue (#285).
