# Failure: Dim screens

**Seen in:** AlterLab launch trailer, device and laptop shots

## What went wrong

Scene exposure was set for the hero object and room, and the screen emission was left physically 'correct'. The UI rendered two stops under, text fell below legible contrast, and the grade then crushed it further.

## How to detect it

- Mean luminance of the screen region more than about one stop under the frame's key.
- On-screen text contrast ratio under 4.5:1 at delivery size.
- Screen material emission strength below the recipe's screen preset.

## The fix

Expose for the screen first: set the screen emission from the screen preset, then light the room around it. Protect the screen region in the grade (a qualifier or mask that keeps its levels). Re-check text contrast on the final frame, not on the capture.
