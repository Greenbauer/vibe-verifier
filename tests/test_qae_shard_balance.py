"""Balance late multi-role criteria through the existing QAE action harness."""
import unittest

import test_qae_shards as shard_tests


class Balance(unittest.TestCase):
    run_action = shard_tests.Share.run_action
    shares = shard_tests.Share.shares

    def test_late_multi_role_criteria_fit_with_the_feature_re_walk_without_renumbering(self):
        roles = {3: "admin, viewer", 17: "admin, viewer", 18: "admin, editor, viewer"}
        files = {"qae-inputs/pr-body.md": shard_tests.body(*(
            "Criterion %d holds [as: %s]" % (number, roles.get(number, "editor")) for number in range(1, 19)))}
        files.update({"qae-inputs/features/%s.md" % name: "# %s\n" % name for name in ("cart", "search", "sign-in")})
        shares = self.shares(files, 8)
        seen = []
        loads = []
        for index, (share, turns) in enumerate(shares):
            numbers = [int(criterion[2:]) for criterion in share.split(", ")]
            self.assertEqual(numbers, sorted(numbers))
            seen.extend(numbers)
            walks = sum(2 * len(roles.get(number, "editor").split(", ")) for number in numbers) + (3 if index == 0 else 0)
            loads.append(walks)
            self.assertLessEqual(walks, 6, share)
            self.assertEqual(turns, 40 + 30 * walks)
        self.assertEqual(sorted(seen), list(range(1, 19)))
        self.assertEqual(sum(loads), 47)

