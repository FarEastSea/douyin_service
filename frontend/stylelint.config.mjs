import stylelint from "stylelint";

const rule = "studio/no-duplicate-media";
const messages = stylelint.utils.ruleMessages(rule, {
  repeated: (query) => `Merge repeated @media ${query}`,
});
const uniqueMedia = stylelint.createPlugin(rule, () => (root, result) => {
  const queries = new WeakMap();
  root.walkAtRules("media", (node) => {
    const seen = queries.get(node.parent) || new Set();
    const query = node.params.replace(/\s+/g, "");
    if (seen.has(query))
      stylelint.utils.report({
        ruleName: rule,
        result,
        node,
        message: messages.repeated(node.params),
      });
    seen.add(query);
    queries.set(node.parent, seen);
  });
});
export default {
  plugins: [uniqueMedia],
  rules: {
    [rule]: true,
    "declaration-no-important": true,
    "no-duplicate-selectors": true,
    "declaration-block-no-duplicate-properties": true,
  },
};
