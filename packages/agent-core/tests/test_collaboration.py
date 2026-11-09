import tempfile
import threading
import time
import unittest
from pathlib import Path
from agent_core.collaboration.service import CollaborationService, CollaborationError


def task(key, deps=None, resources=None):
    return dict(id=key, goal=key, dependsOn=deps or [], resources=resources or [],
                outputs=[key + '.txt'], acceptance=['文件包含任务结果'], timeout=10)


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.tmp.cleanup()

    def service(self, runner):
        service = CollaborationService(Path(self.tmp.name), runner)
        self.services.append(service)
        return service

    def finish(self, service, key):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.get(key)
            if result['state'] != 'running':
                return result
            time.sleep(.01)
        self.fail('scheduler did not finish')

    def test_parallel_dependencies_and_durable_artifacts(self):
        barrier = threading.Barrier(2)
        def runner(project, current, directory, deps, stop):
            if current['id'] in ('a', 'b'):
                barrier.wait(timeout=2)
            else:
                self.assertEqual(len(deps), 2)
                self.assertTrue((directory / 'inputs/a/a.txt').is_file())
            (directory / (current['id'] + '.txt')).write_text(current['id'])
            return dict(accepted=True, summary='done')
        service = self.service(runner)
        project = service.create('project', [task('a'), task('b'), task('c', ['a', 'b'])], 'r1')
        service.start(project['id'])
        result = self.finish(service, project['id'])
        self.assertEqual(result['state'], 'succeeded', str(result))
        self.assertEqual(service.create('project', [task('a')], 'r1')['id'], project['id'])
        self.assertEqual(len(result['tasks'][2]['artifacts'][0]['sha256']), 64)

    def test_exclusive_resources_and_independent_retry(self):
        active = 0
        lock = threading.Lock()
        def runner(project, current, directory, deps, stop):
            nonlocal active
            with lock:
                active += 1
                self.assertEqual(active, 1)
            time.sleep(.03)
            with lock:
                active -= 1
            if current['id'] == 'b' and current['attempt'] == 1:
                raise ValueError('retry')
            (directory / (current['id'] + '.txt')).write_text('ok')
            return dict(accepted=True)
        service = self.service(runner)
        project = service.create('p', [task('a', resources=['app']), task('b', resources=['app'])], 'r')
        service.start(project['id'])
        self.assertEqual(self.finish(service, project['id'])['state'], 'failed')
        service.start(project['id'])
        result = self.finish(service, project['id'])
        self.assertEqual(result['state'], 'succeeded', str(result))
        self.assertEqual([t['attempt'] for t in result['tasks']], [1, 2])

    def test_reject_cycle_and_escape(self):
        service = self.service(None)
        with self.assertRaises(CollaborationError):
            service.create('p', [task('a', ['b']), task('b', ['a'])], 'r')
        bad = task('a'); bad['outputs'] = ['../escape']
        with self.assertRaises(CollaborationError):
            service.create('p', [bad], 'r')

    def test_failed_validation_blocks_dependents(self):
        service = self.service(lambda *args: dict(accepted=False, reason='missing evidence'))
        project = service.create('p', [task('a'), task('b', ['a'])], 'r')
        service.start(project['id'])
        result = self.finish(service, project['id'])
        self.assertEqual([t['state'] for t in result['tasks']], ['failed', 'blocked'])

    def test_new_requirement_requires_revision(self):
        service = self.service(None)
        project = service.create('p', [task('a')], 'r')
        service.message(project['id'], 'new requirement')
        with self.assertRaises(CollaborationError):
            service.start(project['id'])
        revised = service.revise(project['id'], [task('a')], 1)
        self.assertEqual(revised['revision'], 2)
        self.assertFalse(revised['requiresRevision'])

    def test_reopen_marks_abandoned_running_as_interrupted(self):
        service = self.service(None)
        project = service.create('p', [task('a')], 'r')
        project['state'] = 'running'; project['tasks'][0]['state'] = 'running'
        service._save(project)
        reopened = self.service(None)
        self.assertEqual(reopened.get(project['id'])['state'], 'interrupted')

    def test_delivery_requires_approval_and_preserves_source(self):
        def runner(project, current, directory, deps, stop):
            (directory / 'a.txt').write_text('approved artifact')
            return dict(accepted=True)
        service = self.service(runner)
        project = service.create('p', [task('a')], 'r')
        service.start(project['id']); self.finish(service, project['id'])
        with self.assertRaises(CollaborationError):
            service.deliver(project['id'], lambda *args: False, lambda path: None)
        result = service.deliver(project['id'], lambda *args: True, lambda path: None)
        self.assertEqual((Path(self.tmp.name) / result['path'] / 'a/a.txt').read_text(), 'approved artifact')
        with self.assertRaises(CollaborationError):
            service.deliver(project['id'], lambda *args: True, lambda path: None)

    def test_tampered_artifact_cannot_be_delivered(self):
        def runner(project, current, directory, deps, stop):
            (directory / 'a.txt').write_text('ok')
            return dict(accepted=True)
        service = self.service(runner)
        project = service.create('p', [task('a')], 'r')
        service.start(project['id']); result = self.finish(service, project['id'])
        (service.root / result['tasks'][0]['artifacts'][0]['path']).write_text('changed')
        with self.assertRaises(CollaborationError):
            service.deliver(project['id'], lambda *args: True, lambda path: None)

    def test_agent_writes_and_independent_verifier_reads_artifact(self):
        from agent_core.config import Config
        from agent_core.loop.agent import Agent
        from agent_core.llm import ChatResponse, ToolCall
        from agent_core.collaboration.agent import AgentRunner
        class Client:
            supports_native_tools = True
            def __init__(self): self.calls = 0
            def chat(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ChatResponse(tool_calls=[ToolCall('w', 'write_file', {'path':'a.txt','content':'verified output'})])
                if self.calls == 2: return ChatResponse(content='created')
                if self.calls == 3:
                    return ChatResponse(tool_calls=[ToolCall('r', 'read_file', {'path':'a.txt'})])
                return ChatResponse(content='{"accepted":true,"reason":"read actual output","checks":["file checked"]}')
        parent = Agent(Config.load(Path(self.tmp.name), memory_enabled=False, audit_enabled=False), client=Client())
        approvals = []
        def approve(request):
            approvals.append(request.tool)
            return 'approve'
        service = self.service(AgentRunner(parent, approve))
        project = service.create('p', [task('a')], 'r')
        service.start(project['id'])
        result = self.finish(service, project['id'])
        self.assertEqual(result['state'], 'succeeded', str(result))
        self.assertEqual(approvals, ['write_file'])
        self.assertFalse((Path(self.tmp.name) / 'a.txt').exists())
