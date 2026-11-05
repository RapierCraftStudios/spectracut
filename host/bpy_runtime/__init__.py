# SPDX-License-Identifier: AGPL-3.0-or-later
"""bpy runtime: runs inside Blender only.

It ships in the ``spectracut-host`` wheel as files: the host guard reads
``tools.py`` and hands ``runner.py`` to Blender. Never import it from the control
plane or the host agent process (``spectracut._bpy_guard`` blocks that).
"""
