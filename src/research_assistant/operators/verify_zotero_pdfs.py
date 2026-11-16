"""Verify acquired original bytes, literal anchors and annotation page mappings.

No provider network, LLM, remote write or reading mutation. Optional page PNGs
are temporary visual QA artifacts on the registered disk; delete them after operator review.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from sqlalchemy import select,func

from research_assistant import storage as db
from research_assistant.annotation_alignment import align_annotations
from research_assistant.asset_service import read_source_asset,_content
from research_assistant.assets import ArtifactStore,extract_pdf
from research_assistant.service import ResearchService,canonical
from research_assistant.storage_paths import select_asset_root,validate_external_storage
from research_assistant.zotero_assets import _current_metadata,_private_report,_annotations


def require(value):
    if not value:raise ValueError('pdf_acceptance_mismatch')


def verify(service,store,*,asset_root=None,render_directory=None,expected_pdfs=None):
    counts=Counter();statuses=Counter();methods=Counter();visual=[]
    with service.storage.transaction() as c:
        pdfs=list(c.execute(select(db.sources,db.source_assets.c.sha256.label('original_sha256'),
            db.source_assets.c.byte_size,db.source_assets.c.blob_key).join(db.source_assets,
            db.source_assets.c.source_version_id==db.sources.c.id).where(service._scope(db.sources),
            service._scope(db.source_assets),db.sources.c.kind=='zotero_attachment_pdf',
            db.source_assets.c.state=='active')).mappings())
    require(pdfs)
    for source in pdfs:
        with service.storage.transaction() as c:
            if not service._linked_asset_available(c,source['id']):
                counts['inaccessible_pdf_contexts_skipped']+=1
                continue
        data=read_source_asset(service,store,source['id'])
        require(hashlib.sha256(data).hexdigest()==source['original_sha256'] and len(data)==source['byte_size'])
        extraction=extract_pdf(data)
        require(source['content']==_content(extraction) and hashlib.sha256(source['content'].encode()).hexdigest()==source['sha256'])
        require(source['extraction_version']==extraction['parser_name']+':'+extraction['parser_version'])
        selected=None
        with service.storage.transaction() as c:
            link=c.execute(select(db.attachment_asset_links).where(service._scope(db.attachment_asset_links),
                db.attachment_asset_links.c.pdf_source_id==source['id'])).mappings().one()
            methods[link['provenance'].get('acquisition_method','zotero_api_file')]+=1
            require(hashlib.md5(data,usedforsecurity=False).hexdigest()==link['provenance']['attachment_md5'])
            _,_,_,registration,_,_=_current_metadata(service,c,link['metadata_snapshot_id'])
            current_annotations=dict(_annotations(service,c,registration['id'],link['provenance']['attachment_key']))
            alignment_rows=list(c.execute(select(db.annotation_alignments).where(service._scope(db.annotation_alignments),
                    db.annotation_alignments.c.pdf_source_id==source['id'])).mappings())
            require({row['annotation_snapshot_id'] for row in alignment_rows if row['annotation_snapshot_id'] in current_annotations}
                    ==set(current_annotations))
            anchored_pages=set()
            for span in c.execute(select(db.evidence).where(service._scope(db.evidence),
                    db.evidence.c.source_version_id==source['id'])).mappings():
                loc=span['locator'];index=loc.get('page_index')
                require(type(index) is int and 0<=index<len(extraction['pages']))
                page=extraction['pages'][index]
                start,end=loc.get('text_start'),loc.get('text_end')
                require(type(start) is int and type(end) is int and 0<=start<end<=len(page['text']))
                require(loc.get('kind')=='pdf' and loc.get('quote_hash')==span['quote_hash']
                    and loc.get('extraction_version')==source['extraction_version'])
                require(loc['source_sha256']==source['original_sha256'] and loc['page_text_sha256']==page['text_sha256'])
                require(page['text'][loc['text_start']:loc['text_end']]==span['text'])
                require(hashlib.sha256(span['text'].encode()).hexdigest()==span['quote_hash'])
                require(start==0 and end==len(page['text']))
                anchored_pages.add(index)
                require(service._evidence_available(c,span['id']))
                counts['literal_page_anchors_verified']+=1
            require(anchored_pages=={page['page_index'] for page in extraction['pages'] if page['text']})
            current_rows=[row for row in alignment_rows if row['annotation_snapshot_id'] in current_annotations]
            if len(current_rows)!=len(alignment_rows):
                counts['historical_or_inaccessible_annotation_alignments_skipped']+=len(alignment_rows)-len(current_rows)
            reports=align_annotations(extraction,[current_annotations[row['annotation_snapshot_id']] for row in current_rows],
                expected_pdf_sha256=source['original_sha256'])
            for row,report in zip(current_rows,reports,strict=True):
                annotation=current_annotations[row['annotation_snapshot_id']]
                require(annotation.get('parentItem')==link['provenance']['attachment_key'])
                require(canonical(row['report'])==canonical(_private_report(report)))
                statuses[report['match_status']]+=1;counts['annotation_records_verified']+=1
                if report['page_index'] is not None:
                    position=annotation.get('annotationPosition')
                    position=json.loads(position) if isinstance(position,str) else position
                    require(position['pageIndex']==report['page_index'] and report['page_number']==report['page_index']+1)
                    counts['annotation_page_indices_verified']+=1
                for match in report['matches']:
                    page=extraction['pages'][report['page_index']]
                    literal=page['text'][match['text_start']:match['text_end']]
                    require(literal==match['literal_text'] and hashlib.sha256(literal.encode()).hexdigest()==match['literal_sha256'])
                    counts['literal_annotation_candidates_verified']+=1
                if report['match_status'] in {'exact','normalized'} and (selected is None or report['page_index']>selected['page_index']):
                    selected={'source_snapshot_id':source['id'],'page_index':report['page_index'],
                        'page_number':report['page_number'],'match_status':report['match_status']}
        counts['pdfs_verified']+=1;counts['bytes_verified']+=len(data);counts['pdf_pages_verified']+=extraction['page_count']
        if render_directory is not None and selected is not None:
            require(render_directory.is_absolute() and asset_root is not None)
            render_directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            prefix=render_directory/(source['original_sha256'][:12]+'-page-'+str(selected['page_number']))
            binary=shutil.which('pdftoppm');require(binary)
            rendered=subprocess.run([binary,'-f',str(selected['page_number']),'-l',str(selected['page_number']),
                '-scale-to','1400','-singlefile','-png',str(asset_root/source['blob_key']),str(prefix)],capture_output=True,timeout=30)
            require(rendered.returncode==0 and prefix.with_suffix('.png').is_file())
            visual.append(dict(selected,image_path=str(prefix.with_suffix('.png'))))
    require(counts['pdfs_verified']>0)
    if expected_pdfs is not None:require(counts['pdfs_verified']==expected_pdfs)
    with service.storage.transaction() as c:
        objects=[dict(row) for row in c.execute(select(db.source_objects).where(service._scope(db.source_objects))).mappings()]
        cursors=[dict(row) for row in c.execute(select(db.connector_cursors).where(service._scope(db.connector_cursors))).mappings()]
        baseline=dict(source_heads_sha256=hashlib.sha256(canonical(sorted(objects,key=lambda x:x['id'])).encode()).hexdigest(),
            cursors_sha256=hashlib.sha256(canonical(sorted(cursors,key=lambda x:x['id'])).encode()).hexdigest(),
            reading_sessions=c.execute(select(func.count()).select_from(db.reading_sessions).where(service._scope(db.reading_sessions))).scalar_one(),
            work_versions=c.execute(select(func.count()).select_from(db.versions).where(service._scope(db.versions))).scalar_one())
    return dict(status='ok',counts=dict(counts),alignment_counts=dict(statuses),acquisition_methods=dict(methods),
                baseline_comparison=baseline,visual_samples=visual,llm_calls=0,remote_write=False)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime-config',type=Path,default=Path.home()/'.scholars-attendant/runtime/postgres.json')
    p.add_argument('--render-samples',type=Path)
    p.add_argument('--baseline',type=Path)
    p.add_argument('--report',type=Path)
    p.add_argument('--expected-pdfs',type=int,help='Require this many current visible PDF contexts')
    args=p.parse_args(argv);service=None
    try:
        root=select_asset_root(None,args.runtime_config,environ={})
        runtime=json.loads(args.runtime_config.read_text())
        validate_external_storage(root,runtime['asset_storage'])
        # QA artifacts and reports must stay on the same registered physical disk.
        backing_root=Path(runtime['asset_storage']['backing_image_path']).parent
        for target in (args.render_samples,args.report):
            if target is not None:
                require(target.is_absolute() and target.resolve().is_relative_to(backing_root.resolve()))
        service=ResearchService(runtime['database_url'],runtime.get('owner_id','local'),runtime.get('scope_id','private'))
        with ArtifactStore(root) as store:result=verify(service,store,asset_root=root,
            render_directory=args.render_samples,expected_pdfs=args.expected_pdfs)
        if args.baseline:
            baseline=json.loads(args.baseline.read_text())
            require(result['baseline_comparison']==baseline)
            result['metadata_checkpoint_and_reading_unchanged']=True
        if args.report:args.report.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result));return 0
    except Exception as error:
        print(json.dumps({'status':'failed','error_code':'pdf_acceptance_unavailable','error_type':type(error).__name__}),file=sys.stderr);return 1
    finally:
        if service is not None:service.storage.engine.dispose()


if __name__=='__main__':
    raise SystemExit(main())
