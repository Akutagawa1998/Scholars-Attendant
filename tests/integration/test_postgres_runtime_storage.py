"""Runtime mount guards use synthetic disk and Docker state only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime_module():
    path=Path(__file__).resolve().parents[2]/'scripts/postgres-runtime.py'
    spec=importlib.util.spec_from_file_location('fixture_postgres_runtime',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external(tmp_path):
    mount=tmp_path/'apfs-volume';mount.mkdir()
    backing=tmp_path/'external-drive';backing.mkdir()
    image=backing/'fixture.sparsebundle';image.mkdir()
    root=mount/'postgres'
    storage=dict(kind='bind',data_dir=str(root),mount_point=str(mount),volume_uuid='fixture-apfs-uuid',
        backing_image_path=str(image),backing_volume_mountpoint=str(backing),backing_volume_uuid='fixture-external-uuid')
    disks={mount:dict(VolumeUUID=storage['volume_uuid'],FilesystemType='apfs',MountPoint=str(mount)),
           backing:dict(VolumeUUID=storage['backing_volume_uuid'],FilesystemType='exfat',MountPoint=str(backing))}
    return dict(config=dict(postgres_storage=storage,port=55432,image_digest='pgvector/pgvector@sha256:fixture'),
                root=root,mount=mount,backing=backing,image=image,disks=disks,
                probe=lambda path:disks[path],is_mount=lambda path:path in disks)


def test_legacy_named_volume_needs_no_external_disk(runtime_module):
    def forbidden(_):
        pytest.fail('Legacy storage should not probe an external disk')
    assert runtime_module.postgres_mount({},probe=forbidden,is_mount=forbidden)==(
        'type=volume,src=scholars-attendant-pgdata,dst=/var/lib/postgresql/data')


def test_valid_bind_mount_uses_registered_apfs_and_backing_disk(runtime_module,external):
    spec=runtime_module.postgres_mount(external['config'],probe=external['probe'],is_mount=external['is_mount'])
    assert spec=='type=bind,src='+str(external['root'])+',dst=/var/lib/postgresql/data'
    assert not external['root'].exists()  # Validation does not create a fallback path.


@pytest.mark.parametrize('returncode',[1,2,125])
def test_previously_initialized_store_cannot_be_silently_recreated(runtime_module,external,returncode):
    external['config']['postgres_storage']['initialized']=True
    mount=runtime_module.postgres_mount(external['config'],probe=external['probe'],is_mount=external['is_mount'])
    calls=[]
    def runner(*args,**kwargs):
        calls.append((args,kwargs))
        return SimpleNamespace(returncode=returncode)
    with pytest.raises(RuntimeError,match='Initialized external PostgreSQL data is missing or unavailable'):
        runtime_module.validate_initialized_data(external['config'],mount,runner=runner)
    assert len(calls)==1
    assert not external['root'].exists()


def test_initialized_probe_uses_container_owner_and_readonly_data_without_secrets(runtime_module,external):
    external['config']['postgres_storage']['initialized']=True
    mount=runtime_module.postgres_mount(external['config'],probe=external['probe'],is_mount=external['is_mount'])
    calls=[]
    def runner(*args,**kwargs):
        calls.append((args,kwargs))
        return SimpleNamespace(returncode=0)
    runtime_module.validate_initialized_data(external['config'],mount,runner=runner)
    args,kwargs=calls[0]
    assert args[:2]==('run','--rm')
    for flag,value in [('--pull','never'),('--network','none'),('--user','postgres'),('--entrypoint','sh')]:
        assert args[args.index(flag)+1]==value
    assert '--read-only' in args
    assert args[args.index('--mount')+1]==mount+',readonly'
    assert args.count('--mount')==1
    assert external['config']['image_digest'] in args
    assert 'PG_VERSION' in args[-1] and 'test ! -L' in args[-1]
    assert kwargs=={'check':False}
    assert not external['root'].exists()  # The host never reads inside PGDATA.


def test_fresh_store_does_not_require_existing_pgdata(runtime_module,external):
    def forbidden(*args,**kwargs):
        pytest.fail('Fresh data should be initialized by the regular PostgreSQL entrypoint')
    runtime_module.validate_initialized_data(external['config'],'fixture-mount',runner=forbidden)


def test_initialized_probe_rejects_unpinned_image_before_docker(runtime_module,external):
    external['config']['postgres_storage']['initialized']=True
    external['config']['image_digest']='pgvector/pgvector:latest'
    def forbidden(*args,**kwargs):
        pytest.fail('Unpinned images must not be executed')
    with pytest.raises(RuntimeError,match='requires its pinned image'):
        runtime_module.validate_initialized_data(external['config'],'fixture-mount',runner=forbidden)


@pytest.mark.parametrize('failure',['apfs-unmounted','backing-unmounted','wrong-apfs-uuid','wrong-backing-uuid',
    'wrong-filesystem','missing-image','outside-root','symlink-root','unknown-kind'])
def test_invalid_external_storage_never_becomes_named_volume(runtime_module,external,failure):
    config=external['config'];storage=config['postgres_storage']
    probe=external['probe'];is_mount=external['is_mount']
    if failure=='apfs-unmounted':is_mount=lambda path:path!=external['mount']
    elif failure=='backing-unmounted':is_mount=lambda path:path!=external['backing']
    elif failure=='wrong-apfs-uuid':external['disks'][external['mount']]['VolumeUUID']='another-apfs'
    elif failure=='wrong-backing-uuid':external['disks'][external['backing']]['VolumeUUID']='another-drive'
    elif failure=='wrong-filesystem':external['disks'][external['mount']]['FilesystemType']='exfat'
    elif failure=='missing-image':external['image'].rmdir()
    elif failure=='outside-root':storage['data_dir']=str(external['backing']/'postgres')
    elif failure=='symlink-root':
        target=external['mount']/'elsewhere';target.mkdir();external['root'].symlink_to(target,target_is_directory=True)
    elif failure=='unknown-kind':storage['kind']='unrecognized'
    with pytest.raises(RuntimeError):
        runtime_module.postgres_mount(config,probe=probe,is_mount=is_mount)
    if failure!='symlink-root':assert not external['root'].exists()


def data_mount(*,kind='bind',source='/fixture/data',volume='scholars-attendant-pgdata',rw=True):
    return dict(Type=kind,Source=source,Name=volume,Destination='/var/lib/postgresql/data',RW=rw)


def test_existing_container_matches_only_its_exact_writable_data_mount(runtime_module,external):
    config=external['config']
    mounts=[data_mount(source=str(external['root'])),dict(Type='bind',Source='/fixture/private-password',
        Destination='/run/secrets/admin-password',RW=False)]
    runtime_module.validate_container_mount(config,mounts)
    runtime_module.validate_container_mount({},[data_mount(kind='volume')])


@pytest.mark.parametrize('source',[
    '/Volumes/fixture-apfs/postgres','/host_mnt/Volumes/fixture-apfs/postgres'])
def test_exact_docker_desktop_volumes_prefix_matches_host_mount(runtime_module,source):
    config={'postgres_storage':{'kind':'bind','data_dir':'/Volumes/fixture-apfs/postgres'}}
    runtime_module.validate_container_mount(config,[data_mount(source=source)])


@pytest.mark.parametrize('source',[
    '/another/Volumes/fixture-apfs/postgres','/host_mnt/VolumesExtra/fixture-apfs/postgres',
    '/host_mnt/Users/fixture-apfs/postgres','/Volumes/fixture-apfs/other-postgres'])
def test_docker_desktop_normalization_does_not_accept_matching_suffix(runtime_module,source):
    config={'postgres_storage':{'kind':'bind','data_dir':'/Volumes/fixture-apfs/postgres'}}
    with pytest.raises(RuntimeError,match='does not match'):
        runtime_module.validate_container_mount(config,[data_mount(source=source)])


@pytest.mark.parametrize('mutation',['wrong-source','named-volume','readonly','missing','duplicate'])
def test_existing_bind_container_mismatch_stops_before_start(runtime_module,external,mutation):
    mounts=[data_mount(source=str(external['root']))]
    if mutation=='wrong-source':mounts[0]['Source']=str(external['mount']/'unrelated')
    elif mutation=='named-volume':mounts[0]['Type']='volume'
    elif mutation=='readonly':mounts[0]['RW']=False
    elif mutation=='missing':mounts=[]
    elif mutation=='duplicate':mounts=mounts+mounts
    with pytest.raises(RuntimeError,match='does not match'):
        runtime_module.validate_container_mount(external['config'],mounts)


@pytest.mark.parametrize('command',['up','restart','status'])
def test_missing_external_mount_blocks_all_operating_commands_before_mutation(runtime_module,tmp_path,monkeypatch,command):
    private=tmp_path/'runtime';private.mkdir()
    (private/'postgres.json').write_text(json.dumps({'postgres_storage':{'kind':'bind','data_dir':'/fixture/postgres'}}))
    calls=[]
    def fake_docker(*args,**kwargs):
        calls.append(args)
        assert args[:2]==('container','inspect')
        return SimpleNamespace(returncode=0,stdout=json.dumps({runtime_module.LABEL:'postgres'}))
    monkeypatch.setattr(runtime_module,'docker',fake_docker)
    with pytest.raises(RuntimeError,match='External PostgreSQL storage'):
        runtime_module.main([command,'--runtime-dir',str(private)])
    assert len(calls)==1


@pytest.mark.parametrize('command',['up','restart','status'])
def test_wrong_existing_mount_blocks_main_before_data_probe_or_start(runtime_module,tmp_path,monkeypatch,command):
    private=tmp_path/'runtime';private.mkdir()
    config={'postgres_storage':{'kind':'bind','data_dir':'/Volumes/fixture/postgres','initialized':True}}
    (private/'postgres.json').write_text(json.dumps(config))
    monkeypatch.setattr(runtime_module,'postgres_mount',lambda config:'fixture-bind')
    calls=[]
    def fake_docker(*args,**kwargs):
        calls.append(args)
        assert args[:2]==('container','inspect')
        state=({runtime_module.LABEL:'postgres'} if '.Config.Labels' in args[3]
               else [data_mount(source='/Volumes/another/postgres')])
        return SimpleNamespace(returncode=0,stdout=json.dumps(state))
    monkeypatch.setattr(runtime_module,'docker',fake_docker)
    with pytest.raises(RuntimeError,match='does not match'):
        runtime_module.main([command,'--runtime-dir',str(private)])
    assert len(calls)==2


@pytest.mark.parametrize('command',['up','restart','status'])
def test_initialized_data_probe_fails_before_operating_on_correct_container(runtime_module,tmp_path,monkeypatch,command):
    private=tmp_path/'runtime';private.mkdir()
    config={'postgres_storage':{'kind':'bind','data_dir':'/Volumes/fixture/postgres','initialized':True},
            'image_digest':'pgvector/pgvector@sha256:fixture'}
    (private/'postgres.json').write_text(json.dumps(config))
    monkeypatch.setattr(runtime_module,'postgres_mount',lambda config:'fixture-bind')
    calls=[]
    def fake_docker(*args,**kwargs):
        calls.append(args)
        if args[:2]==('container','inspect'):
            state=({runtime_module.LABEL:'postgres'} if '.Config.Labels' in args[3]
                   else [data_mount(source='/Volumes/fixture/postgres')])
            return SimpleNamespace(returncode=0,stdout=json.dumps(state))
        assert args[:2]==('run','--rm')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(runtime_module,'docker',fake_docker)
    with pytest.raises(RuntimeError,match='Initialized external PostgreSQL data is missing or unavailable'):
        runtime_module.main([command,'--runtime-dir',str(private)])
    assert len(calls)==3


def test_stop_remains_available_for_disconnected_external_storage(runtime_module,tmp_path,monkeypatch,capsys):
    private=tmp_path/'runtime';private.mkdir()
    (private/'postgres.json').write_text(json.dumps({'postgres_storage':{'kind':'bind','data_dir':'/fixture/postgres'}}))
    calls=[]
    def fake_docker(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout=json.dumps({runtime_module.LABEL:'postgres'}))
    monkeypatch.setattr(runtime_module,'docker',fake_docker)
    runtime_module.main(['stop','--runtime-dir',str(private)])
    assert calls[-1]==('stop',runtime_module.NAME)
    assert json.loads(capsys.readouterr().out)['status']=='stopped'


def test_new_bind_container_command_preserves_loopback_and_secret_file(runtime_module,external,tmp_path):
    mount=runtime_module.postgres_mount(external['config'],probe=external['probe'],is_mount=external['is_mount'])
    args=runtime_module.container_run_args(external['config'],tmp_path,mount)
    assert args[:2]==('run','-d')
    assert args[args.index('-p')+1]=='127.0.0.1:55432:5432'
    assert mount in args
    assert 'type=bind,src='+str(tmp_path/'admin-password')+',dst=/run/secrets/admin-password,readonly' in args
    assert not any('type=volume' in value for value in args)
    assert all('PASSWORD=' not in value for value in args)
