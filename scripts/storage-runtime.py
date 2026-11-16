#!/usr/bin/env python3
"""Mount or safely detach the registered private external APFS image.

This utility never creates images, directories, databases, or fallback stores.
Only the dedicated PostgreSQL container may be stopped during unmount; API
processes and other containers are not managed here. Command diagnostics and
private runtime configuration are never forwarded to the terminal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from research_assistant.assets import AssetError
from research_assistant.storage_paths import disk_info, validate_external_storage


NAME = 'scholars-attendant-postgres'
LABEL = 'org.scholars-attendant.component'
DATA_DESTINATION = '/var/lib/postgresql/data'
CONTAINER_FORMAT = ('{"id":{{json .Id}},"labels":{{json .Config.Labels}},'
                    '"mounts":{{json .Mounts}},"running":{{json .State.Running}}}')
_CONTAINER_ID = re.compile(r'[a-f0-9]{64}\Z')
_SAFE_ERRORS = frozenset({
    'storage_configuration_unavailable', 'external_storage_unavailable',
    'external_storage_mismatch', 'storage_mount_failed', 'storage_unmount_failed',
    'storage_unmount_busy',
    'postgres_container_unavailable', 'postgres_container_mismatch',
    'postgres_stop_failed', 'storage_runtime_unavailable',
})


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or '\0' in str(value):
        raise AssetError('external_storage_mismatch')
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise AssetError('external_storage_mismatch')
        return Path(os.path.abspath(path))
    except (OSError, ValueError, TypeError):
        raise AssetError('external_storage_mismatch') from None


def _no_symlinks(path):
    try:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise AssetError('external_storage_mismatch')
    except OSError:
        raise AssetError('external_storage_unavailable') from None


def load_config(path):
    try:
        config = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise AssetError('storage_configuration_unavailable') from None
    if not isinstance(config, dict):
        raise AssetError('storage_configuration_unavailable')
    return config


def storage_plan(config):
    """Normalize declared roots and require one consistent registered image."""
    targets = []
    if config.get('asset_storage') is not None:
        targets.append((config.get('asset_root'), config['asset_storage']))
    if config.get('postgres_storage') is not None:
        storage = config['postgres_storage']
        if not isinstance(storage, dict) or storage.get('kind') != 'bind':
            raise AssetError('external_storage_mismatch')
        targets.append((storage.get('data_dir'), storage))
    if not targets:
        raise AssetError('storage_configuration_unavailable')

    normalized = []
    identity = None
    for root, original in targets:
        if not isinstance(original, dict):
            raise AssetError('external_storage_mismatch')
        storage = dict(original)
        root = _path(root)
        for key in ('mount_point', 'backing_image_path', 'backing_volume_mountpoint'):
            storage[key] = str(_path(storage.get(key)))
        for key in ('volume_uuid', 'backing_volume_uuid'):
            if not isinstance(storage.get(key), str) or not storage[key].strip():
                raise AssetError('external_storage_mismatch')
        mount = Path(storage['mount_point'])
        backing = Path(storage['backing_volume_mountpoint'])
        image = Path(storage['backing_image_path'])
        if (root == mount or not root.is_relative_to(mount)
                or image == backing or not image.is_relative_to(backing)
                or image.suffix.casefold() != '.sparsebundle'):
            raise AssetError('external_storage_mismatch')
        if storage.get('root') is not None and _path(storage['root']) != root:
            raise AssetError('external_storage_mismatch')
        for path in (root, mount, backing, image):
            _no_symlinks(path)
        current = (mount, storage['volume_uuid'].casefold(), image,
                   backing, storage['backing_volume_uuid'].casefold())
        if identity is not None and current != identity:
            raise AssetError('external_storage_mismatch')
        identity = current
        normalized.append((root, storage))
    return {'targets': normalized, 'mount': identity[0], 'image': identity[2],
            'backing': identity[3], 'backing_uuid': identity[4]}


def validate_backing(plan, *, probe=None, is_mount=None):
    """Check the backing volume before hdiutil can attach anything from the registered path."""
    probe = probe or disk_info
    is_mount = is_mount or os.path.ismount
    try:
        if not is_mount(plan['backing']) or not plan['image'].is_dir():
            raise AssetError('external_storage_unavailable')
        info = probe(plan['backing'])
        if not isinstance(info, dict):
            raise AssetError('external_storage_unavailable')
        uuid = info.get('VolumeUUID')
        if not isinstance(uuid, str) or uuid.casefold() != plan['backing_uuid']:
            raise AssetError('external_storage_mismatch')
        if info.get('MountPoint') is not None and _path(info['MountPoint']) != plan['backing']:
            raise AssetError('external_storage_mismatch')
        if info.get('ReadOnlyVolume') is True or info.get('ReadOnly') is True:
            raise AssetError('external_storage_unavailable')
    except AssetError:
        raise
    except Exception:
        raise AssetError('external_storage_unavailable') from None


def validate_mounted(plan, *, probe=None, is_mount=None):
    for root, storage in plan['targets']:
        validate_external_storage(root, storage, probe=probe, is_mount=is_mount)


def command(args, *, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=timeout, check=False)
        if len(result.stdout) > 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            raise AssetError('storage_runtime_unavailable')
        return result
    except AssetError:
        raise
    except Exception:
        raise AssetError('storage_runtime_unavailable') from None


def postgres_snapshot(config, *, runner=None, target=NAME):
    """Inspect only ownership, data mount, state, and immutable container ID."""
    storage = config.get('postgres_storage')
    if (not isinstance(storage, dict) or storage.get('kind') != 'bind'
            or config.get('container', NAME) != NAME):
        raise AssetError('postgres_container_mismatch')
    expected = _path(storage.get('data_dir'))
    result = (runner or command)(['docker', 'container', 'inspect', '--format',
                                  CONTAINER_FORMAT, target], timeout=10)
    if result.returncode:
        raise AssetError('postgres_container_unavailable')
    try:
        snapshot = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise AssetError('postgres_container_unavailable') from None
    if not isinstance(snapshot, dict):
        raise AssetError('postgres_container_unavailable')
    labels = snapshot.get('labels')
    identifier = snapshot.get('id')
    mounts = snapshot.get('mounts')
    if (not isinstance(labels, dict) or labels.get(LABEL) != 'postgres'
            or not isinstance(identifier, str) or not _CONTAINER_ID.fullmatch(identifier)
            or type(snapshot.get('running')) is not bool or not isinstance(mounts, list)
            or (target != NAME and identifier != target)):
        raise AssetError('postgres_container_mismatch')
    data = [item for item in mounts if isinstance(item, dict)
            and item.get('Destination') == DATA_DESTINATION]
    if len(data) != 1 or data[0].get('Type') != 'bind' or data[0].get('RW') is not True:
        raise AssetError('postgres_container_mismatch')
    source = data[0].get('Source')
    # Docker Desktop may report the VM-side path for this exact host prefix.
    if isinstance(source, str) and source.startswith('/host_mnt/Volumes/'):
        source = source[len('/host_mnt'):]
    try:
        if _path(source) != expected:
            raise AssetError('postgres_container_mismatch')
    except AssetError:
        raise AssetError('postgres_container_mismatch') from None
    return snapshot


def operate(action, config, *, probe=None, is_mount=None, runner=None):
    is_mount = is_mount or os.path.ismount
    runner = runner or command
    plan = storage_plan(config)
    validate_backing(plan, probe=probe, is_mount=is_mount)
    if action == 'mount':
        if not is_mount(plan['mount']):
            result = runner(['/usr/bin/hdiutil', 'attach', str(plan['image']),
                             '-owners', 'on', '-nobrowse'], timeout=50)
            if result.returncode:
                raise AssetError('storage_mount_failed')
        validate_mounted(plan, probe=probe, is_mount=is_mount)
        return {'status': 'ready', 'mounted': True, 'external_storage_verified': True}
    validate_mounted(plan, probe=probe, is_mount=is_mount)
    if action == 'status':
        return {'status': 'ready', 'mounted': True, 'external_storage_verified': True}
    if action != 'unmount':
        raise AssetError('storage_runtime_unavailable')

    snapshot = postgres_snapshot(config, runner=runner)
    if snapshot['running']:
        result = runner(['docker', 'container', 'stop', '--time', '30', snapshot['id']], timeout=45)
        if result.returncode:
            raise AssetError('postgres_stop_failed')
        stopped = postgres_snapshot(config, runner=runner, target=snapshot['id'])
        if stopped['running']:
            raise AssetError('postgres_stop_failed')
    # Recheck identities after stopping and immediately before detach.
    validate_mounted(plan, probe=probe, is_mount=is_mount)
    result = runner(['/usr/bin/hdiutil', 'detach', str(plan['mount'])], timeout=30)
    if result.returncode == 16:
        raise AssetError('storage_unmount_busy')
    if result.returncode or is_mount(plan['mount']):
        raise AssetError('storage_unmount_failed')
    return {'status': 'unmounted', 'mounted': False, 'postgres_status': 'stopped'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['mount', 'status', 'unmount'])
    parser.add_argument('--runtime-config', type=Path,
        default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    args = parser.parse_args(argv)
    try:
        result = operate(args.command, load_config(args.runtime_config))
    except Exception as error:
        code = error.code if isinstance(error, AssetError) and error.code in _SAFE_ERRORS else 'storage_runtime_unavailable'
        print(json.dumps({'status': 'failed', 'error_code': code}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
