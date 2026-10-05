"""Bind one owned isolated ERP to the workbench, without printing credentials."""

import argparse
import json
from pathlib import Path

import requests

from holmes.erpnext_support.security import safe_path
from holmes.erpnext_support.settings import EXAMPLE, Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lab-config', type=Path, required=True)
    parser.add_argument('--settings', type=Path, default=EXAMPLE / '.runtime/workbench/settings.json')
    parser.add_argument('--collector', action='store_true')
    args = parser.parse_args()
    config_path = safe_path(EXAMPLE / 'lab/.runtime', args.lab_config)
    config = json.loads(config_path.read_text())
    if config_path.parent.name != config['project'] or not config['project'].startswith('holmes-erp-'):
        raise ValueError('只绑定本模块初始化的隔离项目')
    ownership = json.loads(safe_path(config_path.parent, config_path.parent / 'ownership.json').read_text())
    if not ownership.get('owned') or ownership.get('closed'):
        raise ValueError('实验资源未持有或已清理')
    settings = Settings.load(args.settings)
    secret = json.loads(safe_path(config_path.parent, config_path.parent / 'secret.json').read_text())
    destination = safe_path(settings.runtime, settings.runtime / 'credentials/erp-lab.json')
    if destination.exists():
        if json.loads(destination.read_text()) != secret:
            raise ValueError('现有erp-lab凭据不同，拒绝替换')
    else:
        with destination.open('x', encoding='utf-8') as output:
            json.dump(secret, output)
    settings.credentials('erp-lab')
    collector = None
    if args.collector:
        source = safe_path(EXAMPLE / '.runtime', EXAMPLE / '.runtime/collector/collector.json')
        collector = json.loads(source.read_text())
        directory = safe_path(settings.runtime, settings.runtime / 'collectors')
        directory.mkdir(exist_ok=True)
        target = directory / 'erp-lab.json'
        if target.exists():
            if json.loads(target.read_text()) != collector:
                raise ValueError('现有采集器凭据不同，拒绝替换')
        else:
            with target.open('x', encoding='utf-8') as output:
                json.dump(collector, output)
        settings.collector_credentials('erp-lab')
    login = json.loads(safe_path(settings.runtime, settings.runtime / 'initial-login.json').read_text())
    with requests.Session() as session:
        session.trust_env = False
        response = session.post('http://127.0.0.1:8765/api/session', json=login, timeout=10)
        response.raise_for_status()
        session.headers.update({'X-CSRF-Token': response.json()['csrf_token'], 'Origin': 'http://127.0.0.1:8765'})
        response = session.get('http://127.0.0.1:8765/api/environments', timeout=10)
        response.raise_for_status()
        payload = {'name': '本机 ERPNext 隔离实验', 'base_url': 'http://127.0.0.1:8090', 'credential_ref': 'erp-lab',
                   'collector_ref': 'erp-lab' if collector else None, 'erpnext_version': '15.85.1', 'frappe_version': '15.87.0',
                   'installed_apps': ['frappe','erpnext','holmes_readonly'], 'log_source': 'erp-api',
                   'allowed_services': ['application','database','redis'] if collector else ['application'], 'model_consent': False}
        existing = next((e for e in response.json()['items'] if e['credential_ref'] == 'erp-lab'), None)
        if existing:
            raise ValueError('环境已登记。请在环境配置页检查后编辑，不自动覆盖现有配置。')
        response = session.post('http://127.0.0.1:8765/api/environments', json=payload, timeout=10)
        response.raise_for_status()
        print(json.dumps({'environment_id': response.json()['id'], 'model_consent': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
