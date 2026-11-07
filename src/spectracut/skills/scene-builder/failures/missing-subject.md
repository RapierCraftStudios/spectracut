# Failure: Missing subject

**Seen in:** scene builder dry runs

## What went wrong

The scene was lit and composed, but the object the description is about was missing, hidden behind another object or outside the camera's view. The frame looked finished and said nothing.

## How to detect it

- An object the description names has no matching build call, or its location is behind the camera or outside the frustum.
- The Critic's adherence criterion scores low while lighting scores well.

## The fix

Name every subject object in the plan and give it a build call. Aim the camera at it, fill the frame with it and light it first. Check object names against the description before handing in.
