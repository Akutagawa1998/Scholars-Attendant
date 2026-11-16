"""Acceptance verifier detects missing/tampered anchors on isolated stores."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select,update,delete

from research_assistant import storage as db
from test_zotero_assets import fixture,ingest,metadata,ANNOTATION,ATTACHMENT
from test_postgres import pg_url


@pytest.fixture
def verifier():
    spec=importlib.util.spec_from_file_location('synthetic_verify_pdf',Path(__file__).resolve().parents[2]/'src/research_assistant/operators/verify_zotero_pdfs.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def test_verifies_all_current_pages_and_annotations(fixture,verifier):
    s,store,*_=fixture
    ingest(fixture)
    r=verifier.verify(s,store,expected_pdfs=1)
    assert r['counts']['pdfs_verified']==1 and r['counts']['literal_page_anchors_verified']==2
    assert r['counts']['annotation_page_indices_verified']==1
    assert r['alignment_counts']=={'exact':1}
    assert r['baseline_comparison']['reading_sessions']==0 and r['baseline_comparison']['work_versions']==0


@pytest.mark.parametrize('tamper',['negative_page','negative_start','quote_hash','parser_version','kind'])
def test_rejects_locators_that_python_slices_might_accept(fixture,verifier,tamper):
    s,store,*_=fixture;receipt=ingest(fixture)
    with s.storage.transaction() as c:
        row=c.execute(select(db.evidence).where(db.evidence.c.source_version_id==receipt['source_snapshot_id'])).mappings().first()
        loc=dict(row['locator'])
        if tamper=='negative_page':loc['page_index']=-2
        elif tamper=='negative_start':loc['text_start']=-len(row['text'])
        elif tamper=='quote_hash':loc['quote_hash']='0'*64
        elif tamper=='parser_version':loc['extraction_version']='fabricated'
        else:loc['kind']='json'
        c.execute(update(db.evidence).where(db.evidence.c.id==row['id']).values(locator=loc))
    with pytest.raises(ValueError):verifier.verify(s,store,expected_pdfs=1)


@pytest.mark.parametrize('missing',['page_evidence','annotation_alignment'])
def test_absent_rows_cannot_disappear_from_acceptance_counts(fixture,verifier,missing):
    s,store,*_=fixture;receipt=ingest(fixture)
    table=db.evidence if missing=='page_evidence' else db.annotation_alignments
    col=table.c.source_version_id if missing=='page_evidence' else table.c.pdf_source_id
    with s.storage.transaction() as c:
        row=c.execute(select(table.c.id).where(col==receipt['source_snapshot_id'])).first()
        c.execute(delete(table).where(table.c.id==row[0]))
    with pytest.raises(ValueError):verifier.verify(s,store,expected_pdfs=1)


def test_requires_expected_visible_pdf_count(fixture,verifier):
    s,store,*_=fixture;ingest(fixture)
    with pytest.raises(ValueError):verifier.verify(s,store,expected_pdfs=2)


def test_normal_annotation_revision_checks_current_alignment(fixture,verifier):
    s,store,*_=fixture;ingest(fixture)
    metadata(s,ANNOTATION,'annotation',version=23,parentItem=ATTACHMENT,
        annotationType='highlight',annotationText='Synthetic selected quote.',annotationPosition='{"pageIndex":0}')
    ingest(fixture)
    r=verifier.verify(s,store,expected_pdfs=1)
    assert r['counts']['annotation_records_verified']==1
    assert r['counts']['historical_or_inaccessible_annotation_alignments_skipped']==1
