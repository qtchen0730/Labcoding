"""Native lifecycle UI checks/preview, with in-memory data and no network transport."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from time import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'app'))
from app import App
from account_client import AccountError
from verify_admin_cards import ModelConfigFixture, wait, descendants
from verify_support import TEST_ROOT
from themes import THEMES
from versioning import read_version_info


class LifecycleFixture(ModelConfigFixture):
    def __init__(self, phase='running'):
        super().__init__()
        self.phase = phase
        self.fail_service = False
        self.set_phase(phase)

    def set_phase(self, phase):
        self.phase = phase
        running = phase == 'running'
        now = time()
        self.model.update(model_id='lab-general', model_name='Qwen3.6-27B Q4_K_M · 隔离预览',
            status='running' if running else 'unavailable', total_context=163840 if running else None,
            slots=2 if running else None, effective_context_limit=81920 if running else None,
            output_limit=8192, output_token_limit=8192, available_slots=2 if running else 0,
            control_available=running, detail='本地隔离数据，不连接任何服务器。' if running else
                '模型服务已停止；账号及额度可用，参数变更与重新同步暂停。',
            gpu=dict(index=0, name='NVIDIA GeForce RTX 5090', used_mib=29000 if running else 1000,
                     total_mib=32607, free_mib=3107 if running else 31107, unaccounted_mib=500,
                     utilization_percent=88.9 if running else 3.1, sampled_at=now),
            model_service=dict(state=phase, state_label='运行中' if running else '已停止',
                model_name='Qwen3.6-27B Q4_K_M', backend='llama.cpp', gpu_index=0,
                api_available=running, pid=22222 if running else None,
                model_process_mib=28000 if running else None, estimated_release_mib=28000 if running else None,
                observed_released_mib=None if running else 28000, checked_at=now,
                last_started_at='2026-11-25 14:00:00 CST',
                startup_estimate=dict(required_mib=29024, safety_margin_mib=1024, measured_model_mib=28000),
                gpu_free_mib=3107 if running else 31107,
                can_start=not running, can_stop=running, can_restart=running))
        if running: self.model['gpu']['model_process_mib'] = 28000
        self.model['context_sync'] = dict(saved_configuration=dict(total_context=163840, slots=2),
            backend=dict(status='synced' if running else 'unverified', effective_context_limit=81920 if running else None,
                         ready_checked_at=now if running else None),
            builtin=dict(context_limit=81920 if running else None),
            opencode=dict(managed=True, status='synced' if running else 'ready', instance_count=1 if running else 0,
                          configured_context_limit=81920, effective_context_limit=81920 if running else None))

    def request(self, method, path, body=None, **options):
        if path == '/api/admin/model/service/preview':
            self.requests.append((method,path,body)); self.operation = body['operation']
            return dict(confirmation='isolated-ui-ticket', expires_in=90, service=deepcopy(self.model['model_service']),
                        running_tasks=0, impact='仅演示本地界面。无生产连接、无模型推理、无真实服务控制。')
        if path == '/api/admin/model/service/apply':
            self.requests.append((method,path,body))
            if self.fail_service:
                raise AccountError('隔离：活动任务存在，拒绝停止。',409)
            self.set_phase('stopped' if self.operation == 'stop' else 'running')
            return dict(status='applied', model=deepcopy(self.model))
        if path == '/api/tasks':
            return dict(slots=2, running_tasks=0, available_slots=2 if self.phase == 'running' else 0)
        return super().request(method,path,body,**options)


def create(phase, data):
    root = tk.Tk()
    gui = App(root, data, smoke=True)
    fixture = LifecycleFixture(phase)
    gui.account_client = fixture
    gui.account = dict(id=2, username='Lifecycle.Preview', role='admin', status='active')
    gui.account_online = True
    gui.engine = SimpleNamespace(limits=(81920,8192), loaded_limits=dict(context=81920,input=79872,output=8192),
        process=SimpleNamespace(poll=lambda:None), verify_loaded_limits=lambda:dict(context=81920,input=79872,output=8192))
    gui.theme.set('Midnight'); gui.show_page('admin')
    page = gui.page_controller; page.tabs.select(page.model_tab)
    root.title('Labcoding '+read_version_info()['version']+' · '+('运行中' if phase == 'running' else '已停止')+' · 隔离卡片布局预览')
    root.state('zoomed')
    return root,gui,page,fixture


def verify(root, gui, page, fixture):
    wait(root,lambda:not gui.busy and page.model_fields['service_state'].get()=='运行中')
    administrator = gui.account
    gui.account = dict(administrator, role='user')
    before = len(fixture.requests)
    for operation in ('start', 'stop', 'restart'):
        page.service_preview(operation)
    assert len(fixture.requests) == before
    gui.account = administrator
    assert page.model_fields['service_memory'].get()=='28,000 MiB'
    assert str(page.service_buttons['start'].cget('state'))=='disabled'
    page.total.set('131072');page.refresh_model_button.invoke();wait(root,lambda:not gui.busy)
    assert page.total.get()=='131072'
    for theme in ('Dark','Midnight'):
        gui.theme.set(theme);gui.apply_theme()
        for width in (1440,800):
            root.state('normal');root.geometry(f'{width}x950');root.update()
            for widget in (*page.model_field_labels.values(),*page.service_buttons.values()):
                x=widget.winfo_rootx()-page.model_viewport.winfo_rootx()
                assert x>=0 and x+widget.winfo_width()<=page.model_viewport.winfo_width(),(theme,width,widget)
            verify_cards(root,gui,page)
    with patch('admin_view.messagebox.askyesno',return_value=False):
        before=len(fixture.requests);page.service_buttons['stop'].invoke();wait(root,lambda:not gui.busy)
        assert not any(p.endswith('/apply') for _,p,_ in fixture.requests[before:])
    with patch('admin_view.messagebox.askyesno',return_value=True):
        page.service_buttons['stop'].invoke()
        wait(root,lambda:not gui.busy and page.model_fields['service_state'].get()=='已停止')
    assert page.model_fields['service_pid'].get()=='未运行'
    assert page.model_fields['service_memory'].get()=='未加载'
    assert page.model_fields['backend_context'].get()=='未加载'
    assert page.model_fields['gpu_process'].get()=='未运行'
    assert page.model_fields['service_released'].get()=='28,000 MiB'
    assert all(v.get()=='未加载' for v in page.model_metrics[:3])
    assert str(page.resync_button.cget('state'))=='disabled' and str(page.preview_button.cget('state'))=='disabled'
    assert str(page.service_buttons['start'].cget('state'))=='normal'
    verify_cards(root,gui,page)
    before=len(fixture.requests);page.refresh_model_button.invoke();wait(root,lambda:not gui.busy)
    assert all(method=='GET' for method,path,body in fixture.requests[before:])
    with patch('admin_view.messagebox.askyesno',return_value=True),patch.object(gui,'reload_idle_model_engine',side_effect=lambda on_done:on_done()):
        page.service_buttons['start'].invoke()
        wait(root,lambda:not gui.busy and page.model_fields['service_state'].get()=='运行中')
    assert fixture.model['effective_context_limit']==81920
    fixture.fail_service=True
    with patch('admin_view.messagebox.askyesno',return_value=True):
        page.service_buttons['stop'].invoke()
        wait(root,lambda:not gui.busy and '最近操作失败' in page.service_note.get())
    assert page.model_fields['service_state'].get()=='运行中'
    print('PASS native lifecycle controls/confirmation/read-only refresh/draft preservation/offline fields; Dark+Midnight; 1440+800 layout',flush=True)
    # Every existing palette must update the card surface and its visible outline.
    for label,_colors in THEMES.values():
        gui.theme.set(label);gui.apply_theme();root.update()
        verify_cards(root,gui,page)
    print('PASS five rounded section cards; themed borders; responsive subgroups; all seven palettes; scrollable full page',flush=True)


def verify_cards(root,gui,page):
    expected=('核心运行指标','模型服务控制','模型参数设置','上下文配置与同步状态','服务器 GPU 资源')
    assert tuple(page.model_cards)==expected
    prior_bottom=0
    for title,card in page.model_cards.items():
        assert card.itemcget(card.outline,'smooth')=='true'
        assert float(card.itemcget(card.outline,'width'))==1
        assert root.winfo_rgb(card.itemcget(card.outline,'fill'))==root.winfo_rgb(gui.colors['panel'])
        assert root.winfo_rgb(card.itemcget(card.outline,'outline'))!=root.winfo_rgb(gui.colors['panel'])
        assert card.body.winfo_width()==card.winfo_width()-card.padding*2
        assert card.winfo_height()>=card.body.winfo_height()+card.padding*2
        assert card.winfo_y()>=prior_bottom+6
        prior_bottom=card.winfo_y()+card.winfo_height()
    service=page.model_cards['模型服务控制'].body
    groups=next(child for child in service.winfo_children() if len(child.grid_slaves())==4)
    expected_columns=2 if groups.winfo_width()>=880 else 1
    assert len({int(shelf.grid_info()['column']) for shelf in groups.grid_slaves()})==expected_columns
    # Time/advanced details remain inside synchronization instead of adding a sixth primary card.
    advanced=page.advanced_toggle
    while advanced.master is not page.model_viewport:
        if advanced is page.model_cards['上下文配置与同步状态']:break
        advanced=advanced.master
    else:raise AssertionError('advanced details left the synchronization card')
    assert page.model_viewport.bbox('all')[3]>page.model_viewport.winfo_height()


if __name__=='__main__':
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    with tempfile.TemporaryDirectory(dir=TEST_ROOT,prefix='lifecycle-ui-') as folder:
        phase=sys.argv[2] if len(sys.argv)>2 and sys.argv[1]=='--preview' else 'running'
        root,gui,page,fixture=create(phase,Path(folder))
        def close(): gui.engine=None;gui.close()
        root.protocol('WM_DELETE_WINDOW',close)
        if '--preview' in sys.argv:
            root.after(1200,lambda:(root.state('zoomed'),page.model_viewport.yview_moveto(0)))
            root.mainloop()
        else:
            try:verify(root,gui,page,fixture)
            finally:close()
