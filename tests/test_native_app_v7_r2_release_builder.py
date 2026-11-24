"""Immutable native-app v7-r2 LAN-only release builder contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


REPO = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPO / 'docs/tools/build_native_app_bundle_v7_r2.py'
VERSION = '20261124-v7-r2'
FINAL_ROOT = '/userdata/helmet-validation/20261124-native-app-v7-r2'
CODE_FILES = ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')
ALERT_FILES = (
    'alert_events.py',
    'alert_mail.py',
    'alert_viewer.py',
    'alert_hub.py',
    'mail_probe.py',
    'mail.example.json',
    'service.py',
    'launch.py',
    'resident.py',
)


def load_builder():
    spec = importlib.util.spec_from_file_location(
        'build_native_app_bundle_v7_r2', BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(payload, names):
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode('ascii') + b'\0')
        digest.update(hashlib.sha256(payload['src/app/' + name]).digest())
    return digest.hexdigest()


class NativeAppV7ReleaseBuilderTests(unittest.TestCase):
    def test_new_immutable_version_root_and_vendor_wrappers(self):
        builder = load_builder()
        self.assertEqual(builder.VERSION, VERSION)
        self.assertEqual(builder.FINAL_ROOT.as_posix(), FINAL_ROOT)
        for tab in ('detection', 'wifi'):
            wrapper = builder.vendor_wrapper(tab).decode('utf-8')
            self.assertIn(FINAL_ROOT + '/src/app/launch.py', wrapper)
            self.assertIn('--root ' + FINAL_ROOT, wrapper)
            self.assertIn('--tab ' + tab, wrapper)
            self.assertNotIn('\\userdata', wrapper)

    def test_bundle_contains_alert_runtime_but_no_live_secrets(self):
        builder = load_builder()
        payload = builder.collect_payload(REPO)
        for name in ALERT_FILES:
            self.assertIn('src/app/' + name, payload)
        for secret in ('src/app/mail.json', 'src/app/event-access.key',
                       'src/app/app-state/mail.json',
                       'src/app/app-state/event-access.key'):
            self.assertNotIn(secret, payload)
        self.assertFalse(any('__pycache__' in name for name in payload))
        self.assertFalse(any(name.endswith(('.pyc', '.pyo')) for name in payload))
        self.assertIn(
            b'REPLACE_WITH_SMTP_AUTH_CODE',
            payload['src/app/mail.example.json'],
            'the example must be a visibly non-live template',
        )
        readme = payload['README.txt'].decode('utf-8')
        self.assertIn('same LAN', readme)
        self.assertIn('http://BOARD_LAN_IP:8092/', readme)
        self.assertNotIn('router forwarding', readme)
        self.assertIn(
            'docs/未戴安全帽告警、邮件与内网事件站-v7-r2.md', payload)
        self.assertNotIn('docs/未戴安全帽告警、邮件与公网事件站-v7.md', payload)

    def test_checked_app_install_paths_include_the_alert_runtime(self):
        deploy = (REPO / 'helmet/app/deploy.ps1').read_text(encoding='utf-8')
        install = (REPO / 'helmet/app/install.py').read_text(encoding='utf-8')
        for name in ('alert_events.py', 'alert_hub.py', 'alert_mail.py',
                     'alert_viewer.py', 'mail_probe.py',
                     'mail.example.json'):
            self.assertIn(name, deploy)
            self.assertIn(name, install)

    def test_identities_match_the_installed_five_file_contract(self):
        builder = load_builder()
        payload = builder.collect_payload(REPO)
        self.assertEqual(
            builder.code_identity_from_payload(payload),
            identity(payload, CODE_FILES),
        )
        expected = identity(payload, ALERT_FILES)
        self.assertEqual(
            builder.alert_identity_from_payload(payload), expected)
        changed = dict(payload)
        changed['src/app/alert_hub.py'] += b'\n# identity fixture\n'
        self.assertNotEqual(
            builder.alert_identity_from_payload(changed), expected)
        self.assertEqual(
            builder.code_identity_from_payload(changed),
            builder.code_identity_from_payload(payload),
        )

    def test_manifest_hashes_and_release_flags_are_truthful(self):
        builder = load_builder()
        payload = builder.collect_payload(REPO)
        manifest = builder.make_manifest(payload)
        self.assertEqual(manifest['version'], VERSION)
        self.assertEqual(manifest['release_tag'], 'native-app-v7-r2')
        self.assertEqual(manifest['deployment_root'], FINAL_ROOT)
        self.assertEqual(manifest['lan_event_port'], 8092)
        self.assertTrue(manifest['lan_only_event_viewer'])
        self.assertFalse(manifest['public_event_viewer_supported'])
        self.assertNotIn('public_event_port', manifest)
        self.assertNotIn('public_nat_verified', manifest)
        self.assertTrue(manifest['alert_event_feature'])
        self.assertFalse(manifest['smtp_credentials_included'])
        self.assertFalse(manifest['event_access_key_included'])
        self.assertFalse(manifest['physical_camera_alert_scenarios_tested'])
        for name, data in payload.items():
            row = manifest['files'][name]
            self.assertEqual(row['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(row['bytes'], len(data))

    def test_build_is_deterministic_and_never_overwrites_a_release(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = directory / 'first.zip'
            second = directory / 'second.zip'
            first_manifest = directory / 'first.json'
            second_manifest = directory / 'second.json'
            builder.build(REPO, first, first_manifest)
            builder.build(REPO, second, second_manifest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with self.assertRaises(FileExistsError):
                builder.build(REPO, first, directory / 'unused.json')
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertEqual(names, sorted(names))
                packaged = json.loads(archive.read('manifest.json'))
            self.assertEqual(packaged['version'], VERSION)
            self.assertTrue(first_manifest.is_file())
            self.assertTrue(second_manifest.is_file())


if __name__ == '__main__':
    unittest.main()
