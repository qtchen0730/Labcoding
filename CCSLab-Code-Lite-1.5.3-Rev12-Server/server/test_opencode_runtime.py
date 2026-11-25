import json
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs

from .opencode_runtime import OpenCodeRuntime, VerificationError, NoRedirect
from .output_settings import OpenCodeContext
from .test_support import TEST_ROOT


def documents(context, path):
    return [dict(type='document', path=str(path), info={'providers': {'ccslab': {
        'options': {'apiKey': 'fixture-key-must-not-return'},
        'models': {'lab-general': {'limit': {'context': context, 'output': 8192}}}}}}),
        dict(type='directory', path='/home/server/.config/opencode'),
        dict(type='directory', path='/home/server/.opencode')]


class Observed(OpenCodeRuntime):
    def __init__(self, contexts):
        super().__init__()
        self.contexts, self.calls, self.configurations = contexts, [], {}
    def _instances(self):
        return [dict(pid=pid, args=['opencode', 'serve']) for pid in self.contexts]
    def _identity(self, row): pass
    def _connection(self, row):
        self.pid = row['pid']
        return 1, 'fixture-auth-never-returned'
    def _get(self, port, authorization, route, timeout):
        self.calls.append(('GET', route))
        if route == '/api/info': return dict(version='2.3.0', pid=self.pid)
        value = self.contexts[self.pid]
        values = value if isinstance(value, dict) else {'/loaded/project': value}
        if route == '/api/debug/location': return [dict(directory=d) for d in values]
        parsed = urlsplit(route)
        directory = parse_qs(parsed.query)['location[directory]'][0]
        if parsed.path == '/api/config':
            return self.configurations.get((self.pid,directory), documents(values[directory],self.config))
        if parsed.path == '/api/model':
            return {'data': [dict(providerID='ccslab', id='lab-general', limit=dict(context=values[directory]))]}
        raise AssertionError(route)


