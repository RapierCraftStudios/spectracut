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

Filled in by a later sub-issue (#284).

## Text

Filled in by a later sub-issue (#284).

## Animation

Filled in by a later sub-issue (#285).

## Particles

Filled in by a later sub-issue (#285).
