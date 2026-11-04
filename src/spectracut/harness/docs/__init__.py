# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ranked bpy API and manual docs search for the harness ``docs.*`` tools.

The seed indexes (``bpy_api.json``, ``manual.json``) are written in-house. An
operator can replace the API index with a full RNA dump made by
``host/bpy_runtime/dump_docs.py`` via ``SPECTRACUT_BPY_DOCS``. Nothing here
imports Blender.
"""
