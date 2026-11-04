# SPDX-License-Identifier: AGPL-3.0-or-later
# Enable every CUDA/OptiX device for Cycles before the job renders (prefs are not stored in .blend files),
# and denoise on the GPU (a scene setting, applied after the .blend loads).
import bpy
from bpy.app.handlers import persistent
prefs = bpy.context.preferences.addons["cycles"].preferences
for kind in ("OPTIX", "CUDA"):
    try:
        prefs.compute_device_type = kind
        prefs.get_devices()
        if any(d.type == kind for d in prefs.devices):
            break
    except TypeError:
        continue
for d in prefs.devices:
    d.use = d.type != "CPU"
print("spectracut: cycles devices", prefs.compute_device_type, [(d.name, d.use) for d in prefs.devices])

@persistent
def _gpu_denoise(_):
    for sc in bpy.data.scenes:
        if sc.render.engine == "CYCLES":
            sc.cycles.device = "GPU"
            sc.cycles.denoising_use_gpu = True
            print("spectracut: gpu denoise on", sc.name)
bpy.app.handlers.load_post.append(_gpu_denoise)
