# Compositor rubric

The Critic scores each criterion in [0, 1]; the score is the weight-averaged criterion score. The same criteria are in `rubric.json`, which the benchmark runner scores.

## integration (weight 1.5)

Do composited elements sit naturally in the shot (perspective, light, edges)?

## ui-sharpness (weight 2.0)

Are screen inserts and UI text crisp and correctly exposed?

## grade (weight 1.5)

Is the grade consistent across shots and within the Look's thresholds?

## artifacts (weight 1.0)

Is the shot free of compositing artifacts (edges, banding, flicker)?
