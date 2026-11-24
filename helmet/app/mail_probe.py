#!/usr/bin/env python3
"""Send one explicit SMTP configuration probe without creating an event."""
import argparse
import json
from pathlib import Path

try:
    from .alert_mail import LanAddressResolver, MailSettings, SmtpAlertMailer
    from .alert_viewer import AlertAccessKey
except ImportError:
    from alert_mail import LanAddressResolver, MailSettings, SmtpAlertMailer
    from alert_viewer import AlertAccessKey


def build_mailer(root):
    root = Path(root).resolve()
    settings = MailSettings.load(root / 'app-state' / 'mail.json')
    resolver = LanAddressResolver()
    key_path = root / 'app-state' / 'event-access.key'
    access_key = None
    if key_path.is_file():
        try:
            access_key = AlertAccessKey.load_or_create(key_path)
        except (OSError, ValueError):
            access_key = None

    def access_url(host):
        if access_key is None:
            return None
        return access_key.url_path(host, lan_port=8092, scheme='http')

    return SmtpAlertMailer(settings, access_url, resolver)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args(argv)
    mailer = None
    try:
        mailer = build_mailer(args.root)
        result = mailer.probe()
    except (OSError, RuntimeError, ValueError, TypeError):
        print(json.dumps({
            'ok': False,
            'error': 'mail_probe_failed',
            'hint': '检查 app-state/mail.json、SMTP 授权码、端口与安全模式',
        }, ensure_ascii=False))
        return 1
    finally:
        if mailer is not None:
            mailer.close(0)
    print(json.dumps({
        'ok': True,
        'sent': result['sent'],
        'recipient': result['recipient'],
        'lan_address_available': result['lan_address_available'],
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
