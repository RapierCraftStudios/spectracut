# Scene builder build-tool cheat sheet

Generated from the harness argument schemas by
`spectracut.studio.scene_builder.tool_cheat_sheet()`; do not edit by hand.
Arguments not listed are refused.

## camera.add

Add a camera and make it active.

- `name` (optional): string
- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]
- `lens` (optional): number (>= 1, <= 5000)

## camera.add_aspect

Add a camera for a delivery aspect ratio.

- `aspect` (required): "16:9" | "9:16" | "1:1" | "4:5"
- `name` (optional): string

## camera.move

Keyframe a camera move with easing.

- `keys` (required): list[CameraKey] (len >= 1)
- `easing` (optional): "linear" | "ease_in" | "ease_out" | "ease_in_out"

`CameraKey`:

- `frame` (required): integer (>= 0)
- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]
- `lens` (optional): number (>= 1, <= 5000)

## camera.set_dof

Set depth of field on the active camera.

- `enabled` (optional): boolean
- `focus_object` (optional): string
- `focus_distance` (optional): number (>= 0)
- `fstop` (optional): number (>= 0.1, <= 128)

## camera.set_lens

Set the active camera's focal length (mm).

- `mm` (required): number (>= 1, <= 5000)

## camera.set_transform

Move or rotate the active camera.

- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]

## camera.use

Make a named camera active.

- `name` (required): string
- `long_edge` (optional): integer (>= 16, <= 16384)

## curve.add

Add a bezier or poly curve through points; bevel it into a tube.

- `points` (required): list[list[number][3]] (len >= 2, len <= 256). control points the curve passes through
- `name` (optional): string (len >= 1, len <= 63)
- `kind` (optional): "bezier" | "poly"
- `bevel_depth` (optional): number (>= 0, <= 10). tube radius in m; 0 is a bare line
- `bevel_resolution` (optional): integer (>= 0, <= 32)
- `resolution` (optional): integer (>= 1, <= 64)
- `cyclic` (optional): boolean
- `location` (optional): list[number][3]

## light.add

Add a point/sun/area/spot light (optional area size, colour, spot angle), or a rig preset (three_point, key_fill, product_softbox, rim_area) aimed at a target.

Rules: Either one light or a preset rig, never both. One light: kind, energy, location, rotation, size (area only), color, spot_angle (spot only); no target, distance or power. Rig: rig with optional target, distance and power, and none of the single-light arguments.

- `kind` (optional): "point" | "sun" | "area" | "spot"
- `name` (optional): string
- `energy` (optional): number (>= 0, <= 10000000.0)
- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]
- `size` (optional): number (> 0, <= 100). area light size in m (area only)
- `color` (optional): list[number (>= 0, <= 1)][3]
- `spot_angle` (optional): number (>= 1, <= 180). spot cone angle in degrees (spot only)
- `rig` (optional): "three_point" | "key_fill" | "product_softbox" | "rim_area". add a preset rig of lights aimed at target instead of one light
- `target` (optional): list[number][3]. point the rig aims at (rig only)
- `distance` (optional): number (> 0, <= 1000). rig light distance from target in m (rig only)
- `power` (optional): number (> 0, <= 100). rig energy multiplier (rig only)

## material.apply_preset

Give a mesh a material preset (plastics, metals, glass incl. clear/frosted/smoked, emissive, anodised metal) with a colour or brand colour and optional roughness, ior, transmission, emission, anisotropy and metallic overrides.

Rules: Pass color or brand_color, not both.

