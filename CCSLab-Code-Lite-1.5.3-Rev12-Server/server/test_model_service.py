"""Lifecycle integration using real SQLite/Gateway and simulated systemd/GPU only."""
from copy import deepcopy
import json
from pathlib import Path
from contextlib import ExitStack, closing
from types import SimpleNamespace
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from . import model_control as m, model_host as h, model_service as s
from .accounts import Accounts, write_admin_code
from .gateway import Settings, create_app
from .task_slots import TaskSlots
from .test_model_control import snapshot, Parameters
from .test_support import TEST_ROOT


class FakeHost:
    def __init__(self, root):
        self.root = root
        self.running, self.pid, self.fail_start, self.busy = True, 40123, False, 0
        self.calls = []
        self.free_override = None
        self.guard = SimpleNamespace(marked=False, installed=False)
        self.profile = Parameters(163840, 2)

    def current(self):
        value = snapshot(self.profile, busy=self.busy, train=196608)
        value.update(_pid=self.pid if self.running else None, gpu=self.gpu(self.pid if self.running else None))
        if not self.running:
            value.update(status='unavailable', total_context=None, slots=None, effective_context_limit=None,
                         _active_slots=None, _observed_slot_context=None, _uniform_context=False, _config={})
        return value

    def snapshot(self): return h.settings(163840, 2)
    def unit_profile(self): return 'fixture-launch-fingerprint'
    def configuration_identity(self, gpu=None):
        return dict(version=s.SAMPLE_VERSION, fingerprint=self.unit_profile(), model_id='lab-general',
                    weights_name='Fixture-Q4_K_M.gguf', quantization='Q4_K_M', weights_identity='fixture-weight',
                    backend_identity='fixture-backend', gpu=dict(index=0,uuid='GPU-fixture',name='Fixture RTX 5090',total_mib=32607),
                    configuration=dict(total_context=163840,slots=2), _execution=dict(pid=self.pid,start_epoch=1), _latest_change=0)
    def sample_record(self):
        return json.loads(s.SAMPLE_RECORD.read_text()) if s.SAMPLE_RECORD.exists() else None
    def store_sample(self, sample):s.SAMPLE_RECORD.write_text(json.dumps(sample))
    def unit(self):
        return dict(ActiveState='active' if self.running else 'inactive', SubState='running' if self.running else 'dead',
                    MainPID=str(self.pid if self.running else 0), ExecMainStartTimestamp='2026-11-25 10:00:00 CST', Result='success')
    def gpu(self, pid=None):
        used = 29000 if self.running else 1000
        gpu = dict(index=0, name='Fixture RTX 5090', total_mib=32607, used_mib=used,
                   free_mib=self.free_override if self.free_override is not None else 32607-used-500,
                   unaccounted_mib=500, sampled_at=1000)
        if pid and self.running: gpu.update(model_process_mib=28000, _model_mib=28000)
        return gpu
    def save_state(self, state): s.STATE.write_text(json.dumps(state))
    def fence(self, previous):
        self.calls.append('fence'); self.guard.installed = self.guard.marked = True
        m.MODEL_MAINTENANCE.write_bytes(b'ccslab fixed model maintenance\n')
    def end_maintenance(self):
        self.calls.append('unfence'); self.guard.installed = self.guard.marked = False
        m.MODEL_MAINTENANCE.unlink(missing_ok=True)
    def service(self, action):
        self.calls.append(action)
        if action == 'stop': self.running = False
        else:
            self.running = not self.fail_start
            self.pid += 1
    def stopped(self, old_pid): return not self.running
    def wait_verified(self, context, slots, old_pid):
        assert (context, slots) == (163840, 2)
        return self.running


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_ROOT)))
        self.root = root
        for owner, name, value in ((s, 'STATE', root/'state.json'), (m, 'MODEL_MAINTENANCE', root/'maintenance'),
                (s, 'SAMPLE_RECORD', root/'sample.json'),
                (m, 'ROLLBACK_PENDING', root/'pending'), (m, 'DEPLOYMENT_HOLD', root/'deployment')):
            self.stack.enter_context(patch.object(owner, name, value))
        self.stack.enter_context(patch.object(m, '_root_file', side_effect=lambda p: Path(p).read_bytes()))
        self.fake = FakeHost(root)
        self.policy = m.Policy('a'*64, 0, 'http://172.17.0.1:8000', 'lab-general', root/'key')
        self.model = m.ModelControl(root/'accounts.sqlite3', self.policy.llama_url, 'fixture')
        self.model._policy = lambda: self.policy
        self.model._runtime = lambda policy=None: self.fake.current()
        self.stack.enter_context(patch.object(s, 'unit_status', side_effect=lambda _: self.fake.unit()))
        self.stack.enter_context(patch.object(m, 'CONTEXT_ENV', root/'context.env'))
        m.CONTEXT_ENV.write_bytes(self.fake.snapshot())
        self.model._invoke = lambda payload: h.run_service(self.fake, self.policy, payload)
        def inspection(policy):
            assessed=s.startup_assessment(self.fake,s.read_state(m._root_file),m.context_env(self.fake.snapshot()),policy)
            return {key:assessed[key] for key in ('estimate','sample_status','reason')}
        self.model.service_inspection=inspection
        self.addCleanup(self.stack.close)

    def action(self, operation):
        ticket = self.model.service_preview('admin', operation)['confirmation']
        return self.model.service_apply('admin', ticket)

    def test_idle_stop_then_start_verifies_context_and_fresh_gpu_without_settings_writes(self):
        before = m.CONTEXT_ENV.read_bytes()
        result = self.action('stop')['model']
        self.assertEqual(result['model_service']['state'], 'stopped')
        self.assertIsNone(result['effective_context_limit']); self.assertIsNone(result['slots'])
        self.assertIsNone(result['model_service']['pid']); self.assertIsNone(result['model_service']['model_process_mib'])
        self.assertEqual(result['context_sync']['saved_configuration'], dict(total_context=163840, slots=2))
        self.assertEqual(result['model_service']['observed_released_mib'], 28000)
        self.assertEqual(result['gpu']['used_mib'], 1000)
        self.assertTrue(m.MODEL_MAINTENANCE.exists()); self.assertFalse(self.model.begin_inference())
        result = self.action('start')['model']
        self.assertEqual(result['model_service']['state'], 'running')
        self.assertEqual(result['effective_context_limit'], 81920)
        self.assertFalse(m.MODEL_MAINTENANCE.exists()); self.assertTrue(self.model.begin_inference()); self.model.end_inference()
        self.assertEqual(m.CONTEXT_ENV.read_bytes(), before)
        self.assertEqual([c for c in self.fake.calls if c in {'stop','start'}], ['stop','start'])

    def test_active_lease_including_tools_and_approval_refuses_stop(self):
        slots = TaskSlots(self.model)
        slots.acquire('one', 7, 'a'*43, 2)
        with self.assertRaisesRegex(m.ModelControlError, '活动任务'):
            self.action('stop')
        self.assertNotIn('stop', self.fake.calls)
        slots.release('one', 7, 'a'*43)
        self.action('stop')

    def test_direct_request_busy_refuses_without_stopping(self):
        self.fake.busy = 1
        with self.assertRaises(m.ModelControlError): self.action('stop')
        self.assertNotIn('stop', self.fake.calls)
        self.assertFalse(m.MODEL_MAINTENANCE.exists())

    def test_new_direct_request_after_fence_refuses_and_restores_running_gate(self):
        fence = self.fake.fence
        def race(previous): fence(previous); self.fake.busy = 1
        self.fake.fence = race
        with self.assertRaises(m.ModelControlError): self.action('stop')
        self.assertNotIn('stop', self.fake.calls)
        self.assertFalse(m.MODEL_MAINTENANCE.exists())

    def test_new_tasks_rejected_during_atomic_gateway_transaction(self):
        ticket = self.model.service_preview('admin', 'stop')['confirmation']
        invoke = self.model._invoke
        def check(payload):
            self.assertFalse(self.model.begin_inference())
            with self.assertRaises(m.ModelControlError): self.model.apply('admin', 'x'*43)
            with self.assertRaises(m.ModelControlError): self.model.service_apply('admin', ticket)
            return invoke(payload)
        self.model._invoke = check
        self.model.service_apply('admin', ticket)

    def test_insufficient_and_unknown_gpu_refuse_start_without_killing_other_processes(self):
        self.action('stop'); self.fake.calls.clear(); self.fake.free_override = 5000
        with self.assertRaisesRegex(m.ModelControlError, '显存不足'): self.action('start')
        self.assertNotIn('start', self.fake.calls)
        self.assertTrue(m.MODEL_MAINTENANCE.exists())
        self.fake.gpu = lambda pid=None: None
        with self.assertRaisesRegex(m.ModelControlError, '无法读取'): self.action('start')

    def test_missing_or_different_profile_is_not_a_measurement(self):
        self.action('stop')
        self.fake.unit_profile = lambda: 'changed launch'
        with self.assertRaisesRegex(m.ModelControlError, '不匹配'): self.action('start')
        self.assertEqual(self.fake.calls.count('start'), 0)

    def test_failed_start_keeps_gate_and_allows_controlled_retry(self):
        self.action('stop'); self.fake.fail_start = True
        with self.assertRaisesRegex(m.ModelControlError, '启动'): self.action('start')
        self.assertEqual(self.model.status()['model_service']['state'], 'start_failed')
        self.assertFalse(self.model.begin_inference()); self.assertTrue(m.MODEL_MAINTENANCE.exists())
        self.fake.fail_start = False
        self.assertEqual(self.action('start')['model']['model_service']['state'], 'running')

    def test_restart_rechecks_gpu_and_does_not_repeat_on_confirmation_retry(self):
        ticket = self.model.service_preview('admin', 'restart')['confirmation']
        self.model.service_apply('admin', ticket)
        with self.assertRaises(m.ModelControlError): self.model.service_apply('admin', ticket)
        self.assertEqual([c for c in self.fake.calls if c in {'stop','start'}], ['stop','start'])

    def test_gateway_recreation_preserves_stop_and_blocks_configuration_resync(self):
        self.action('stop')
        fresh = m.ModelControl(self.model.db, self.policy.llama_url, 'fixture')
        self.assertFalse(fresh.begin_inference())
        with self.assertRaises(m.ModelControlError): fresh.require_online()
        self.model._invoke = lambda payload: self.fail('must not call helper for stopped configuration')
        with self.assertRaisesRegex(m.ModelControlError, '先恢复服务'):
            self.model.preview('admin', 163840, 2)
        self.assertEqual(self.fake.calls.count('start'), 0)

    def test_unknown_state_and_foreign_confirmation_fail_closed(self):
        s.STATE.write_text('{broken')
        self.assertFalse(self.model.begin_inference())
        self.assertEqual(self.model.status()['model_service']['state'], 'unknown')
        with self.assertRaises(m.ModelControlError): self.model.service_preview('admin', 'start')
        s.STATE.unlink()
        ticket = self.model.service_preview('admin', 'stop')['confirmation']
        with self.assertRaises(m.ModelControlError): self.model.service_apply('another-admin', ticket)
        with self.assertRaises(m.ModelControlError): self.model.apply('admin', ticket)

    def test_helper_accepts_only_fixed_verbs_and_no_paths_services_or_commands(self):
        request = dict(operation='stop', policy=self.policy.digest, expected_generation=0, operation_id='b'*64)
        self.assertEqual(h.parse_request(json.dumps(request))['operation'], 'stop')
        for change in (dict(operation='kill'), dict(service='other.service'), dict(command='whoami'), dict(expected_generation=True)):
            with self.assertRaises(ValueError): h.parse_request(json.dumps(dict(request, **change)))

    def test_real_gateway_auth_accounts_shared_slots_pause_error_and_no_charge(self):
        verifier = self.root/'administrator.verifier'; write_admin_code(verifier, '310742')
        accounts = Accounts(self.model.db, admin_code_file=verifier); accounts.migrate()
        accounts.create('administrator-test', 'fixture-admin-password', bootstrap=True)
        ordinary = accounts.create('ordinary-test', 'fixture-user-password', bootstrap=False)
        calls = []
        app = create_app(Settings(self.model.db, 'http://llama.fixture', 'lab-general', 'fixture', admin_registration_code_file=verifier),
                         model_control=self.model, allow_loopback_http=True,
                         upstream_transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(500)))
        with TestClient(app, base_url='https://gateway.fixture') as client:
            admin = client.post('/api/login', json=dict(username='administrator-test',password='fixture-admin-password',
                               role='admin',authorization_code='310742')).json()['token']
            client.post('/api/admin/users/'+str(ordinary['id'])+'/status',headers={'Authorization':'Bearer '+admin},json={'status':'active'})
            user = client.post('/api/login', json=dict(username='ordinary-test',password='fixture-user-password')).json()['token']
            ah, uh = {'Authorization':'Bearer '+admin}, {'Authorization':'Bearer '+user}
            endpoint = '/api/admin/model/service/'
            for operation in ('start', 'stop', 'restart'):
                self.assertEqual(client.post(endpoint+'preview',headers=uh,json={'operation':operation}).status_code,403)
            self.assertEqual(client.post(endpoint+'apply',headers=uh,json={'confirmation':'x'}).status_code,403)
            ticket = client.post(endpoint+'preview',headers=ah,json={'operation':'stop'}).json()['confirmation']
            self.assertEqual(client.post(endpoint+'apply',headers=ah,json={'confirmation':ticket}).status_code,200)
            self.assertEqual(client.get('/health').status_code,200)
            before = client.get('/api/me',headers=uh).json()['user']['quota']
            result = client.post('/v1/chat/completions',headers=uh,json={'model':'lab-general','messages':[]})
            self.assertEqual(result.status_code,503); self.assertEqual(result.json()['error']['code'],'model_paused')
            self.assertIn('detail',result.json()); self.assertEqual(calls,[])
            self.assertEqual(client.get('/api/me',headers=uh).json()['user']['quota'],before)
            self.assertEqual(client.get('/api/tasks',headers=uh).json()['available_slots'],0)
            self.assertEqual(client.post('/api/tasks/'+'a'*32+'/acquire',headers=uh).status_code,503)
            self.assertEqual(client.get('/api/admin/users',headers=ah).status_code,200)
        with closing(sqlite3.connect(self.model.db)) as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM model_service_audit').fetchone()[0],0)

    def test_restart_free_memory_race_after_stop_retains_pause(self):
        stop = self.fake.service
        def change(action):
            stop(action)
            if action == 'stop': self.fake.free_override = 2000
        self.fake.service = change
        with self.assertRaisesRegex(m.ModelControlError, '显存不足'): self.action('restart')
        self.assertNotIn('start', self.fake.calls)
        self.assertTrue(m.MODEL_MAINTENANCE.exists()); self.assertFalse(self.model.begin_inference())

    def test_systemd_commands_are_fixed_and_refresh_is_read_only(self):
        host = h.ServiceHost(self.policy, 'fixture')
        with patch.object(m, '_fixed_command', return_value='') as command:
            host.service('stop'); host.service('start')
            self.assertEqual([c.args[0] for c in command.call_args_list],
                             [[m.SYSTEMCTL,'stop','--',m.MODEL_UNIT],[m.SYSTEMCTL,'start','--',m.MODEL_UNIT]])
            with self.assertRaises(ValueError): host.service('other.service')
        self.model.status(); self.model.status()
        self.assertEqual(self.fake.calls, [])

    def test_gateway_restart_during_helper_preflight_cannot_admit_new_tasks(self):
        current = self.fake.current
        def after_intent():
            state = s.read_state(m._root_file)
            if state and state['phase'] == 'stopping':
                fresh = m.ModelControl(self.model.db, self.policy.llama_url, 'fixture')
                self.assertFalse(fresh.begin_inference())
            return current()
        self.fake.current = after_intent
        self.action('stop')

    def test_readonly_reconnects_only_root_recorded_fixed_port_fence(self):
        m.MODEL_MAINTENANCE.write_bytes(b'ccslab fixed model maintenance\n')
        chains = [{'chain':dict(name=c,hook=c,table=h.NFT_TABLE,prio=-150,policy='accept')} for c in ('input','output')]
        rule = {'rule':dict(table=h.NFT_TABLE,expr=[{'match':dict(op='==',left={'payload':dict(protocol='tcp',field='dport')},right=8000)},
                                                   {'reject':dict(type='tcp reset')}])}
        data = {'nftables':chains+[deepcopy(rule),deepcopy(rule)]}
        host = h.ServiceHost(self.policy,'fixture')
        with patch.object(m,'_fixed_command',return_value=json.dumps(data)) as command:
            host.fence({'guard':True})
            self.assertTrue(host.guard.installed)
            self.assertEqual(command.call_args.args[0],[h.NFT,'-j','list','table','inet',h.NFT_TABLE])
        data['nftables'][-1]['rule']['expr'][0]['match']['right']=18000
        with patch.object(m,'_fixed_command',return_value=json.dumps(data)):
            with self.assertRaises(h.MaintenanceError):host.fence({'guard':True})


if __name__ == '__main__': unittest.main()
