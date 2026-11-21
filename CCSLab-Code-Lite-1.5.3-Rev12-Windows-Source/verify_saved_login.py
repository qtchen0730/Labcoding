"""Restart/login regression using real DPAPI, Tk and loopback HTTPS account fixtures."""
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from http.server import ThreadingHTTPServer
from time import monotonic, sleep
import tkinter as tk
import unittest
from unittest.mock import patch

from verify_support import TEST_ROOT
from verify_tls import tls_files
from verify_account_transport import Gateway, serving
sys.path.insert(0,str(Path(__file__).parent/'app'))
from app import App, write_json
from account_client import AccountClient
from saved_login import SavedLogin

USER = {"id":7,"username":"remember-fixture","role":"user","assigned_role":"user","status":"active"}
PASSWORD = "remember-fixture-password"
TOKEN = "renewed-fixture-session"


class LoginGateway(Gateway):
    def handle_request(self):
        if self.command == "POST" and self.path == "/api/login" and not self.server.reject_login:
            self.server.replies["/api/me"] = (200,{"user":USER})
        super().handle_request()
    do_GET = do_POST = handle_request


class RememberedLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT,prefix="login-tls-")
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.cert, key, cls.trust = tls_files(Path(cls.scratch.name))
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls.load_cert_chain(cls.cert,key)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT,prefix="remember-login-")
        self.addCleanup(self.scratch.cleanup)
        self.data = Path(self.scratch.name)
        self.server = ThreadingHTTPServer(("127.0.0.1",0),LoginGateway)
        self.server.socket = self.tls.wrap_socket(self.server.socket,server_side=True)
        self.server.requests,self.server.delay,self.server.reject_login = [],0,False
        self.server.replies = {
            "/api/me":(401,{"detail":"expired fixture"}),
            "/api/login":(200,{"token":TOKEN,"user":USER}),
            "/api/logout":(204,b""),
            "/api/model":(200,{"model_id":"lab-general","effective_context_limit":26368,"output_token_limit":4096}),
        }
        self.enterContext(serving(self.server))
        self.address = f"https://127.0.0.1:{self.server.server_port}"
        write_json(self.data/'preferences.json',{"account_server":self.address,"account_server_manual":True,
                                                 "account_proxy_mode":"direct","theme":"light"})
        self.store = SavedLogin(self.data)
        self.store.save(self.address,USER['username'],PASSWORD,'expired-fixture-session')
        self.gui = None
        self.addCleanup(self.close_gui)

    def close_gui(self):
        if self.gui and not self.gui.closing:
            self.gui.close()

    def open_gui(self):
        self.root = tk.Tk()
        self.gui = App(self.root,self.data,smoke=True)
        client = AccountClient(self.address,tls=self.trust,mode="direct")
        self.gui.gateway_transport = self.gui.account_connection,client
        return self.gui

    def wait(self,predicate):
        deadline=monotonic()+8
        while monotonic()<deadline:
            self.root.update()
            if predicate(): return
            sleep(.01)
        self.fail("Desktop did not reach the expected fixture state")

    def test_new_process_prefills_and_auto_logs_in_with_saved_credentials(self):
        code = """
from pathlib import Path
import sys,time,tkinter as tk
from app import App
root=tk.Tk(); gui=App(root,Path(sys.argv[1]),smoke=True)
page=gui.page_controller
assert page.username.get()=='remember-fixture'
assert page.password.get()=='remember-fixture-password'
assert page.password_entry[1].cget('show')
gui.restore_saved_login()
deadline=time.monotonic()+8
while (gui.busy or not gui.account or gui.account_polling) and time.monotonic()<deadline:
    root.update();time.sleep(.01)
assert gui.account and gui.account['username']=='remember-fixture'
assert not gui.busy
gui.close()
print('Saved login restored across processes')
"""
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent/'app'),SSL_CERT_FILE=str(self.cert.with_name('ca.pem')),
                 PYTHONIOENCODING="utf-8",TEMP=str(TEST_ROOT),TMP=str(TEST_ROOT))
        for restart in range(2):
            with self.subTest(restart=restart):
                result=subprocess.run([sys.executable,'-B','-c',code,str(self.data)],env=env,
                                      capture_output=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
                self.assertEqual(self.store.load(self.address)['token'],TOKEN)
        logins=[json.loads(body) for method,path,body,_ in self.server.requests if path=='/api/login']
        self.assertEqual(logins,[{"username":USER['username'],"password":PASSWORD}])
        self.assertFalse(any(path=='/api/logout' for _,path,_,_ in self.server.requests))

    def test_offline_restart_keeps_fields_and_retry_recovers(self):
        self.server.replies['/api/me']=(503,{"detail":"offline fixture"})
        app=self.open_gui()
        app.restore_saved_login()
        self.wait(lambda:not app.busy)
        self.assertEqual(app.page_controller.username.get(),USER['username'])
        self.assertEqual(app.page_controller.password.get(),PASSWORD)
        self.assertIn('saved_login',app.timers)
        self.assertEqual(self.store.load(self.address)['password'],PASSWORD)
        self.server.replies['/api/me']=(401,{"detail":"expired fixture"})
        app.schedule('saved_login',1,app.restore_saved_login)
        self.wait(lambda:app.account is not None and not app.busy and not app.account_polling)
        self.assertEqual(app.account['username'],USER['username'])

    def test_invalid_password_is_preserved_without_repeated_automatic_attempts(self):
        self.server.reject_login=True
        self.server.replies['/api/login']=(401,{"detail":"invalid fixture"})
        app=self.open_gui()
        app.restore_saved_login()
        self.wait(lambda:not app.busy)
        self.assertTrue(app.saved_login_suppressed)
        self.assertNotIn('saved_login',app.timers)
        self.assertEqual(self.store.load(self.address)['password'],PASSWORD)
        self.assertEqual(app.page_controller.password.get(),PASSWORD)
        before=len(self.server.requests)
        app.restore_saved_login()
        self.root.update()
        self.assertEqual(len(self.server.requests),before)

    def test_switch_server_never_prefills_other_origin_credentials(self):
        app=self.open_gui()
        page=app.page_controller
        self.assertEqual(page.password.get(),PASSWORD)
        app.account_connection=('https://another.fixture.example','direct')
        page.refresh()
        self.assertEqual(page.username.get(),'')
        self.assertEqual(page.password.get(),'')
        self.assertIsNone(self.store.load('https://another.fixture.example'))

    def test_cancel_remember_clears_saved_password_and_pending_auto_login(self):
        app=self.open_gui()
        app.schedule('saved_login',15000,app.restore_saved_login)
        app.page_controller.remember_checkbox.invoke()
        self.assertFalse(self.store.path.exists())
        self.assertTrue(app.saved_login_suppressed)
        self.assertNotIn('saved_login',app.timers)

    def test_timed_out_restore_cannot_recreate_credentials_after_consent_removed(self):
        app = self.open_gui()
        self.server.delay = .25
        with patch('app.WORKER_TIMEOUT', .1):
            app.restore_saved_login()
            self.wait(lambda: not app.busy)
        app.page_controller.remember_checkbox.invoke()
        self.assertFalse(self.store.path.exists())
        deadline = monotonic() + 1.2
        while monotonic() < deadline:
            self.root.update()
            sleep(.01)
        self.assertFalse(self.store.path.exists())
        self.assertIsNone(app.account)
        self.assertTrue(app.saved_login_suppressed)

    def test_busy_startup_defers_saved_login_instead_of_losing_it(self):
        app=self.open_gui()
        app.busy='fixture startup task'
        app.restore_saved_login()
        self.assertIn('saved_login',app.timers)
        self.assertFalse(self.server.requests)
        app.busy=''
        app.schedule('saved_login',1,app.restore_saved_login)
        self.wait(lambda:app.account is not None and not app.busy and not app.account_polling)
        self.assertEqual(app.account['username'],USER['username'])


if __name__=='__main__':
    unittest.main(verbosity=2)
