# Failure: Soft screen inserts

**Seen in:** AlterLab launch trailer, laptop shots

## What went wrong

Captures were scaled down and then back up through two transforms, and a global glow was applied after. The UI text went soft and haloed.

## How to detect it

- Edge sharpness of UI text below the capture's own sharpness.
- Halo or glow around text in the screen region.

## The fix

Use a single transform from the native capture, filter once, and keep glow and bloom masked out of screen regions.
