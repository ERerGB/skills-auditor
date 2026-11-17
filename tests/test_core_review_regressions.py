"""Behavioral regressions discovered during the full source review."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor import cli


class CoreReviewRegressions(unittest.TestCase):
    def test_git_without_remote_is_not_reported_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            result = cli.check_drift_for_path('demo', root)
            self.assertFalse(result.synced)
            self.assertTrue(result.error)
            self.assertEqual(str(root.resolve()), result.display_target)

    def test_failed_git_status_is_not_reported_clean(self):
        def git(args, cwd):
            if args == ['rev-parse', '--show-toplevel']: return str(cwd)
            if args == ['branch', '--show-current']: return 'main'
            if args == ['remote', 'get-url', 'origin']: return 'https://example.invalid/repo.git'
            if args[0] == 'status': return None
            if args[0] == 'rev-list': return '0'
            return ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, '_git', side_effect=git):
            result = cli.check_drift_for_path('demo', Path(tmp))
            self.assertFalse(result.synced)
            self.assertTrue(result.error)

    def test_name_in_body_is_not_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / 'fallback' / 'SKILL.md'
            skill.parent.mkdir()
            skill.write_text('---\ndescription: example\n---\n```yaml\nname: unrelated-example\n```\n')
            self.assertEqual('fallback', cli.parse_skill_name(skill))

    def test_yaml_comments_do_not_change_valid_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / 'SKILL.md'
            skill.write_text('---\nname: example # display name\ndescription: "Read # headings" # inline comment\n---\n')
            self.assertEqual('example', cli.parse_skill_name(skill))
            self.assertEqual([], cli.validate_skill_metadata(skill))
            self.assertEqual([], cli.repair_skill_metadata(skill))

    def test_sync_mapping_rejects_escaping_alias_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'canonical'
            target.mkdir()
            (target / 'SKILL.md').write_text('---\nname: demo\ndescription: demo\n---\n')
            for alias in ['../outside', str(root / 'absolute'), '.', '..', 'nested/name']:
                with self.subTest(alias=alias), self.assertRaises(ValueError):
                    cli.plan_sync(root / 'installed', {alias: str(target)})
            self.assertFalse((root / 'installed').exists())

    def test_apply_rejects_escaping_alias_before_any_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actions = [cli.SyncAction('valid', str(root), 'create_link', ''),
                       cli.SyncAction('../outside', str(root), 'create_link', '')]
            with self.assertRaises(ValueError):
                cli.apply_actions(root / 'installed', actions)
            self.assertFalse((root / 'installed').exists())

    def test_duplicate_detection_includes_top_level_symlink_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installs = root / 'installed'
            installs.mkdir()
            for label in ['first', 'second']:
                source = root / label
                source.mkdir()
                (source / 'SKILL.md').write_text('---\nname: duplicate\ndescription: demo\n---\n' + label)
                (installs / label).symlink_to(source, target_is_directory=True)
            findings = cli.collect_duplicate_skill_names(installs)
            self.assertEqual(1, len(findings))
            self.assertEqual('duplicate', findings[0].skill_name)
            self.assertEqual(2, len(findings[0].skill_md_paths))

    def test_artifact_install_keeps_declared_runtime_dependencies(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_artifact_tests.py"
        spec = importlib.util.spec_from_file_location("artifact_runner_review", script)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = root / "suite-source"
            suite.mkdir()
            wheel = root / "synthetic.whl"
            installed_cli = root / "skills-audit"
            installed_cli.touch()
            commands = []
            with patch.dict(runner.LAYERS, {"smoke": suite}), \
                    patch.object(runner.venv.EnvBuilder, "create"), \
                    patch.object(runner, "environment_paths", return_value=(root / "python", installed_cli, root / "pip")), \
                    patch.object(runner, "wheel_version", return_value="0.0.0"), \
                    patch.object(runner, "checked_run", side_effect=lambda argv, **kw: commands.append(argv)):
                runner.run_layer("smoke", wheel)
            self.assertEqual("install", commands[0][1])
            self.assertNotIn("--no-deps", commands[0])
            self.assertEqual(str(wheel), commands[0][-1])

    def test_ledger_id_cannot_escape_storage_root(self):
        from skills_auditor.ledger import create_ledger
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                create_ledger(run_id='../outside', ledger_root=root / 'ledgers')
            self.assertFalse((root / 'outside.json').exists())

    def test_routing_traces_have_unique_ids_with_frozen_clock(self):
        from skills_auditor.state_machine import RunTrace, write_trace
        with tempfile.TemporaryDirectory() as tmp, patch('skills_auditor.state_machine.datetime') as clock:
            clock.now.return_value.strftime.return_value = '2026-11-18T00-00-00Z'
            clock.now.return_value.isoformat.return_value = '2026-11-18T00:00:00Z'
            first, second = RunTrace(), RunTrace()
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertNotEqual(write_trace(first, Path(tmp)), write_trace(second, Path(tmp)))
            self.assertEqual(2, len(list(Path(tmp).glob('*.json'))))

    def test_receipt_cannot_overwrite_source_after_verification(self):
        from skills_auditor.integration import IntegrationSpec, IntegrationTarget, IntegrationError, build_integration_plan, apply_integration_plan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'source' / 'demo'
            source.mkdir(parents=True)
            entry = source / 'SKILL.md'
            original = '---\nname: demo\ndescription: demo\n---\n'
            entry.write_text(original)
            target = root / 'installed'
            plan = build_integration_plan(IntegrationSpec(root, (source,), (IntegrationTarget('custom', root=target),)))
            with self.assertRaises(IntegrationError):
                apply_integration_plan(plan, receipt_output=entry)
            self.assertEqual(original, entry.read_text())
            self.assertFalse(target.exists())

    def test_sensor_provider_cannot_escape_log_root(self):
        from skills_auditor.observability import SensorEvent, write_sensor_event
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                write_sensor_event(SensorEvent(provider='../../../outside', event_type='stop'), root)
            self.assertFalse((root / 'sensors').exists())

    def test_trace_id_cannot_escape_trace_root(self):
        from skills_auditor.state_machine import RunTrace, write_trace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                write_trace(RunTrace(run_id='../outside'), root / 'traces')
            self.assertFalse((root / 'outside.json').exists())

    def test_dependency_verification_matches_published_schema(self):
        import json
        import jsonschema
        from skills_auditor.integration import IntegrationSpec, IntegrationTarget, build_integration_plan, apply_integration_plan, verify_receipt
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('---\nname: demo\ndescription: demo\n---\n')
            dependency = root / 'dependency.txt'
            dependency.write_text('version one')
            plan = build_integration_plan(IntegrationSpec(root, (source,), (IntegrationTarget('custom', root=root / 'installed'),), required_files=(dependency,)))
            receipt, _ = apply_integration_plan(plan)
            schema_path = Path(__file__).resolve().parents[1] / 'skills_auditor/schemas/integration-verification-v1.schema.json'
            schema = json.loads(schema_path.read_text())
            clean = verify_receipt(receipt)
            self.assertEqual('passed', clean['status'])
            jsonschema.validate(clean, schema)
            dependency.write_text('changed')
            changed = verify_receipt(receipt)
            self.assertEqual(['required_file'], changed['approval']['reason_codes'])
            jsonschema.validate(changed, schema)

    def test_yaml_doubled_single_quote_preserves_literal_hash(self):
        value = "'Bob''s # literal' # trailing"
        self.assertEqual("'Bob''s # literal'", cli._strip_scalar_comment(value))

    def test_route_missing_platform_keeps_primary_without_illegal_transition(self):
        from skills_auditor.state_machine import audit_traces
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / 'skills'
            for platform in ('.agents', '.factory'):
                entry = skills / 'bundle' / platform / 'skills' / 'demo' / 'SKILL.md'
                entry.parent.mkdir(parents=True)
                entry.write_text('---\nname: demo\ndescription: fixture\n---\n' + platform)
            trace, _ = cli.route_pipeline(skills, 'cursor', trace_dir=root / 'traces')
            self.assertEqual(1, len(trace.identities))
            self.assertTrue(trace.identities[0].final_selected)
            self.assertFalse([finding for finding in audit_traces([trace]) if finding.severity == 'error'])

    def test_receipt_rejects_source_case_alias_before_target_writes(self):
        from skills_auditor.integration import IntegrationSpec, IntegrationTarget, IntegrationError, build_integration_plan, apply_integration_plan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'source'
            source.mkdir()
            entry = source / 'SKILL.md'
            original = '---\nname: demo\ndescription: demo\n---\n'
            entry.write_text(original)
            target = root / 'installed'
            plan = build_integration_plan(IntegrationSpec(root, (source,), (IntegrationTarget('custom', root=target),)))
            with self.assertRaises(IntegrationError):
                apply_integration_plan(plan, receipt_output=root / 'SOURCE' / 'SKILL.md')
            self.assertEqual(original, entry.read_text())
            self.assertFalse(target.exists())
