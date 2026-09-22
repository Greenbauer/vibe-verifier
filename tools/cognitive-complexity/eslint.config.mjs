import sonarjs from "eslint-plugin-sonarjs";
import tsParser from "@typescript-eslint/parser";
const max = Number(process.env.VV_COMPLEXITY_MAX || 15);
export default [{
  files: ["**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}"],
  languageOptions: { parser: tsParser, parserOptions: { ecmaFeatures: { jsx: true }, ecmaVersion: "latest", sourceType: "module" } },
  plugins: { sonarjs },
  rules: { "sonarjs/cognitive-complexity": ["error", max] },
}];