- `object` (required): string
- `preset` (required): "matte_plastic" | "glossy_plastic" | "brushed_metal" | "polished_metal" | "glass" | "rubber" | "ceramic" | "clear_glass" | "frosted_glass" | "smoked_glass" | "emissive" | "anodised_metal"
- `color` (optional): list[number (>= 0, <= 1)] (len >= 3, len <= 4)
- `brand_color` (optional): string (matches ^#[0-9a-fA-F]{6}$). a brand-kit swatch '#rrggbb'; recorded as color
- `name` (optional): string
- `roughness` (optional): number (>= 0, <= 1)
- `ior` (optional): number (>= 1, <= 3)
- `transmission` (optional): number (>= 0, <= 1)
- `emission_color` (optional): list[number (>= 0, <= 1)] (len >= 3, len <= 4)
- `emission_strength` (optional): number (>= 0, <= 1000)
- `anisotropy` (optional): number (>= 0, <= 1)
- `metallic` (optional): number (>= 0, <= 1)

## object.add_primitive

Add a cube, plane, sphere or cylinder.

- `kind` (required): "cube" | "plane" | "uv_sphere" | "cylinder"
- `name` (optional): string
- `location` (optional): list[number][3]
- `scale` (optional): list[number][3]

## object.add_slab

Add a box of given dimensions with bevelled edges.

Rules: bevel_width must be at most half the smallest dimension.

- `dimensions` (required): list[number (> 0, <= 1000)][3]. box size in m along x, y, z
- `name` (optional): string (len >= 1, len <= 63)
- `bevel_width` (optional): number (>= 0). bevel width in m, at most half the smallest dimension
- `segments` (optional): integer (>= 1, <= 16)
- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]

## object.animate

Animate an object with a preset: turntable, reveal, shatter or assemble (seeded).

Rules: frame_end must be after frame_start; each preset takes only its own options. turntable: degrees, axis. reveal: from, offset (offset only, and required, when from is position). shatter and assemble: pieces, seed, distance.

- `object` (required): string (len >= 1, len <= 63)
- `preset` (required): "turntable" | "reveal" | "shatter" | "assemble"
- `frame_start` (required): integer (>= 0, <= 1048574)
- `frame_end` (required): integer (>= 0, <= 1048574)
- `degrees` (optional): number (>= -3600, <= 3600)
- `axis` (optional): "x" | "y" | "z"
- `from` (optional): "scale" | "position"
- `offset` (optional): list[number][3]. position reveal only
- `pieces` (optional): integer (>= 2, <= 256)
- `seed` (optional): integer (>= 0, <= 2147483647)
- `distance` (optional): number (>= 0, <= 100)

## object.array

Repeat an object by an offset (Array modifier or linked).

- `object` (required): string (len >= 1, len <= 63)
- `count` (required): integer (>= 2, <= 1000). total copies, original included
- `offset` (required): list[number][3]. constant offset between copies, in m
- `mode` (optional): "modifier" | "linked". an Array modifier (default) or linked duplicates sharing the mesh

## object.boolean

Cut, join or intersect a mesh with a hidden cutter.

Rules: object and cutter must be different objects.

- `object` (required): string (len >= 1, len <= 63)
- `cutter` (required): string (len >= 1, len <= 63). mesh to cut with; hidden from renders
- `operation` (optional): "difference" | "union" | "intersect"

## object.import_gltf

Import a glTF/GLB asset from the assets dir.

- `path` (required): string. relative to the host's assets dir; no absolute paths or ..
- `name` (optional): string
- `location` (optional): list[number][3]
- `scale` (optional): list[number][3]

## object.keyframe

Keyframe an object's transforms, a camera's lens or material inputs, with easing.

Rules: keys have strictly increasing frames; material applies only to keys with inputs. Each key needs at least one of location, rotation, scale, lens or inputs.

- `object` (required): string (len >= 1, len <= 63)
- `keys` (required): list[KeyframeKey] (len >= 2, len <= 1000)
- `material` (optional): string (len >= 1, len <= 63). material slot to key inputs on (default: active material)
- `easing` (optional): "linear" | "ease_in" | "ease_out" | "ease_in_out"

`KeyframeInputs`:

Rules: Allowlisted Principled BSDF inputs of the object's material; at least one is set.

- `emission_strength` (optional): number (>= 0, <= 1000)
- `alpha` (optional): number (>= 0, <= 1)
- `roughness` (optional): number (>= 0, <= 1)
- `transmission` (optional): number (>= 0, <= 1)

`KeyframeKey`:

Rules: A key needs at least one of location, rotation, scale, lens or inputs.

- `frame` (required): integer (>= 0, <= 1048574)
- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]
- `scale` (optional): list[number][3]
- `lens` (optional): number (>= 1, <= 5000). camera objects only
- `inputs` (optional): KeyframeInputs

## object.parent

Parent an object to another, keeping its transform.