class RuntimeTests(unittest.TestCase):
    def test_no_instance_is_ready_and_multiple_instances_are_not_collapsed(self):
        self.assertEqual(Observed({}).status(131072)['status'], 'ready')
        for contexts, expected in (({1:131072}, 'synced'), ({1:163840}, 'pending_reload'),
                                    ({1:131072,2:163840}, 'partial'), ({1:131072,2:131072}, 'synced')):
            runtime=Observed(contexts); state=runtime.status(131072)
            self.assertEqual(state['status'], expected)
            self.assertEqual(state['instance_count'],len(contexts))
            self.assertEqual(state['effective_context_limit'],next(iter(contexts.values())) if len(set(contexts.values()))==1 else None)
            self.assertFalse(state['automatic_reload'])
            self.assertTrue(all(method=='GET' for method,path in runtime.calls))
            self.assertFalse(any('prompt' in path or 'completion' in path for method,path in runtime.calls))

    def test_unknown_instance_pid_change_and_shared_locations_do_not_report_success(self):
        runtime=Observed({1:131072,2:131072})
        def identify(row):
            if row['pid']==2: raise VerificationError('实例已退出或 PID 已变化')
        runtime._identity=identify
        self.assertEqual(runtime.status(131072)['status'],'partial')
        with patch.object(runtime,'_instances',side_effect=PermissionError('secret-not-for-api')):
            result=runtime.status(131072)
            self.assertEqual(result['status'],'unverified');self.assertNotIn('secret-not-for-api',str(result))
        runtime=Observed({1:131072})
        runtime._instances=lambda:[dict(pid=1,args=['opencode','serve','--service'])]
        self.assertEqual(runtime.status(131072)['status'],'synced')
        runtime=Observed({1:131072})
        runtime._connection=lambda row: (_ for _ in ()).throw(VerificationError('私有交互实例无 HTTP 接口'))
        result=runtime.status(131072)
        self.assertEqual(result['status'],'unverified');self.assertIn('交互',result['detail'])
        self.assertNotIn('fixture-auth',str(result))

    def test_file_ready_is_separate_from_runtime_and_helper_readback_is_unchanged(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            path=Path(folder)/'opencode.json'
            document={'provider':{'ccslab':{'options':{'apiKey':'fixture-only'},'models':{'lab-general':{
                'limit':{'context':163840,'output':8192,'input':120000}}}}},'compaction':{'auto':True}}
            path.write_text(json.dumps(document));path.chmod(0o600)
            store=OpenCodeContext(path);store.runtime=Observed({})
            before=path.read_bytes()
            self.assertEqual(store.runtime_status(163840)['status'],'ready')
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(store.status(163840)['status'],'pending_reload')
            self.assertEqual(store.runtime_status(131072)['status'],'out_of_sync')
            store.sync(131072)
            document['provider']['ccslab']['models']['lab-general']['limit']['context']=131072
            self.assertEqual(json.loads(path.read_text()),document)
            self.assertEqual(store.runtime_status(131072)['status'],'ready')
            store.runtime=Observed({12:163840})
            store.runtime.config=path
            self.assertEqual(store.runtime_status(131072)['status'],'pending_reload')
            self.assertEqual(store.runtime_status(131072)['effective_context_limit'],163840)
            store.runtime=Observed({12:131072})
            store.runtime.config=path
            self.assertEqual(store.runtime_status(131072)['effective_context_limit'],131072)
            # Native watchers may invalidate the provider cache asynchronously.
            # Explicit synchronization waits using reads; it must not reload a location.
            store.runtime=Observed({12:163840});read=store.runtime.status;rounds=[]
            store.runtime.config=path
            def refresh(context):
                rounds.append(context)
                if len(rounds)>1:store.runtime.contexts[12]=context
                return read(context)
            store.runtime.status=refresh
            before=path.read_bytes()
            self.assertEqual(store.synchronize_runtime(131072)['status'],'synced')
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(len(rounds),2)
            self.assertTrue(all(method=='GET' for method,path in store.runtime.calls))

    def test_redirects_never_receive_runtime_credentials(self):
        with self.assertRaises(VerificationError):
            NoRedirect().redirect_request(None,None,302,'redirect',{},'http://remote.invalid')

    def test_malformed_runtime_replies_do_not_become_verified_or_trigger_mutations(self):
        for route, bad in (('/api/info', {'version':'2.3.0','pid':999}),
                           ('/api/model', {'data':'invalid'}),
                           ('/api/config', {}), ('/api/config', []),
                           ('/api/debug/location', [])):
            runtime=Observed({1:131072});read=runtime._get
            runtime._get=lambda port,auth,path,timeout: bad if urlsplit(path).path==route else read(port,auth,path,timeout)
            result=runtime.status(131072)
            self.assertEqual(result['status'],'unverified')
            self.assertIsNone(result['effective_context_limit'])
            self.assertFalse(result['automatic_reload'])

    @unittest.skipUnless(os.name=='posix','requires Linux proc symlinks')
    def test_proc_identity_association_socket_and_namespace_are_required(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root=Path(folder);cfg=root/'opencode.json';cfg.write_text('{}')
            proc=root/'proc';proc.mkdir(); p=proc/'123';p.mkdir()
            binary=root/'opencode';(p/'exe').symlink_to(binary)
            (p/'comm').write_text('opencode')
            (p/'cmdline').write_bytes(b'opencode\0serve\0--port\012345\0')
            fields=['S','1']+['0']*17+['100']+['0']*10
            (p/'stat').write_text('123 (opencode fixture) '+' '.join(fields))
            (p/'environ').write_bytes(b'OPENCODE_CONFIG='+os.fsencode(cfg)+b'\0OPENCODE_SERVER_PASSWORD=fixture-only\0')
            (p/'fd').mkdir();(p/'fd/3').symlink_to('socket:[777]')
            (p/'net').mkdir();(p/'net/tcp').write_text('header\n 0: 0100007F:3039 00000000:0000 0A 0 0 0 0 0 777\n')
            for d in (p/'ns',proc/'self/ns'):d.mkdir(parents=True)
            (p/'ns/net').symlink_to('net:[123]');(proc/'self/ns/net').symlink_to('net:[123]')
            runtime=OpenCodeRuntime(cfg,binary=binary,proc=proc)
            row=runtime._instances()[0];runtime._identity(row)
            port,auth=runtime._connection(row)
            self.assertEqual(port,12345);self.assertTrue(auth.startswith('Basic '))
            (p/'stat').write_text('123 (opencode fixture) '+' '.join(fields[:19]+['101']+fields[20:]))
            with self.assertRaises(VerificationError):runtime._identity(row)
            (p/'net/tcp').write_text('header\n 0: 00000000:3039 00000000:0000 0A 0 0 0 0 0 777\n')
            with self.assertRaises(VerificationError):runtime._connection(row)

    def test_all_locations_all_config_documents_and_priority(self):
        runtime=Observed({8123:{'/one':131072,'/two & space':131072},9517:131072})
        key=(8123,'/one')
        runtime.configurations[key]=[dict(type='document',path='/other.json',info={'providers':{'other':{}}}),
                                     *documents(131072,runtime.config)]
        state=runtime.status(131072)
        self.assertEqual(state['status'],'synced')
        self.assertEqual(state['instances'][0]['location_count'],2)
        self.assertEqual(state['runtime_scope'],'all_loaded_locations')
        self.assertNotIn('fixture-key',str(state))
        runtime.contexts[8123]['/two & space']=163840
        self.assertEqual(runtime.status(131072)['status'],'partial')
        runtime.contexts={8123:163840}
        key=(8123,'/loaded/project')
        runtime.configurations[key]=documents(131072,runtime.config)+documents(163840,'/project/override.json')[:1]
        state=runtime.status(131072)
        self.assertEqual(state['effective_context_limit'],163840)
        self.assertEqual(state['status'],'pending_reload')
        # Loaded model readback must agree with all documents' precedence.
        runtime.contexts[8123]=131072
        self.assertEqual(runtime.status(131072)['status'],'unverified')
        runtime.configurations[key]=documents(True,runtime.config)
        self.assertEqual(runtime.status(131072)['status'],'unverified')
        runtime.configurations[key]=documents(131072,'/unmanaged.json')
        self.assertEqual(runtime.status(131072)['status'],'unverified')

    def test_location_and_process_lists_must_remain_complete_during_reads(self):
        runtime=Observed({1:{'/one':131072,'/two':131072}})
        runtime.configurations[(1,'/two')]=[dict(type='document',path=str(runtime.config),info={})]
        state=runtime.status(131072)
        self.assertEqual(state['status'],'unverified')
        self.assertIsNone(state['effective_context_limit'])
        runtime=Observed({1:131072});read=runtime._get;count=[]
        def changing(port,auth,route,timeout):
            value=read(port,auth,route,timeout)
            if route=='/api/debug/location':
                count.append(1)
                if len(count)>1:value.append(dict(directory='/newly-loaded'))
            return value
        runtime._get=changing
        self.assertEqual(runtime.status(131072)['status'],'unverified')
        runtime=Observed({1:131072});initial=runtime._instances()
        runtime._instances=lambda:initial if not runtime.calls else [dict(pid=99,start='replacement')]
        self.assertEqual(runtime.status(131072)['status'],'unverified')

    def test_real_http_basic_list_401_and_redirect_never_leak_credentials(self):
        calls=[];expected='Basic '+base64.b64encode(b'opencode:fixture-password').decode()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append(self.path)
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/must-not-follow');self.end_headers();return
                if self.headers.get('Authorization')!=expected:
                    self.send_response(401);self.end_headers();return
                values={'/api/config':documents(131072,OpenCodeRuntime().config),
                        '/api/info':dict(pid=7319,version='2.3.0'),
                        '/api/debug/location':[dict(directory='/loaded/project')],
                        '/api/model':{'data':[dict(providerID='ccslab',id='lab-general',limit=dict(context=131072))]}}
                self.send_response(200);self.end_headers()
                self.wfile.write(json.dumps(values[urlsplit(self.path).path]).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            runtime=Observed({7319:131072});runtime._get=OpenCodeRuntime._get.__get__(runtime)
            runtime._connection=lambda row:(server.server_port,expected)
            with patch.dict(os.environ,{'http_proxy':'http://127.0.0.1:1'}):
                self.assertEqual(runtime.status(131072)['status'],'synced')
            runtime._connection=lambda row:(server.server_port,'Basic wrong-fixture')
            result=runtime.status(131072)
            self.assertEqual(result['status'],'unverified');self.assertIn('认证失败',result['detail'])
            self.assertNotIn('fixture-password',str(result))
            with self.assertRaises(HTTPError) as caught:runtime._get(server.server_port,'','/api/config',.7)
            self.assertEqual(caught.exception.code,401)
            caught.exception.close()
            with self.assertRaises(VerificationError):runtime._get(server.server_port,expected,'/redirect',.7)
            self.assertNotIn('/must-not-follow',calls)
        finally:
            server.shutdown();server.server_close();worker.join(timeout=2)

    @unittest.skipUnless(os.name=='posix','requires Linux proc symlinks')
    def test_proc_stdio_parent_child_unrelated_denial_target_denial_and_restart(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root=Path(folder);cfg=root/'opencode.json';cfg.write_text('{}');proc=root/'proc';proc.mkdir()
            binary=root/'opencode';(proc/'self/ns').mkdir(parents=True)
            (proc/'self/ns/net').symlink_to('net:[123]')
            def process(pid,ppid,args,name='opencode',start='100',port=38291):
                p=proc/str(pid);p.mkdir();(p/'comm').write_text(name)
                (p/'exe').symlink_to(binary if name=='opencode' else root/name)
                (p/'cmdline').write_bytes(b'\0'.join(os.fsencode(a) for a in args)+b'\0')
                fields=['S',str(ppid)]+['0']*17+[start]+['0']*10
                (p/'stat').write_text(str(pid)+' (fixture) '+' '.join(fields))
                (p/'environ').write_bytes(b'OPENCODE_CONFIG='+os.fsencode(cfg)+b'\0OPENCODE_PASSWORD=fixture-primary\0OPENCODE_SERVER_PASSWORD=wrong-fallback\0OPENCODE_SERVER_USERNAME=wrong-user\0')
                (p/'fd').mkdir();(p/'fd/3').symlink_to('socket:[777]')
                (p/'net').mkdir();(p/'net/tcp').write_text('header\n 0: 0100007F:'+format(port,'04X')+' 00000000:0000 0A 0 0 0 0 0 777\n')
                (p/'ns').mkdir();(p/'ns/net').symlink_to('net:[123]');return p
            process(7123,1,['opencode'])
            child=process(9817,7123,['opencode','serve','--stdio'])
            unrelated=process(22,1,['sshd'],name='sshd')
            runtime=OpenCodeRuntime(cfg,binary=binary,proc=proc);readlink=Path.readlink
            def denied(path):
                if path==unrelated/'exe':raise PermissionError('unrelated denial')
                return readlink(path)
            with patch.object(Path,'readlink',denied):rows=runtime._instances()
            self.assertEqual([r['pid'] for r in rows],[9817]);row=rows[0]
            port,auth=runtime._connection(row)
            self.assertEqual(port,38291)
            self.assertEqual(base64.b64decode(auth.split()[1]),b'opencode:fixture-primary')
            def target_denied(path):
                if path==child/'exe':raise PermissionError('secret-not-for-api')
                return readlink(path)
            with patch.object(Path,'readlink',target_denied):
                result=runtime.status(131072)
                self.assertEqual(result['status'],'unverified');self.assertIn('9817',result['detail'])
                self.assertNotIn('secret-not-for-api',str(result))
            fields=(child/'stat').read_text().rsplit(')',1)[1].split();fields[19]='101'
            (child/'stat').write_text('9817 (fixture) '+' '.join(fields))
            with self.assertRaises(VerificationError):runtime._identity(row)
            (child/'exe').unlink()
            process(11037,7123,['opencode','serve','--stdio'],start='202',port=41083)
            rows=runtime._instances()
            self.assertEqual([r['pid'] for r in rows],[11037]);self.assertEqual(runtime._connection(rows[0])[0],41083)
