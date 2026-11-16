"""Atomicity and simultaneous writers, rather than sequential mocks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from research_assistant import storage as db
from research_assistant.service import ResearchService, DomainError

def service_at(url):
    service = ResearchService(url)
    service.initialize()
    return service

def capture(service, value):
    return service.execute('capture.message', {
        'message_id': str(uuid4()), 'session_id': 'synthetic-input',
        'text': 'Synthetic priority update: ' + value,
        'claims': [{'subject_id': 'fixture:profile', 'predicate': 'priority',
                    'value': {'priority': value}, 'quote': value,
                    'valid_from': '2026-01-01T00:00:00Z'}],
        'idempotency_key': str(uuid4()),
    }, trusted_capture=True)['data']['trusted_message_id']

def test_simultaneous_writers_get_one_winner_and_a_visible_conflict(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'concurrency.sqlite3')
    first, second = service_at(url), service_at(url)
    original = first.execute('memory.record_user_statement', {
        'trusted_message_id': capture(first, 'A'), 'idempotency_key': 'initial'
    })['data']
    messages = [capture(first, 'B'), capture(second, 'C')]
    barrier = Barrier(2)
    def writer(index):
        barrier.wait()
        try:
            return [first, second][index].execute('memory.record_user_statement', {
                'trusted_message_id': messages[index], 'memory_id': original['memory_id'],
                'expected_revision': 1, 'idempotency_key': 'concurrent-' + str(index),
            })['status']
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    assert sorted(results) == ['ok', 'revision_conflict']
    current = first.execute('memory.recall', {})['data']['reported_facts']
    assert len(current) == 1 and current[0]['revision'] == 2

def test_failed_state_change_rolls_back_prior_interval_and_outbox(tmp_path):
    service = service_at('sqlite:///' + str(tmp_path / 'atomicity.sqlite3'))
    original = service.execute('memory.record_user_statement', {
        'trusted_message_id': capture(service, 'A'), 'idempotency_key': 'initial'
    })['data']
    message = capture(service, 'B')  # same valid_from as original: invalid state transition
    def counts():
        with service.storage.transaction() as c:
            return [c.scalar(select(func.count()).select_from(t)) for t in (db.assertions, db.events, db.jobs)]
    before = counts()
    with pytest.raises(DomainError, match='state change'):
        service.execute('memory.record_user_statement', {
            'trusted_message_id': message, 'memory_id': original['memory_id'],
            'expected_revision': 1, 'change_kind': 'state_change', 'idempotency_key': 'failed-change',
        })
    assert counts() == before
    facts = service.execute('memory.recall', {})['data']['reported_facts']
    assert facts[0]['assertion_id'] == original['assertion_id']
    assert facts[0]['system_to'] is None
