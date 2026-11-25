"""Regression for ExecStart bookkeeping versus stable model memory identity."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from . import model_service as s, model_control as c, model_host as h
from . import test_model_service as fixtures
from .test_support import TEST_ROOT


def display(executable='/bin/llama-server', weights='/models/Fixture-Q4_K_M.gguf', stopped=False):
    return ('{ path='+executable+' ; argv[]='+executable+' -m '+weights+' -ngl 99 ; ignore_errors=no ; '
            'start_time=[Wed 2026-11-25 10:00:00 CST] ; stop_time=['+
            ('Wed 2026-11-25 11:00:00 CST' if stopped else 'n/a')+'] ; pid=40123 ; code='+
            ('exited' if stopped else '(null)')+' ; status='+('0' if stopped else '0/0')+' }')


class SampleLifecycleTests(unittest.TestCase):
    setUp = fixtures.LifecycleTests.setUp
    action = fixtures.LifecycleTests.action
    def test_long_stopped_and_gateway_recreated_uses_same_persistent_sample(self):
        self.action('stop')
        value=json.loads(s.STATE.read_text());value['sample']['sampled_at']=1
        s.STATE.write_text(json.dumps(value));self.fake.store_sample(value['sample'])
        fresh=c.ModelControl(self.model.db,self.policy.llama_url,'fixture')
        fresh._policy=self.model._policy;fresh._runtime=self.model._runtime
        fresh.service_inspection=self.model.service_inspection
        fresh._invoke=self.model._invoke
        self.assertEqual(fresh.status()['model_service']['startup_estimate']['sampled_at'],1)
        ticket=fresh.service_preview('admin','start')['confirmation']
        self.assertEqual(fresh.service_apply('admin',ticket)['model']['model_service']['state'],'running')

    def test_missing_state_sample_recovers_only_trusted_matching_record(self):
        self.action('stop');value=json.loads(s.STATE.read_text());del value['sample']
        s.STATE.write_text(json.dumps(value));before=s.STATE.read_bytes()
        shown=self.model.status()['model_service']
        self.assertEqual(shown['startup_estimate']['source'],'root_sample_record')
        self.assertEqual(s.STATE.read_bytes(),before)  # Refresh never rewrites a sample.
        self.assertEqual(self.action('start')['model']['model_service']['state'],'running')
        self.assertEqual(json.loads(s.STATE.read_text())['sample']['profile_version'],s.SAMPLE_VERSION)

    def test_all_records_missing_refuses_instead_of_using_manual_mib(self):
        self.action('stop');value=json.loads(s.STATE.read_text());del value['sample']
        s.STATE.write_text(json.dumps(value));s.SAMPLE_RECORD.unlink()
        self.assertIsNone(self.model.status()['model_service']['startup_estimate'])
        with self.assertRaisesRegex(c.ModelControlError,'持久显存样本'):self.action('start')
        self.assertEqual(self.fake.calls.count('start'),0)

    def test_ui_and_real_preflight_share_identity_and_budget(self):
        self.action('stop');state=json.loads(s.STATE.read_text());saved=c.context_env(self.fake.snapshot())
        displayed=self.model.status()['model_service']['startup_estimate']
        actual=h.check_start_memory(self.fake,state,saved,self.policy)['estimate']
        self.assertEqual(displayed,actual)
        self.fake.unit_profile=lambda:'different configured weights or KV settings'
        self.assertIsNone(self.model.status()['model_service']['startup_estimate'])
        with self.assertRaisesRegex(h.MaintenanceError,'profile_mismatch'):h.check_start_memory(self.fake,state,saved,self.policy)

    def test_legacy_stop_record_recovers_without_start_or_manual_sample(self):
        self.action('stop');state=json.loads(s.STATE.read_text());saved=c.context_env(self.fake.snapshot())
        identity=self.fake.configuration_identity();identity.update(_legacy_display=display(stopped=True),
            _execution=s.exec_configuration(display(stopped=True)),_latest_change=0)
        stamp=datetime.strptime('2026-11-25 10:30:00','%Y-%m-%d %H:%M:%S').timestamp()
        state.update(model_name=identity['weights_name'],sample=dict(model_mib=29102,sampled_at=stamp,
            configuration=saved,policy=self.policy.digest,gpu_index=0,
            unit_profile=hashlib.sha256(display().encode()+self.fake.snapshot()).hexdigest()))
        self.assertNotEqual(state['sample']['unit_profile'],hashlib.sha256(display(stopped=True).encode()+self.fake.snapshot()).hexdigest())
        s.STATE.write_text(json.dumps(state));self.fake.configuration_identity=lambda gpu=None:identity
        before=s.STATE.read_bytes();result=self.model.status()['model_service']
        self.assertEqual(result['startup_estimate']['required_mib'],30126)
        self.assertEqual(s.STATE.read_bytes(),before)
        self.assertEqual(self.fake.calls.count('start'),0)
        self.assertEqual(self.action('start')['model']['model_service']['state'],'running')
        recovered=json.loads(s.STATE.read_text())['sample']
        self.assertEqual(recovered['model_mib'],29102)
        self.assertEqual(recovered['source'],'verified_legacy_stop_record')
        self.assertEqual(recovered['profile_version'],2)
        with sqlite3.connect(self.model.db) as db:
            self.assertEqual(db.execute("SELECT actor,operation,outcome,reason FROM model_service_audit ORDER BY id DESC LIMIT 1").fetchone(),
                             ('admin','start','applied','sample_recovered'))

    def test_unprovable_legacy_record_and_unsampled_stop_fail_closed(self):
        self.action('stop');state=json.loads(s.STATE.read_text())
        state['sample'].pop('profile_version');state['sample']['unit_profile']='f'*64
        s.STATE.write_text(json.dumps(state))
        self.assertIsNone(self.model.status()['model_service']['startup_estimate'])
        with self.assertRaises(c.ModelControlError):self.action('start')
        # A running instance must not be stopped if its sample cannot be saved.
        s.STATE.unlink();s.SAMPLE_RECORD.unlink();self.fake.running=True
        original=self.fake.gpu
        def no_process_memory(pid=None):
            gpu=original(pid);gpu.pop('model_process_mib',None);gpu.pop('_model_mib',None);return gpu
        self.fake.gpu=no_process_memory;count=self.fake.calls.count('stop')
        with self.assertRaisesRegex(c.ModelControlError,'无法采集'):self.action('stop')
        self.assertEqual(self.fake.calls.count('stop'),count)

    @unittest.skipUnless(os.name=='posix','requires native helper flock')
    def test_readonly_inspect_rpc_uses_same_preflight_while_model_is_offline(self):
        self.action('stop');state_before=s.STATE.read_bytes();record_before=s.SAMPLE_RECORD.read_bytes()
        lock=self.root/'helper.lock';lock.write_bytes(b'')
        def rpc(args,**options):
            self.assertEqual(args,['/usr/bin/sudo','-n','--',c.CONTROLLER])
            payload=h.parse_request(options['input']);self.assertEqual(payload['operation'],'inspect')
            result=h.execute_change(payload)
            return SimpleNamespace(returncode=0,stdout=json.dumps(result))
        with patch.object(h,'LOCK_FILE',lock),patch.object(c.Policy,'read',return_value=self.policy), \
                patch.object(h,'ServiceHost',return_value=self.fake),patch.object(h,'api_key',side_effect=AssertionError('inspect must not load a model API key')), \
                patch.object(c.subprocess,'run',side_effect=rpc):
            self.model.service_inspection=lambda policy:c.ModelControl.service_inspection(self.model,policy)
            shown=self.model.status()['model_service']['startup_estimate']
        checked=h.check_start_memory(self.fake,json.loads(state_before),c.context_env(self.fake.snapshot()),self.policy)['estimate']
        self.assertEqual(shown,checked)
        self.assertEqual(s.STATE.read_bytes(),state_before);self.assertEqual(s.SAMPLE_RECORD.read_bytes(),record_before)
        self.assertEqual(self.fake.calls.count('start'),0)


class ProfileTests(unittest.TestCase):
    def test_exec_runtime_fields_do_not_change_stable_configuration(self):
        before=s.exec_configuration(display());after=s.exec_configuration(display(stopped=True))
        self.assertEqual(before['stable'],after['stable'])
        self.assertNotEqual(s.exec_configuration(display(weights='/models/Other-Q8_0.gguf'))['stable'],after['stable'])

    def test_real_identity_binds_weights_quantization_environment_gpu_and_backend(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory).resolve();exe=root/'llama-server';weights=root/'Fixture-Q4_K_M.gguf'
            unit=root/'model.service';env=root/'llm.env';context=root/'context.env'
            exe.write_bytes(b'backend');weights.write_bytes(b'GGUF fixture');unit.write_bytes(b'fixed unit')
            env.write_text('KV_CACHE=f16\nAPI_KEY=fixture-must-not-be-exposed\n');context.write_bytes(h.settings(163840,1))
            policy=c.Policy('a'*64,0,'http://172.17.0.1:8000','lab-general',root/'key')
            gpu=dict(index=0,_uuid='GPU-fixture',name='Fixture RTX 5090',total_mib=32607,free_mib=32000)
            stopped=[False]
            props=dict(Environment='CUDA_VISIBLE_DEVICES=0',EnvironmentFiles=env.as_posix()+' (ignore_errors=no) '+context.as_posix()+' (ignore_errors=no)',
                FragmentPath=unit.as_posix(),DropInPaths='',User='root',Group='',WorkingDirectory=root.as_posix(),
                RootDirectory='',RootImage='',MemoryMax='infinity',LimitMEMLOCK='infinity')
            def command(args):
                if args[0]==c.NVIDIA_SMI:return 'fixture-driver'
                if '--property=ExecStart' in args:return display(exe.as_posix(),weights.as_posix(),stopped[0])
                return '\n'.join(k+'='+v for k,v in props.items())
            real_lstat=Path.lstat
            def lstat(path,*args,**kwargs):
                if path==unit:
                    value=real_lstat(path,*args,**kwargs)
                    return SimpleNamespace(st_uid=0,st_mode=0o100644,st_mtime=value.st_mtime,st_ctime=value.st_ctime)
                return real_lstat(path,*args,**kwargs)
            with patch.object(c,'_fixed_command',side_effect=command),patch.object(c,'_root_file',side_effect=lambda p:Path(p).read_bytes()),patch.object(Path,'lstat',lstat):
                first=s.configuration_identity(c,policy,context.read_bytes(),gpu)
                stopped[0]=True
                self.assertEqual(first['fingerprint'],s.configuration_identity(c,policy,context.read_bytes(),gpu)['fingerprint'])
                self.assertEqual(first['quantization'],'Q4_K_M')
                self.assertNotIn('fixture-must-not-be-exposed',json.dumps(s.public_identity(first)))
                for mutate in (lambda:env.write_text('KV_CACHE=q8_0'),lambda:weights.write_bytes(b'changed weights'),
                               lambda:exe.write_bytes(b'new backend'),lambda:gpu.update(_uuid='GPU-replaced'),
                               lambda:context.write_bytes(h.settings(131072,1))):
                    previous=s.configuration_identity(c,policy,context.read_bytes(),gpu)['fingerprint'];mutate()
                    self.assertNotEqual(previous,s.configuration_identity(c,policy,context.read_bytes(),gpu)['fingerprint'])


if __name__=='__main__':unittest.main()
