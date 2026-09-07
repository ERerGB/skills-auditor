"""Published managed CLI recipes must continue to parse without filesystem effects."""

from pathlib import Path
import re
import shlex
import unittest

from skills_auditor.lifecycle.cli import _Parser, configure


class TestLifecycleDocumentation(unittest.TestCase):
    def test_every_managed_guide_shell_recipe_matches_the_shipped_parser(self):
        guide = Path(__file__).parents[1] / "docs/managed-lifecycle.md"
        content = guide.read_text(encoding="utf-8")
        commands = []
        for block in re.findall(r"```bash\n(.*?)\n```", content, flags=re.DOTALL):
            for line in block.replace("\\\n", " ").splitlines():
                arguments = shlex.split(line)
                if arguments:
                    self.assertEqual(arguments[:2], ["skills-audit", "lifecycle"])
                    commands.append(arguments[2:])
        self.assertGreaterEqual(len(commands), 20, "Keep executable recipes for every managed entry family.")
        families = set()
        for command in commands:
            with self.subTest(command=command):
                parsed = configure(_Parser()).parse_args(command)
                families.add(parsed.lifecycle_command)
        self.assertTrue({"plan", "apply", "verify", "preflight", "inspect", "recover", "batch",
                         "incidents", "investigate", "append-note", "invocation", "retention"}.issubset(families))


if __name__ == "__main__":
    unittest.main()
