// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Flamenco custom job type: spectracut-shot.
//
// Turns a signed SpectraCut shot job into one task per frame chunk. Every task
// runs `spectracut-host run` on the worker, which verifies the job signature and
// the host registration through the execution guard before starting
// `blender -b --factory-startup` with the bpy runtime. It never passes Python
// to Blender: hosts only run declarative recipes.
//
// assets_dir is normally `{storage}/assets/<sha>`: the brand assets the control
// plane staged content-addressed on shared storage. `{storage}` is Flamenco's
// two-way variable, which the Manager expands per worker platform when it
// dispatches the task, so the value is passed through unchanged.

/* global author */

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const JOB_TYPE = {
  label: "SpectraCut Shot",
  description: "Render a signed SpectraCut recipe through spectracut-host",
  settings: [
    { key: "job_file", type: "string", subtype: "file_path", required: true,
      description: "Signed SpectraCut job (JSON) on shared storage" },
    { key: "frames", type: "string", required: true,
      description: "Frame range to render, e.g. 1-120" },
    { key: "chunk_size", type: "int32", default: 10, required: true,
      description: "Frames per task" },
    { key: "output_dir", type: "string", subtype: "dir_path", required: true,
      description: "Where the host writes frames and result.json" },
    { key: "assets_dir", type: "string", subtype: "dir_path", required: false,
      description: "Assets root on shared storage, normally {storage}/assets/<sha> " +
        "(the two-way storage variable maps it per worker platform)" },
  ],
};

function parseFrames(frames) {
  const match = /^\s*(\d+)\s*(?:-\s*(\d+))?\s*$/.exec(String(frames));
  if (!match) {
    throw new Error("frames must look like 1-120, got " + frames);
  }
  const start = parseInt(match[1], 10);
  const end = match[2] === undefined ? start : parseInt(match[2], 10);
  if (end < start) {
    throw new Error("frames end before start: " + frames);
  }
  return { start: start, end: end };
}

function chunkFrames(start, end, size) {
  if (!(size >= 1)) {
    throw new Error("chunk_size must be >= 1");
  }
  const chunks = [];
  for (let first = start; first <= end; first += size) {
    chunks.push({ start: first, end: Math.min(first + size - 1, end) });
  }
  return chunks;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function compileJob(job) {
  const settings = job.settings;
  if (!settings.job_file) {
    throw new Error("job_file is required");
  }
  const range = parseFrames(settings.frames);
  const chunks = chunkFrames(range.start, range.end, settings.chunk_size);
  for (const chunk of chunks) {
    const frames = chunk.start + "-" + chunk.end;
    const task = author.Task("render-" + frames, "blender");
    const args = [
      "run",
      "--job-file", settings.job_file,
      "--frames", frames,
      "--output", settings.output_dir,
    ];
    if (typeof settings.assets_dir === "string" && settings.assets_dir !== "") {
      args.push("--assets-dir", settings.assets_dir);
    }
    task.addCommand(author.Command("exec", { exe: "spectracut-host", args: args }));
    job.addTask(task);
  }
}
