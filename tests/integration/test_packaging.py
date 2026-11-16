"""Release metadata and installed-operator safety, synthetic inputs only."""
from pathlib import Path
import json
import runpy
import sys
import tomllib
from types import SimpleNamespace

import pytest

from research_assistant import __version__
from research_assistant.api.http import create_app
from research_assistant.assets import AssetError, extract_pdf
from research_assistant.operators import install_smoke


ROOT = Path(__file__).resolve().parents[2]


def test_one_version_drives_python_build_http_and_cli(capsys):
    from research_assistant.cli import main
    project = tomllib.loads((ROOT/'pyproject.toml').read_text())
    assert project['project']['dynamic'] == ['version']
    assert project['tool']['setuptools']['dynamic']['version']['attr'] == 'research_assistant.__version__'
    assert __version__ == '0.5.1'
    assert create_app(service=object(), api_token='synthetic').version == __version__
    with pytest.raises(SystemExit) as exit:
        main(['--version'])
    assert exit.value.code == 0 and capsys.readouterr().out.strip() == __version__


def test_cached_knowledge_library_operator_is_an_installed_entrypoint():
    project = tomllib.loads((ROOT/'pyproject.toml').read_text())
    assert 'configure_knowledge_library' in install_smoke.OPERATORS
    assert project['project']['scripts']['research-assistant-configure-knowledge-library'] == (
        'research_assistant.operators.configure_knowledge_library:main')


@pytest.mark.parametrize('name', install_smoke.OPERATORS)
def test_installed_operator_and_legacy_wrapper_share_main_and_help(name, monkeypatch, capsys):
    from importlib import import_module
    module = import_module('research_assistant.operators.' + name)
    project = tomllib.loads((ROOT/'pyproject.toml').read_text())
    assert project['project']['scripts']['research-assistant-' + name.replace('_', '-')] == module.__name__ + ':main'
    monkeypatch.setattr(sys, 'argv', [name, '--help'])
    with pytest.raises(SystemExit) as exit:
        runpy.run_path(str(ROOT/'scripts'/(name.replace('_', '-') + '.py')), run_name='__main__')
    assert exit.value.code == 0 and 'usage:' in capsys.readouterr().out
    wrapper = runpy.run_path(str(ROOT/'scripts'/(name.replace('_', '-') + '.py')), run_name='synthetic_wrapper')
    assert wrapper['main'] is module.main


def test_install_acceptance_pdf_is_original_synthetic_text_and_v2_is_literal_only():
    extraction = extract_pdf(install_smoke.synthetic_pdf())
    assert extraction['pages'][0]['text'].strip() == 'Synthetic installation quote.'
    result = install_smoke.local_v2()
    assert result == dict(status='resolved_by_local_text', basis='canonical_literal',
                          literal_hash_verified=True, geometry_claimed=False)


def test_install_smoke_volume_guard_precedes_creation_or_fixture_database(tmp_path, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise AssetError('external_storage_unavailable')
    monkeypatch.setattr(install_smoke, 'select_asset_root', unavailable)
    monkeypatch.setattr(install_smoke, 'smoke', lambda *a, **k: pytest.fail('unguarded smoke'))
    target = tmp_path/'must-not-exist'
    assert install_smoke.main(['--work-directory', str(target)]) == 1
    assert not target.exists()
    assert json.loads(capsys.readouterr().err)['error_code'] == 'installation_acceptance_unavailable'


def test_install_smoke_refuses_internal_directory_after_registered_guard(tmp_path, monkeypatch, capsys):
    mount = tmp_path/'synthetic-apfs';mount.mkdir()
    config = tmp_path/'runtime.json';config.write_text(json.dumps(dict(asset_storage=dict(mount_point=str(mount)))))
    monkeypatch.setattr(install_smoke, 'select_asset_root', lambda *a, **k: mount/'assets')
    monkeypatch.setattr(install_smoke, 'validate_external_storage', lambda *a, **k: None)
    monkeypatch.setattr(install_smoke, 'smoke', lambda *a, **k: pytest.fail('unguarded smoke'))
    target = tmp_path/'internal'
    assert install_smoke.main(['--runtime-config', str(config), '--work-directory', str(target)]) == 1
    assert not target.exists()
    assert 'installation_acceptance_unavailable' in capsys.readouterr().err


def test_pg_acceptance_admin_only_provisions_random_fixture_and_cleans_it_on_failure(tmp_path, monkeypatch):
    password = tmp_path/'admin-password';password.write_text('synthetic-admin-secret');password.chmod(0o600)
    statements, urls = [], []
    class Connection:
        dialect = SimpleNamespace(identifier_preparer=SimpleNamespace(quote=lambda value: '"'+value+'"'))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement): statements.append(str(statement))
    class Engine:
        def connect(self): return Connection()
        def dispose(self): pass
    def engine(url, **kwargs):
        urls.append(url);return Engine()
    monkeypatch.setattr(install_smoke, 'create_engine', engine)
    runtime = dict(database_url='postgresql+psycopg://synthetic_app:synthetic@127.0.0.1:54321/real_do_not_modify', port=54321)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        with install_smoke.fixture_database(runtime, tmp_path, True, password) as target:
            assert 'real_do_not_modify' not in target and 'synthetic_app' in target
            raise RuntimeError('synthetic failure')
    assert len(statements) == 3
    assert statements[0].startswith('CREATE DATABASE "ra_install_') and statements[0].endswith(' OWNER "synthetic_app"')
    assert statements[1] == 'CREATE EXTENSION vector WITH SCHEMA public'
    assert statements[2] == statements[0].replace('CREATE', 'DROP', 1).removesuffix(' OWNER "synthetic_app"')
    assert all(url.username == 'scholars_admin' for url in urls)
    assert all('ALTER ROLE' not in statement and 'real_do_not_modify' not in statement for statement in statements)
