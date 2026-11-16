import unittest
from pathlib import Path

SKILL = (Path(__file__).resolve().parent.parent / "skills" / "lead" / "SKILL.md").read_text(encoding="utf-8")


class LeadWindowDispatch(unittest.TestCase):
    def test_refused_window_gets_card_not_silent_subagent(self):
        self.assertIn("不许悄悄改派后台子代理", SKILL)
        self.assertIn("own_initiative", SKILL)

    def test_clear_scope_skips_scouting(self):
        self.assertIn("不先派侦察", SKILL)
        self.assertIn("范围清楚就不侦察", SKILL)

    def test_window_builds_its_own_worktree(self):
        self.assertIn("use_worktree 为 true", SKILL)
        self.assertIn("信任工作区", SKILL)


if __name__ == "__main__":
    unittest.main()
