# Failure: Illegible text

**Seen in:** scene builder dry runs

## What went wrong

An engraved readout was too small, at a grazing angle to the camera and unlit, so it read as noise. In another candidate the builder invented copy that was not in the brief.

## How to detect it

- `text.add` size is small relative to the subject, or the text faces away from the camera.
- No light rakes across the text plate.
- A `text.add` body is not one of the brief's allowed copy strings (the builder refuses these before rendering).

## The fix

Use only the brief's copy, verbatim. Make the text large enough to read at delivery size, face it to the camera and light it with a raking strip so the engraving casts shadows.
