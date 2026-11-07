# Failure: Flat placeholder frame

**Seen in:** AlterLab launch film, M2 style frames before the scene builder

## What went wrong

The style frame was one plane with a single matte material in the first palette swatch and a camera. It had no lights, no geometry from the description and no depth. The render was an almost uniform field of colour that showed nothing the description asked for.

## How to detect it

- The recipe has one or two geometry steps and no `light.add`.
- Luma variance of the frame is near zero; the histogram is one narrow spike.
- The Critic's adherence and lighting criteria score near 0 while palette scores well.

## The fix

Plan the scene from the description first: every named object, its material, a key, fill and rim light, and a camera with a deliberate lens. Emit the whole scene from `scene.clear`, then check the plan against the frame before handing in.
