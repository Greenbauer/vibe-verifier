"""`bin/vibe-verifier merge-grant`: the default policy's workflow-security exclusion, line by line."""
import unittest

from test_merge_grant import WF, WF_BASE, decide


class WorkflowLines(unittest.TestCase):
    def verdict(self, head, base=WF_BASE, paths=(WF,)):
        return decide(list(paths), base={WF: base} if base else {}, head={WF: head} if head else {})[0]

    def test_allowed_edits(self):
        for head in (WF_BASE + "\n      - run: npm run lint",
                     WF_BASE + '\n        env: { GH_TOKEN: "${{ github.token }}" }',
                     WF_BASE + "\n  gates:\n    permissions:\n      contents: read\n      pull-requests: read\n"
                               "    steps:\n      - run: node gates.mjs\n        env:\n          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
                     WF_BASE + "\n  a:\n    permissions: {}\n  b:\n    permissions: read-all",
                     WF_BASE + '\n      - run: cat "${secrets}"'):
            self.assertEqual(self.verdict(head), "non-product grant", head)
        self.assertEqual(self.verdict(WF_BASE, base=None), "non-product grant")            # a new read-only workflow
        self.assertEqual(self.verdict(WF_BASE + "\n      - name: write the summary", base=WF_BASE + "\n      - name: read the docs"),
                         "non-product grant")
        self.assertEqual(self.verdict(WF_BASE + "\n      - run: out=$(jq -n '{a:1}' > \"$secrets\")",
                                      base=WF_BASE + "\n      - run: out=$(jq -n '{}' > \"$secrets\")"), "non-product grant")

    def test_lines_that_stay_a_persons(self):
        for head in (WF_BASE.replace("contents: read", "contents: write"),
                     WF_BASE.replace("permissions:\n  contents: read\n", ""),
                     WF_BASE.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-latest\n    permissions: write-all"),
                     WF_BASE + '\n        env: { NPM_TOKEN: "${{ secrets.NPM_TOKEN }}" }',
                     WF_BASE + "\n  call:\n    uses: ./.github/workflows/deploy.yml\n    secrets: inherit",
                     WF_BASE.replace("on: [pull_request]", "on: [pull_request_target]"),
                     WF_BASE.replace("contents: read", "contents: read\n  id-token: write"),
                     WF_BASE + '\n  pages:\n    permissions: { pages: "write", contents: read }',
                     WF_BASE + '\n        env: { A: "${{ secrets.GITHUB_TOKEN }}", B: "${{ secrets.NPM_TOKEN }}" }',
                     WF_BASE + '\n        env: { ALL: "${{ toJSON(secrets) }}" }',
                     WF_BASE + '\n        env: { FOO: "${{secrets.FOO}}" }',
                     WF_BASE + '\n      - run: echo "${{ secrets.FOO }}" > "$secrets"'):
            self.assertIsNone(self.verdict(head), head)
        self.assertIsNone(self.verdict(WF_BASE.replace("contents: read", "contents: write"), base=None))  # a new workflow

    def test_a_case_only_change_of_a_scope_line_is_still_a_change(self):
        self.assertIsNone(self.verdict(WF_BASE.replace("contents: read", "contents: write"),
                                       base=WF_BASE.replace("contents: read", "contents: WRITE")))

    def test_a_permissions_block_moved_to_another_job_is_removed_there(self):
        base = "jobs:\n  a:\n    runs-on: x\n    permissions:\n      contents: read\n  b:\n    runs-on: x"
        head = "jobs:\n  a:\n    runs-on: x\n  b:\n    runs-on: x\n    permissions:\n      contents: read"
        self.assertIsNone(self.verdict(head, base=base))
        self.assertEqual(self.verdict(base + "\n    timeout-minutes: 5", base=base), "non-product grant")

    def test_the_token_carve_out_and_line_breaks_cannot_hide_a_line(self):
        self.assertIsNone(self.verdict(WF_BASE + "\n      - run: echo ${{ secrets.GITHUB_TOKEN-secrets.NPM }}"))
        self.assertIsNone(self.verdict(WF_BASE + "\n      - run: echo ${{ secrets.GITHUB_TOKEN$secrets.NPM }}"))
        self.assertIsNone(self.verdict(WF_BASE.replace("contents: read", "contents:\fwrite")))

    def test_removed_permission_lines(self):
        two = WF_BASE.replace("contents: read", "contents: read\n  pull-requests: read")
        self.assertIsNone(self.verdict(WF_BASE, base=two))
        self.assertIsNone(self.verdict(WF_BASE.replace("  contents: read\n", ""), base=WF_BASE.replace("contents: read", "contents: write")))
        self.assertIsNone(self.verdict("permissions: read-all\njobs: {}", base="permissions: write-all\njobs: {}"))

    def test_a_workflow_renamed_out_of_github_is_read_at_its_old_path(self):
        grant, _ = decide([], renamed=[(WF, "docs/ci.md")], base={WF: WF_BASE}, head={"docs/ci.md": "The CI notes."})
        self.assertIsNone(grant)


if __name__ == "__main__":
    unittest.main()
