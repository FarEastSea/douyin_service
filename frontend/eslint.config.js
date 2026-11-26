import vue from "eslint-plugin-vue";
import ts from "@typescript-eslint/parser";

export default [
  ...vue.configs["flat/essential"],
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: ts } },
    rules: { "vue/multi-word-component-names": "off" },
  },
];
