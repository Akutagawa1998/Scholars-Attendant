"""Synthetic volume identities: no disk is mounted and no real config is read."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_assistant.assets import AssetError
from research_assistant.storage_paths import select_asset_root,validate_external_storage,disk_info


@pytest.fixture
def storage(tmp_path):
    mount=tmp_path/'mounted-data';mount.mkdir()
    backing=tmp_path/'Synthetic-Disk';backing.mkdir()
    image=backing/'Scholars-Attendant/ResearchData.sparsebundle';image.mkdir(parents=True)
    root=mount/'assets'
    settings=dict(mount_point=str(mount),volume_uuid='inner-volume',backing_image_path=str(image),
        backing_volume_mountpoint=str(backing),backing_volume_uuid='outer-volume')
    info={mount:dict(VolumeUUID='INNER-VOLUME',FilesystemType='apfs',MountPoint=str(mount)),
          backing:dict(VolumeUUID='OUTER-VOLUME',FilesystemType='exfat',MountPoint=str(backing))}
    called=[]
    def probe(path):
        called.append(path)
        return info[path]
    config=tmp_path/'private-runtime.json'
    config.write_text(json.dumps(dict(asset_root=str(root),asset_storage=settings)))
    return SimpleNamespace(root=root,mount=mount,backing=backing,image=image,settings=settings,
        info=info,probe=probe,is_mount=lambda p:p in info,called=called,config=config)


def selected(storage,explicit=None,*,environ=None,is_mount=None):
    return select_asset_root(explicit,storage.config,environ={} if environ is None else environ,
        probe=storage.probe,is_mount=is_mount or storage.is_mount)


def test_external_default_is_verified_without_creating_root(storage):
    assert selected(storage)==storage.root
    assert storage.called==[storage.mount,storage.backing]
    assert not storage.root.exists()


def test_database_url_environment_does_not_skip_external_configuration(storage):
    assert selected(storage,environ={'RA_DATABASE_URL':'synthetic://fixture'})==storage.root
    assert storage.called==[storage.mount,storage.backing]


@pytest.mark.parametrize('override',['cli','environment'])
def test_same_external_path_override_cannot_bypass_mount_guard(storage,override):
    explicit=storage.root if override=='cli' else None
    environment={'RA_ASSET_ROOT':str(storage.root)} if override=='environment' else {}
    with pytest.raises(AssetError,match='external_storage_unavailable'):
        selected(storage,explicit,environ=environment,is_mount=lambda _:False)
    assert not storage.called and not storage.root.exists()


def test_explicit_local_override_precedes_environment_and_config(storage,tmp_path):
    local=tmp_path/'local-assets'
    assert selected(storage,local,environ={'RA_ASSET_ROOT':str(storage.root)},is_mount=lambda _:False)==local
    assert not storage.called and not local.exists()


def test_normalized_explicit_path_cannot_bypass_configured_mount_guard(storage):
    alias=storage.mount/'unused/../assets'
    with pytest.raises(AssetError,match='external_storage_unavailable'):
        selected(storage,alias,is_mount=lambda _:False)
    assert not storage.root.exists()


def test_symlink_alias_to_configured_root_cannot_bypass_guard(storage,tmp_path):
    alias=tmp_path/'alias-assets';alias.symlink_to(storage.root,target_is_directory=True)
    with pytest.raises(AssetError,match='external_storage_mismatch'):
        selected(storage,alias)
    assert not storage.root.exists()


def test_environment_local_override_precedes_config(storage,tmp_path):
    local=tmp_path/'local-assets'
    assert selected(storage,environ={'RA_ASSET_ROOT':str(local)},is_mount=lambda _:False)==local
    assert not storage.called and not local.exists()


@pytest.mark.parametrize('override',['cli','environment'])
def test_explicit_local_workflow_can_run_without_runtime_config(tmp_path,override):
    root=tmp_path/'local-assets';config=tmp_path/'missing.json'
    explicit=root if override=='cli' else None
    environment={'RA_ASSET_ROOT':str(root)} if override=='environment' else {}
    assert select_asset_root(explicit,config,environ=environment)==root
    assert not root.exists()


def test_missing_runtime_cannot_silently_fall_back_to_internal_storage(tmp_path):
    with pytest.raises(AssetError,match='storage_configuration_unavailable'):
        select_asset_root(None,tmp_path/'missing.json',environ={'RA_DATABASE_URL':'synthetic://fixture'})


def test_invalid_runtime_is_redacted_even_with_override(tmp_path):
    config=tmp_path/'PRIVATE.json';config.write_text('PRIVATE malformed config')
    with pytest.raises(AssetError) as error:
        select_asset_root(tmp_path/'local-assets',config,environ={})
    assert error.value.code=='storage_configuration_unavailable'
    assert 'PRIVATE' not in str(error.value)


def test_old_valid_local_runtime_retains_legacy_default(tmp_path):
    config=tmp_path/'old-runtime.json';config.write_text('{}')
    assert select_asset_root(None,config,environ={})==Path.home()/'.scholars-attendant/runtime/assets'


def test_external_path_without_volume_identity_is_rejected(tmp_path):
    config=tmp_path/'unsafe-runtime.json';config.write_text(json.dumps({'asset_root':'/Volumes/synthetic-unmounted/assets'}))
    with pytest.raises(AssetError,match='external_storage_mismatch'):
        select_asset_root(None,config,environ={})


@pytest.mark.parametrize('change',[
    {'VolumeUUID':'wrong-volume'}, {'FilesystemType':'exfat'}, {'MountPoint':'/synthetic-wrong-mount'},
])
def test_wrong_volume_or_non_posix_filesystem_never_creates_root(storage,change):
    storage.info[storage.mount].update(change)
    with pytest.raises(AssetError,match='external_storage_mismatch'):
        selected(storage)
    assert not storage.root.exists()


def test_readonly_volume_is_unavailable(storage):
    storage.info[storage.mount]['ReadOnlyVolume']=True
    with pytest.raises(AssetError,match='external_storage_unavailable'):
        selected(storage)


def test_missing_backing_image_is_unavailable(storage):
    storage.image.rmdir()
    with pytest.raises(AssetError,match='external_storage_unavailable'):
        selected(storage)


def test_missing_backing_drive_is_unavailable(storage):
    with pytest.raises(AssetError,match='external_storage_unavailable'):
        selected(storage,is_mount=lambda p:p==storage.mount)


def test_same_named_backing_drive_with_wrong_identity_is_rejected(storage):
    storage.info[storage.backing]['VolumeUUID']='wrong-external-disk'
    with pytest.raises(AssetError,match='external_storage_mismatch'):
        selected(storage)


def test_root_outside_mount_is_rejected(storage,tmp_path):
    with pytest.raises(AssetError,match='external_storage_mismatch'):
        validate_external_storage(tmp_path/'escape',storage.settings,probe=storage.probe,is_mount=storage.is_mount)


@pytest.mark.parametrize('which',['root','parent','image'])
def test_symlink_paths_are_rejected(storage,tmp_path,which):
    if which=='root':
        target=tmp_path/'other-assets';target.mkdir();storage.root.symlink_to(target,target_is_directory=True)
    elif which=='parent':
        target=tmp_path/'other-mount';target.mkdir();storage.mount.rmdir();storage.mount.symlink_to(target,target_is_directory=True)
    else:
        storage.image.rmdir();storage.image.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(AssetError,match='external_storage_mismatch'):
        selected(storage)


def test_probe_exception_cannot_leak_private_diagnostics(storage):
    def broken(_):raise RuntimeError('PRIVATE driver diagnostic')
    with pytest.raises(AssetError) as error:
        validate_external_storage(storage.root,storage.settings,probe=broken,is_mount=storage.is_mount)
    assert error.value.code=='external_storage_unavailable'
    assert 'PRIVATE' not in str(error.value)


def test_diskutil_failure_diagnostics_are_redacted(monkeypatch,storage):
    import research_assistant.storage_paths as module
    monkeypatch.setattr(module.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=b'PRIVATE',stderr=b'PRIVATE'))
    with pytest.raises(AssetError) as error:
        disk_info(storage.mount)
    assert error.value.code=='external_storage_unavailable' and 'PRIVATE' not in str(error.value)


def test_cli_checks_configured_mount_before_reading_pdf_or_opening_database(storage,monkeypatch,capsys):
    path=Path(__file__).resolve().parents[2]/'scripts/import-pdf.py'
    spec=importlib.util.spec_from_file_location('pdf_external_storage_cli',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    import research_assistant.storage_paths as paths
    monkeypatch.setattr(paths.os.path,'ismount',lambda _:False)
    monkeypatch.setenv('RA_DATABASE_URL','synthetic://fixture')
    def forbidden(*a,**k):pytest.fail('Unmounted external storage was not checked first')
    monkeypatch.setattr(module,'configured_service',forbidden)
    monkeypatch.setattr(module,'local_bytes',forbidden)
    assert module.main(['--pdf','PRIVATE.pdf','--metadata','PRIVATE.json','--runtime-config',str(storage.config)])==1
    output=capsys.readouterr()
    assert not output.out and json.loads(output.err)['error_code']=='external_storage_unavailable'
    assert 'PRIVATE' not in output.err and str(storage.root) not in output.err
    assert not storage.root.exists()
