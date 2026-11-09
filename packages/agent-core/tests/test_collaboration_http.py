"""Real host API coverage for collaboration gating and lifecycle."""
import json
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from agent_core.config import Config
from agent_core.host.server import HostServer
from agent_core.host.store import SessionStore
from agent_core.llm import ChatResponse


class Client:
    supports_native_tools = True
    def chat(self, *args, **kwargs):
        return ChatResponse(content='{"accepted":true,"reason":"reviewed","checks":["ok"]}')


class CollaborationHttpTests(unittest.TestCase):
    def test_host_gate_plan_execution_and_source_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'; workspace.mkdir()
            store = SessionStore(root / 'sessions.db')
            host = HostServer(Config.load(workspace, collaboration_enabled=True, index_watch=False, memory_enabled=False), port=0, store=store, client=Client())
            host.start_background()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request(method, path, body=None):
                req = urllib.request.Request(f'http://127.0.0.1:{host.port}/api/collaborations' + path,
                    data=None if body is None else json.dumps(body).encode(), method=method,
                    headers={'X-Vibe-Token':host.guard.token,'Content-Type':'application/json'})
                with opener.open(req, timeout=5) as response:
                    return json.loads(response.read())
            try:
                task = {'id':'analysis', 'goal':'分析测试任务', 'outputs':[], 'acceptance':['给出分析结果']}
                project = request('POST', '', {'goal':'测试协作', 'tasks':[task], 'requestId':'r'})
                self.assertEqual(project['state'], 'draft')
                request('POST', '/' + project['id'] + '/start', {})
                for _ in range(100):
                    result = request('GET', '/' + project['id'])
                    if result['state'] != 'running': break
                    time.sleep(.02)
                self.assertEqual(result['state'], 'succeeded')
                self.assertFalse((workspace / 'analysis.txt').exists())
                self.assertTrue(request('GET', '')['projects'])
                self.assertTrue(any(e['type'] == 'collaboration.state' for e in host.ctx.session.bus.snapshot()))
            finally:
                host.shutdown()

    def test_disabled_feature_rejects_api_without_creating_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = HostServer(Config.load(root, collaboration_enabled=False, index_watch=False, memory_enabled=False), port=0, client=Client())
            host.start_background()
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{host.port}/api/collaborations',
                    headers={'X-Vibe-Token':host.guard.token})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(request, timeout=5)
                self.assertEqual(error.exception.code, 400)
                self.assertFalse((root / '.vibe/collaborations').exists())
            finally:
                host.shutdown()

    def test_pending_approval_does_not_block_project_queries(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = HostServer(Config.load(root, collaboration_enabled=True, index_watch=False, memory_enabled=False), port=0, client=Client())
            entered, release, queried = threading.Event(), threading.Event(), threading.Event()
            try:
                session = host.ctx.session
                def approve(request):
                    entered.set()
                    release.wait(3)
                    return 'deny'
                session._approve = approve
                service = session.collaboration_service()
                worker = threading.Thread(target=lambda: service.runner.approver(None))
                worker.start()
                self.assertTrue(entered.wait(1))
                query = threading.Thread(target=lambda: (session.collaboration_service().list(), queried.set()))
                query.start()
                self.assertTrue(queried.wait(1), 'query blocked behind pending approval')
            finally:
                release.set()
                worker.join(3)
                query.join(3)
                session.close()
                host._server.server_close()