Rules: child and parent must be different objects.

- `child` (required): string (len >= 1, len <= 63)
- `parent` (required): string (len >= 1, len <= 63)

## particles.add

Add a seeded particle/dust system on a mesh emitter.

Rules: frame_end must not be before frame_start; an emitter cannot instance itself.

- `emitter` (required): string (len >= 1, len <= 63). mesh object that emits the particles
- `name` (optional): string (len >= 1, len <= 63). system name (default Dust)
- `count` (optional): integer (>= 1, <= 100000)
- `seed` (optional): integer (>= 0, <= 2147483647). same seed and settings, same particles
- `lifetime` (optional): number (>= 1, <= 10000). frames
- `size` (optional): number (> 0, <= 100)
- `velocity` (optional): number (>= 0, <= 100). initial speed along the emitter's normals
- `frame_start` (optional): integer (>= 0, <= 1048574)
- `frame_end` (optional): integer (>= 0, <= 1048574)
- `instance` (optional): string (len >= 1, len <= 63). object to render per particle

## render.set_engine

Set the render engine.

- `engine` (required): "eevee" | "cycles" | "workbench"

## render.set_frame_range

Set the scene's frame range.

Rules: frame_end must be after frame_start.

- `frame_start` (required): integer (>= 0, <= 1048574)
- `frame_end` (required): integer (>= 0, <= 1048574)

## render.set_property

Set an allowlisted render setting.

- `property` (required): "render.film_transparent" | "render.fps" | "render.use_motion_blur" | "render.motion_blur_shutter" | "cycles.samples" | "eevee.taa_render_samples" | "view_settings.view_transform" | "view_settings.look" | "view_settings.exposure" | "view_settings.gamma"
- `value` (required): boolean | integer | number | string

## render.set_resolution

Set the output resolution.

- `width` (required): integer (>= 16, <= 16384)
- `height` (required): integer (>= 16, <= 16384)
- `percentage` (optional): integer (>= 1, <= 100)

## scene.clear

Remove every object from the scene.

- no arguments

## text.add

Add 3D text: extruded depth or engraved cutout in a plate.

Rules: plate is required for mode 'cutout' and only allowed there; weight is not allowed with font.

- `body` (required): string (len >= 1, len <= 500)
- `name` (optional): string (len >= 1, len <= 63)
- `size` (optional): number (>= 0.001, <= 1000)
- `extrude` (optional): number (>= 0, <= 100)
- `bevel` (optional): number (>= 0, <= 10). rounds the extruded edges (bevel depth)
- `align` (optional): "left" | "center" | "right". left: location is the first glyph's left edge; use for key/value columns
- `weight` (optional): 400 | 600. host brand font Inter weight; ignored with font
- `mode` (optional): "depth" | "cutout". depth: extruded type; cutout: engraved through plate
- `plate` (optional): string (len >= 1, len <= 63). mesh the cutout is cut into
- `location` (optional): list[number][3]
- `rotation` (optional): list[number][3]
- `font` (optional): TextFont. sha256-pinned font in the assets dir; default is the host-shipped Inter

`TextFont`:

- `path` (required): string. font file in the host assets dir (brand-kit OFL font)
- `sha256` (required): string (matches ^[0-9a-f]{64}$)

## world.set_gradient

Set a vertical colour gradient world background (bottom to top) with a strength.

- `bottom` (required): list[number (>= 0, <= 1)] (len >= 3, len <= 4)
- `top` (required): list[number (>= 0, <= 1)] (len >= 3, len <= 4)
- `strength` (optional): number (>= 0, <= 1000)

## world.set_haze

Add volumetric haze to the world (density capped Look-safe); call after the background.

- `density` (required): number (> 0, <= 0.05)
- `color` (optional): list[number (>= 0, <= 1)] (len >= 3, len <= 4)
- `anisotropy` (optional): number (>= -1, <= 1)

## world.set_hdri

Light the world from a sha256-pinned HDRI in the host assets dir.

- `path` (required): string
- `sha256` (required): string (matches ^[0-9a-f]{64}$)
- `strength` (optional): number (>= 0, <= 1000)
- `rotation` (optional): number (>= -360, <= 360)
