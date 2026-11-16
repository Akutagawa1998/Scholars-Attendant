"""Private content locations with fail-closed external-volume identity checks.

Selection and validation never create directories or mount disk images. A
missing configured disk must not turn into an internal-disk fallback directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
from typing import Callable, Mapping, Any

from .assets import AssetError


def disk_info(path: Path) -> dict:
    """Read macOS volume metadata without forwarding diskutil diagnostics."""
    if platform.system() != 'Darwin':
        raise AssetError('external_storage_unavailable')
    try:
        result=subprocess.run(['/usr/sbin/diskutil','info','-plist',str(path)],
            capture_output=True,timeout=10,check=False)
        if result.returncode or len(result.stdout)>1024*1024:
            raise AssetError('external_storage_unavailable')
        value=plistlib.loads(result.stdout)
        if not isinstance(value,dict):
            raise AssetError('external_storage_unavailable')
        return value
    except AssetError:
        raise
    except Exception:
        raise AssetError('external_storage_unavailable') from None


def _absolute(value: Any) -> Path:
    if not isinstance(value,(str,Path)) or not str(value).strip():
        raise AssetError('external_storage_mismatch')
    try:
        path=Path(value).expanduser()
        if not path.is_absolute():
            raise AssetError('external_storage_mismatch')
        return Path(os.path.abspath(path))
    except (OSError,ValueError,TypeError):
        raise AssetError('external_storage_mismatch') from None


def _no_symlinks(path: Path):
    try:
        if any(part.is_symlink() for part in (path,*path.parents)):
            raise AssetError('external_storage_mismatch')
    except OSError:
        raise AssetError('external_storage_unavailable') from None


def _volume(path: Path, expected_uuid: Any, *, probe, is_mount, apfs=False):
    if not isinstance(expected_uuid,str) or not expected_uuid.strip():
        raise AssetError('external_storage_mismatch')
    _no_symlinks(path)
    try:
        if not is_mount(path):
            raise AssetError('external_storage_unavailable')
        info=probe(path)
    except AssetError:
        raise
    except Exception:
        raise AssetError('external_storage_unavailable') from None
    if not isinstance(info,dict):
        raise AssetError('external_storage_unavailable')
    actual_uuid=info.get('VolumeUUID')
    if not isinstance(actual_uuid,str) or actual_uuid.casefold()!=expected_uuid.casefold():
        raise AssetError('external_storage_mismatch')
    actual_mount=info.get('MountPoint')
    if actual_mount is not None and _absolute(actual_mount)!=path:
        raise AssetError('external_storage_mismatch')
    filesystem=info.get('FilesystemType',info.get('FileSystemType'))
    if apfs and (not isinstance(filesystem,str) or filesystem.casefold()!='apfs'):
        raise AssetError('external_storage_mismatch')
    if info.get('ReadOnlyVolume') is True or info.get('ReadOnly') is True:
        raise AssetError('external_storage_unavailable')


def validate_external_storage(root: Path, storage: dict, *,
        probe: Callable[[Path],dict] | None=None,
        is_mount: Callable[[Path],bool] | None=None) -> Path:
    """Validate one configured APFS-backed directory before any local writes.

The same guard supports PDF assets and PostgreSQL bind mounts. Volume identity
comes from the private runtime configuration; path names alone cannot identify
the intended removable disk or a mounted disk image.
    """
    if not isinstance(storage,dict):
        raise AssetError('external_storage_mismatch')
    root=_absolute(root)
    mount=_absolute(storage.get('mount_point'))
    _no_symlinks(root)
    if root==mount or not root.is_relative_to(mount):
        raise AssetError('external_storage_mismatch')
    if storage.get('root') is not None and _absolute(storage['root'])!=root:
        raise AssetError('external_storage_mismatch')
    image=_absolute(storage.get('backing_image_path'))
    _no_symlinks(image)
    probe=probe or disk_info
    is_mount=is_mount or os.path.ismount
    _volume(mount,storage.get('volume_uuid'),probe=probe,is_mount=is_mount,apfs=True)
    try:
        if not image.exists():
            raise AssetError('external_storage_unavailable')
    except OSError:
        raise AssetError('external_storage_unavailable') from None
    backing=storage.get('backing_volume_mountpoint')
    if backing is not None:
        backing=_absolute(backing)
        if not image.is_relative_to(backing):
            raise AssetError('external_storage_mismatch')
        _volume(backing,storage.get('backing_volume_uuid'),probe=probe,is_mount=is_mount)
    return root


def _runtime(path: Path, *, allow_missing: bool) -> dict:
    try:
        value=json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except FileNotFoundError:
        if allow_missing:
            return {}
        raise AssetError('storage_configuration_unavailable') from None
    except (OSError,ValueError,UnicodeError):
        raise AssetError('storage_configuration_unavailable') from None
    if not isinstance(value,dict):
        raise AssetError('storage_configuration_unavailable')
    return value


def select_asset_root(explicit_root: Path | None, runtime_config: Path, *,
        environ: Mapping[str,str] | None=None, probe=None, is_mount=None) -> Path:
    """Select CLI > RA_ASSET_ROOT > private config > legacy configured default.

An explicit override can be used by isolated/local workflows without runtime
configuration. A missing configuration with no override fails rather than
forgetting the user's external-disk preference.
    """
    environment=os.environ if environ is None else environ
    override=explicit_root if explicit_root is not None else environment.get('RA_ASSET_ROOT') or None
    config=_runtime(runtime_config,allow_missing=override is not None)
    configured=config.get('asset_root')
    if configured is not None:
        configured=_absolute(configured)
    selected=override if override is not None else configured
    if selected is None:
        selected=Path.home()/'.scholars-attendant/runtime/assets'
    try:
        selected=Path(os.path.abspath(Path(selected).expanduser()))
    except (OSError,TypeError,ValueError):
        raise AssetError('external_storage_mismatch') from None
    storage=config.get('asset_storage')
    try:
        configured_selected=configured is not None and (selected==configured
            or selected.resolve()==configured.resolve())
    except OSError:
        raise AssetError('external_storage_unavailable') from None
    if configured_selected:
        if storage is not None:
            return validate_external_storage(selected,storage,probe=probe,is_mount=is_mount)
        # Local roots remain supported by old runtime files. An external path
        # requires its identity metadata rather than trusting a directory name.
        if selected.is_relative_to('/Volumes'):
            raise AssetError('external_storage_mismatch')
    return selected
