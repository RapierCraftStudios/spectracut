# Failure: Haze over the product

**Seen in:** AlterLab launch trailer, early lighting passes

## What went wrong

Volumetric fog and a soft atmosphere pass were added for mood. They lifted the blacks, flattened contrast and laid a grey veil over the laptop screen, so the UI read as washed out and the product lost its edges. It looked 'cinematic' in a thumbnail and muddy at delivery size.

## How to detect it

- Black level of the darkest 1% of pixels well above the Look's floor.
- Low local contrast on the product and on screen regions.
- A volume scatter node, world fog or a mist pass enabled in the recipe without a Look that allows it.
- The Critic's legibility criterion scores lower than composition.

## The fix

Remove the atmosphere, or confine it behind the subject with a holdout so it never covers the product or a screen. Restore contrast with key and rim light instead of fog. Only add haze when the Look declares it, and keep it within the Look's QA thresholds.
