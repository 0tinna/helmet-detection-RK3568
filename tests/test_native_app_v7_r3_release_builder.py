"""Immutable native-app v7-r3 direct LAN dashboard release contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


REPO = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPO / 'docs/tools/build_native_app_bundle_v7_r3.py'
VERSION = '20261124-v7-r3'
FINAL_ROOT = '/userdata/helmet-validation/20261124-native-app-v7-r3'
CODE_FILES = ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')
ALERT_FILES = (
    'alert_events.py',
    'alert_viewer.py',
    'alert_hub.py',
    'service.py',
    'launch.py',
    'resident.py',
)
REMOVED_FILES = (
    'alert_mail.py',
    'mail_probe.py',
    'mail.example.json',
    'mail.json',
    'event-access.key',
)


def load_builder():
    spec = importlib.util.spec_from_file_location(
        'build_native_app_bundle_v7_r3', BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(payload, names):
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode('ascii') + b'\0')
        digest.update(hashlib.sha256(payload['src/app/' + name]).digest())
    return digest.hexdigest()


class NativeAppV7R3ReleaseBuilderTests(unittest.TestCase):
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

    def test_bundle_contains_direct_event_runtime_without_mail_or_keys(self):
        builder = load_builder()
        payload = builder.collect_payload(REPO)
        for name in ALERT_FILES:
            self.assertIn('src/app/' + name, payload)
        for name in REMOVED_FILES:
            self.assertNotIn('src/app/' + name, payload)
        self.assertFalse(any('__pycache__' in name for name in payload))
        self.assertFalse(any(name.endswith(('.pyc', '.pyo')) for name in payload))
        readme = payload['README.txt'].decode('utf-8')
        self.assertIn('same LAN', readme)
        self.assertIn('http://BOARD_LAN_IP:8092/', readme)
        self.assertNotIn('SMTP', readme)
        self.assertNotIn('mail_probe', readme)
        self.assertNotIn('access invite', readme)
        self.assertIn(
            'docs/未戴安全帽告警与内网事件站-v7-r3.md', payload)
        self.assertNotIn(
            'docs/未戴安全帽告警、邮件与内网事件站-v7-r2.md', payload)

    def test_checked_app_paths_include_alert_runtime_and_exclude_mail(self):
        deploy = (REPO / 'helmet/app/deploy.ps1').read_text(encoding='utf-8')
        install = (REPO / 'helmet/app/install.py').read_text(encoding='utf-8')
        for name in ('alert_events.py', 'alert_hub.py', 'alert_viewer.py'):
            self.assertIn(name, deploy)
            self.assertIn(name, install)
        for name in ('alert_mail.py', 'mail_probe.py', 'mail.example.json'):
            self.assertNotIn(name, deploy)
            self.assertNotIn(name, install)

    def test_alert_identity_tracks_the_direct_viewer_runtime(self):
        builder = load_builder()
        payload = builder.collect_payload(REPO)
        expected = identity(payload, ALERT_FILES)
        self.assertEqual(
            builder.alert_identity_from_payload(payload), expected)
        changed = dict(payload)
        changed['src/app/alert_viewer.py'] += b'\n# identity fixture\n'
        self.assertNotEqual(
            builder.alert_identity_from_payload(changed), expected)
        self.assertEqual(
            builder.code_identity_from_payload(changed),
            builder.code_identity_from_payload(payload),
        )

    def test_manifest_is_truthful_about_the_direct_lan_viewer(self):
        builder = load_builder()
        payload = builder.collect_payload(REPO)
        manifest = builder.make_manifest(payload)
        self.assertEqual(manifest['version'], VERSION)
        self.assertEqual(manifest['release_tag'], 'native-app-v7-r3')
        self.assertEqual(manifest['deployment_root'], FINAL_ROOT)
        self.assertEqual(manifest['lan_event_port'], 8092)
        self.assertTrue(manifest['lan_only_event_viewer'])
        self.assertFalse(manifest['public_event_viewer_supported'])
        self.assertFalse(manifest['event_viewer_auth_required'])
        self.assertTrue(manifest['alert_event_feature'])
        self.assertNotIn('smtp_credentials_included', manifest)
        self.assertNotIn('event_access_key_included', manifest)
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
