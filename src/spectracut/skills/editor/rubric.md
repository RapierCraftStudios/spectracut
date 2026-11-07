# Editor rubric

The Critic scores each criterion in [0, 1]; the score is the weight-averaged criterion score. The same criteria are in `rubric.json`, which the benchmark runner scores.

## rhythm (weight 1.5)

Does the cut have a varied, deliberate rhythm that matches music and VO?

## readability (weight 1.5)

Is every UI and text shot held long enough to read?

## continuity (weight 1.0)

Are cuts clean, on action, with consistent screen direction?

## length (weight 1.0)

Is the edit tight and within the target length for each delivery?

## beat_sync (weight 1.5)

Does every picture cut land on a music beat (within one frame), with VO lines overriding only where a cut would split a line, as J/L cuts of at most 12 frames?

## alternation (weight 1.0)

Do device shots carry the film with abstract interludes as breathers, in the storyboard's order, joined by match cuts and speed ramps?

## board_max (weight 1.0)

Does every shot stay within what its board allows (its animatic length plus one beat of snap)?

## title_hold (weight 1.0)

Is every title held for its reading time (words / 3 per second + 1 s) after the shot settles?

## cutdown_pacing (weight 1.0)

Are the 15 s and 30 s cut-downs re-paced on the beat (quicker per shot at 15 s, interludes as 1-2-beat punctuation) rather than trimmed in proportion?
