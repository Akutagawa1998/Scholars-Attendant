"""Storage lifecycle uses synthetic volumes and command doubles only."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_assistant.assets import AssetError


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[2]/'scripts/storage-runtime.py'
    spec = importlib.util.spec_from_file_location('external_storage_runtime_cli', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def runtime(tmp_path, module):
    mount = tmp_path/'mounted-data'; mount.mkdir()
    backing = tmp_path/'Synthetic-Disk'; backing.mkdir()
    image = backing/'Scholars-Attendant/ResearchData.sparsebundle'; image.mkdir(parents=True)
    storage = dict(mount_point=str(mount), volume_uuid='inner-volume', backing_image_path=str(image),
                   backing_volume_mountpoint=str(backing), backing_volume_uuid='outer-volume')
    config = dict(container=module.NAME, asset_root=str(mount/'assets'), asset_storage=dict(storage),
                  postgres_storage=dict(storage, kind='bind', data_dir=str(mount/'postgres')))
    info = {mount: dict(VolumeUUID='INNER-VOLUME', FilesystemType='apfs', MountPoint=str(mount)),
            backing: dict(VolumeUUID='OUTER-VOLUME', FilesystemType='exfat', MountPoint=str(backing))}
    state = SimpleNamespace(mounted=True, backing_mounted=True, attach_mounts=True,
                            stop_stops=True, detach_unmounts=True, codes={}, calls=[],
                            snapshot={'id': 'a'*64, 'labels': {module.LABEL: 'postgres'},
                                      'running': True, 'mounts': [
                                          {'Type': 'bind', 'Source': config['postgres_storage']['data_dir'],
                                           'Destination': module.DATA_DESTINATION, 'RW': True}]})
    def probe(path): return info[path]
    def is_mount(path):
        return state.mounted if path == mount else state.backing_mounted if path == backing else False
    def runner(args, *, timeout):
        state.calls.append((args, timeout))
        if args[:3] == ['docker', 'container', 'inspect']:
            return SimpleNamespace(returncode=state.codes.get('inspect', 0),
                                   stdout=json.dumps(state.snapshot), stderr='PRIVATE diagnostic')
        if args[:3] == ['docker', 'container', 'stop']:
            code = state.codes.get('stop', 0)
            if not code and state.stop_stops: state.snapshot['running'] = False
        elif args[:2] == ['/usr/bin/hdiutil', 'attach']:
            code = state.codes.get('attach', 0)
            if not code and state.attach_mounts: state.mounted = True
        elif args[:2] == ['/usr/bin/hdiutil', 'detach']:
            code = state.codes.get('detach', 0)
            if not code and state.detach_unmounts: state.mounted = False
        else:
            pytest.fail('Unexpected command')
        return SimpleNamespace(returncode=code, stdout='PRIVATE command output', stderr='PRIVATE diagnostic')
    def operate(action):
        return module.operate(action, config, probe=probe, is_mount=is_mount, runner=runner)
    return SimpleNamespace(module=module, mount=mount, backing=backing, image=image, config=config,
                           state=state, info=info, probe=probe, is_mount=is_mount, runner=runner, operate=operate)


def commands(runtime):
    return [call[0] for call in runtime.state.calls]


def test_status_only_validates_without_commands_or_creating_roots(runtime):
    assert runtime.operate('status') == {'status': 'ready', 'mounted': True, 'external_storage_verified': True}
    assert not runtime.state.calls
    assert not Path(runtime.config['asset_root']).exists()
    assert not Path(runtime.config['postgres_storage']['data_dir']).exists()


@pytest.mark.parametrize('only', ['assets', 'postgres'])
def test_single_registered_store_can_be_mounted_or_checked(runtime, only):
    if only == 'assets': del runtime.config['postgres_storage']
    else: del runtime.config['asset_storage']; del runtime.config['asset_root']
    assert runtime.operate('mount')['mounted'] is True
    assert not runtime.state.calls


def test_mount_already_verified_volume_does_not_reattach(runtime):
    assert runtime.operate('mount')['status'] == 'ready'
    assert not runtime.state.calls


def test_mount_attaches_only_existing_image_with_owners_then_validates(runtime):
    runtime.state.mounted = False
    assert runtime.operate('mount')['mounted'] is True
    assert commands(runtime) == [['/usr/bin/hdiutil', 'attach', str(runtime.image), '-owners', 'on', '-nobrowse']]
    assert not Path(runtime.config['asset_root']).exists()


@pytest.mark.parametrize('failure', ['missing_backing', 'wrong_backing_uuid', 'missing_image', 'symlink_image'])
def test_backing_identity_checked_before_any_attach(runtime, failure, tmp_path):
    runtime.state.mounted = False
    if failure == 'missing_backing': runtime.state.backing_mounted = False
    elif failure == 'wrong_backing_uuid': runtime.info[runtime.backing]['VolumeUUID'] = 'other-disk'
    elif failure == 'missing_image': runtime.image.rmdir()
    else:
        runtime.image.rmdir(); runtime.image.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AssetError): runtime.operate('mount')
    assert not runtime.state.calls


def test_backing_guard_is_mandatory_even_if_inner_volume_is_still_mounted(runtime):
    runtime.info[runtime.backing]['VolumeUUID'] = 'other-disk'
    with pytest.raises(AssetError, match='external_storage_mismatch'): runtime.operate('status')
    assert not runtime.state.calls


@pytest.mark.parametrize('mutation', ['mount_point', 'volume_uuid', 'backing_image_path', 'backing_volume_uuid'])
def test_disagreeing_asset_and_database_identity_refuses_commands(runtime, mutation, tmp_path):
    runtime.config['asset_storage'][mutation] = str(tmp_path/'different') if mutation.endswith(('point', 'path')) else 'different'
    with pytest.raises(AssetError, match='external_storage_mismatch'): runtime.operate('mount')
    assert not runtime.state.calls


def test_missing_backing_uuid_does_not_attach(runtime):
    del runtime.config['asset_storage']['backing_volume_uuid']
    with pytest.raises(AssetError, match='external_storage_mismatch'): runtime.operate('mount')
    assert not runtime.state.calls


@pytest.mark.parametrize('failure', ['attach_failed', 'not_mounted', 'wrong_inner_uuid', 'non_apfs'])
def test_attach_cannot_report_success_without_mounted_apfs_identity(runtime, failure):
    runtime.state.mounted = False
    if failure == 'attach_failed': runtime.state.codes['attach'] = 1
    elif failure == 'not_mounted': runtime.state.attach_mounts = False
    elif failure == 'wrong_inner_uuid': runtime.info[runtime.mount]['VolumeUUID'] = 'other-image'
    else: runtime.info[runtime.mount]['FilesystemType'] = 'exfat'
    with pytest.raises(AssetError) as error: runtime.operate('mount')
    assert error.value.code in {'storage_mount_failed', 'external_storage_mismatch', 'external_storage_unavailable'}
    assert len(runtime.state.calls) == 1
    assert commands(runtime)[0][1] == 'attach'
    assert not Path(runtime.config['asset_root']).exists()


def test_unmounted_status_does_not_attach(runtime):
    runtime.state.mounted = False
    with pytest.raises(AssetError, match='external_storage_unavailable'): runtime.operate('status')
    assert not runtime.state.calls


def test_unmount_verifies_dedicated_database_then_stops_by_id_before_detach(runtime):
    assert runtime.operate('unmount') == {'status': 'unmounted', 'mounted': False, 'postgres_status': 'stopped'}
    assert commands(runtime) == [
        ['docker', 'container', 'inspect', '--format', runtime.module.CONTAINER_FORMAT, runtime.module.NAME],
        ['docker', 'container', 'stop', '--time', '30', 'a'*64],
        ['docker', 'container', 'inspect', '--format', runtime.module.CONTAINER_FORMAT, 'a'*64],
        ['/usr/bin/hdiutil', 'detach', str(runtime.mount)],
    ]
    assert '.Config.Env' not in runtime.module.CONTAINER_FORMAT


def test_already_stopped_verified_database_can_detach_without_stop(runtime):
    runtime.state.snapshot['running'] = False
    assert runtime.operate('unmount')['mounted'] is False
    assert len(runtime.state.calls) == 2
    assert commands(runtime)[-1][1] == 'detach'


@pytest.mark.parametrize('mutation', ['wrong_label', 'missing_id', 'wrong_source', 'named_volume', 'readonly',
                                     'duplicate_data_mount', 'relative_source', 'unrelated_container'])
def test_unmount_refuses_to_stop_or_detach_unmatched_database(runtime, mutation):
    snapshot = runtime.state.snapshot
    if mutation == 'wrong_label': snapshot['labels'][runtime.module.LABEL] = 'unrelated'
    elif mutation == 'missing_id': del snapshot['id']
    elif mutation == 'wrong_source': snapshot['mounts'][0]['Source'] = str(runtime.mount/'unrelated')
    elif mutation == 'named_volume': snapshot['mounts'][0]['Type'] = 'volume'
    elif mutation == 'readonly': snapshot['mounts'][0]['RW'] = False
    elif mutation == 'duplicate_data_mount': snapshot['mounts'].append(dict(snapshot['mounts'][0]))
    elif mutation == 'relative_source': snapshot['mounts'][0]['Source'] = 'postgres'
    else: runtime.config['container'] = 'unrelated'
    with pytest.raises(AssetError, match='postgres_container_mismatch'): runtime.operate('unmount')
    assert all(command[:3] == ['docker', 'container', 'inspect'] for command in commands(runtime))
    assert runtime.state.mounted is True and snapshot['running'] is True


def test_unmount_requires_registered_database_bind_store(runtime):
    del runtime.config['postgres_storage']
    with pytest.raises(AssetError, match='postgres_container_mismatch'): runtime.operate('unmount')
    assert not runtime.state.calls


def test_unmount_cannot_stop_unknown_or_missing_container(runtime):
    runtime.state.codes['inspect'] = 1
    with pytest.raises(AssetError, match='postgres_container_unavailable'): runtime.operate('unmount')
    assert len(runtime.state.calls) == 1 and runtime.state.mounted


@pytest.mark.parametrize('failure', ['stop_error', 'still_running'])
def test_unmount_requires_confirmed_database_stop(runtime, failure):
    if failure == 'stop_error': runtime.state.codes['stop'] = 1
    else: runtime.state.stop_stops = False
    with pytest.raises(AssetError, match='postgres_stop_failed'): runtime.operate('unmount')
    assert not any(command[:2] == ['/usr/bin/hdiutil', 'detach'] for command in commands(runtime))
    assert runtime.state.mounted


@pytest.mark.parametrize('failure', ['detach_error', 'still_mounted'])
def test_unmount_does_not_force_or_retry_failed_detach(runtime, failure):
    if failure == 'detach_error': runtime.state.codes['detach'] = 1
    else: runtime.state.detach_unmounts = False
    with pytest.raises(AssetError, match='storage_unmount_failed'): runtime.operate('unmount')
    assert commands(runtime)[-1] == ['/usr/bin/hdiutil', 'detach', str(runtime.mount)]
    assert runtime.state.snapshot['running'] is False


def test_busy_volume_remains_mounted_with_database_stopped(runtime):
    runtime.state.codes['detach'] = 16
    with pytest.raises(AssetError, match='storage_unmount_busy'):
        runtime.operate('unmount')
    assert runtime.state.mounted is True
    assert runtime.state.snapshot['running'] is False
    assert commands(runtime)[-1] == ['/usr/bin/hdiutil', 'detach', str(runtime.mount)]


def test_docker_desktop_host_prefix_requires_exact_volumes_prefix(runtime):
    config = copy.deepcopy(runtime.config)
    config['postgres_storage']['data_dir'] = '/Volumes/Synthetic-Data/postgres'
    runtime.state.snapshot['mounts'][0]['Source'] = '/host_mnt/Volumes/Synthetic-Data/postgres'
    assert runtime.module.postgres_snapshot(config, runner=runtime.runner)['id'] == 'a'*64
    runtime.state.snapshot['mounts'][0]['Source'] = '/unrelated/Volumes/Synthetic-Data/postgres'
    with pytest.raises(AssetError, match='postgres_container_mismatch'):
        runtime.module.postgres_snapshot(config, runner=runtime.runner)


@pytest.mark.parametrize('contents', ['PRIVATE malformed JSON', '["PRIVATE"]'])
def test_config_and_cli_failures_never_print_private_content(module, tmp_path, capsys, contents):
    config = tmp_path/'PRIVATE-runtime.json'; config.write_text(contents)
    assert module.main(['mount', '--runtime-config', str(config)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err) == {'status': 'failed', 'error_code': 'storage_configuration_unavailable'}
    assert 'PRIVATE' not in output.err


def test_cli_command_diagnostics_are_redacted(runtime, tmp_path, monkeypatch, capsys):
    config = tmp_path/'runtime.json'; config.write_text(json.dumps(runtime.config))
    monkeypatch.setattr(runtime.module, 'disk_info', runtime.probe)
    monkeypatch.setattr(runtime.module.os.path, 'ismount', runtime.is_mount)
    def broken(*args, **kwargs): raise RuntimeError('PRIVATE password and driver diagnostic')
    monkeypatch.setattr(runtime.module, 'command', broken)
    runtime.state.mounted = False
    assert runtime.module.main(['mount', '--runtime-config', str(config)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)['error_code'] == 'storage_runtime_unavailable'
    assert 'PRIVATE' not in output.err and str(runtime.image) not in output.err


def test_subprocess_failure_does_not_forward_diagnostics(module, monkeypatch):
    def broken(*args, **kwargs): raise OSError('PRIVATE driver diagnostic')
    monkeypatch.setattr(module.subprocess, 'run', broken)
    with pytest.raises(AssetError, match='storage_runtime_unavailable') as error:
        module.command(['synthetic'], timeout=1)
    assert 'PRIVATE' not in str(error.value)
