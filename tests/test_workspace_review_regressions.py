"""Synthetic package/protocol boundaries; no installed Skills or SSH calls."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.sources import capture
from tests import test_workspace_sources as fixtures


class TestReviewBoundaries(unittest.TestCase):
    setUp = fixtures.TestWorkspaceSources.setUp

    def test_selected_descendant_cannot_hide_an_external_directory_link(self):
        foreign = self.root / 'foreign'
        (foreign / 'alpha').mkdir(parents=True)
        (foreign / 'alpha/SKILL.md').write_text('---\nname: alpha\ndescription: fixture\n---\n')
        (self.source / 'bridge').symlink_to(foreign, target_is_directory=True)
        binding = dict(self.binding, include=['bridge/alpha'], entries={'alpha': 'bridge/alpha'})
        with patch('skills_auditor.workspace.sources.os.walk', side_effect=AssertionError('external directory traversed')):
            with self.assertRaises(LifecycleError) as caught:
                capture(binding)
        self.assertEqual(caught.exception.code, 'workspace_source')

    def test_selected_descendant_can_use_an_internal_directory_alias(self):
        (self.source / 'bridge').symlink_to('skills', target_is_directory=True)
        packet = capture(dict(self.binding, include=['bridge/alpha'], entries={'alpha': 'bridge/alpha'}))
        self.assertEqual(packet['entries'], {'alpha': 'bridge/alpha'})

    def test_nonobject_worker_request_returns_protocol_error_without_state(self):
        for raw in ('[]', 'null', '"string"'):
            result = subprocess.run([sys.executable, '-B', '-m', 'skills_auditor.workspace.worker'],
                                    input=raw, text=True, capture_output=True, timeout=10,
                                    cwd=Path(__file__).resolve().parents[1])
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['code'], 'workspace_protocol')
            self.assertEqual(result.stderr, '')
