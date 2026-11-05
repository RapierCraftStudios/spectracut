# SPDX-License-Identifier: AGPL-3.0-or-later
"""Automated QA checks and report.

Check modules (import them directly; nothing is imported eagerly here):

- :mod:`spectracut.qa.legibility`: screen-text contrast and height, text read time;
- :mod:`spectracut.qa.brand_safety`: third-party URLs, wordmarks, OEM marks, logos,
  cookie banners, forbidden words and leaked secrets;
- :mod:`spectracut.qa.copy_ocr`: the OCR reader protocol, local Tesseract, copy vs spec;
- :mod:`spectracut.qa.look`: haze, grain and bloom against the Look, flicker and
  exposure jumps at cuts;
- :mod:`spectracut.qa.edit_checks`: safe areas and shot repetition;
- :mod:`spectracut.qa.audio`: BS.1770 loudness and true peak against delivery specs;
- :mod:`spectracut.qa.report`: the report, its store and the export gate;
- :mod:`spectracut.qa.common`: shared check rows and pixel helpers.
"""
