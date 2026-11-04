// SPDX-License-Identifier: AGPL-3.0-or-later
import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["**/node_modules/**", "**/dist/**", ".venv/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
);
