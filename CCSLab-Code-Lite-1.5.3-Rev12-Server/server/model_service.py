"""Fixed model service state and admission. No process is started from a read."""
from contextlib import closing
import json
import math
import hashlib
import re
import shlex
import stat
from datetime import datetime
from pathlib import Path
import sqlite3
from time import time

STATE = Path('/etc/ccslab/model-service-state.json')
SAMPLE_RECORD = Path('/etc/ccslab/model-service-sample.json')
SAMPLE_VERSION = 2
PHASES = {'running', 'stopping', 'stopped', 'starting', 'start_failed', 'error'}
LABELS = {'running': '运行中', 'stopping': '正在停止', 'stopped': '已停止',
          'starting': '正在启动', 'start_failed': '启动失败', 'error': '状态异常', 'unknown': '状态未知'}
MESSAGES = {
    'busy': '存在活动任务、任务租约、工具/审批或实际推理请求，请等待任务完成。',
    'gpu_insufficient': 'GPU0可用显存不足，未终止任何科研进程，请释放训练资源后重试。',
    'gpu_unknown': '无法读取GPU0可用显存，未尝试启动。',
    'estimate_unknown': '没有当前模型及配置的可信显存样本。请先在模型正常运行时采样，不能盲目启动。',
    'stop_failed': '服务停止或进程退出核验失败，推理入口保持保护。',
    'start_failed': '模型启动或API/实际每槽上下文核验失败，推理入口保持保护。',
    'state_changed': '服务状态或配置版本已变化，请刷新后重新确认。',
    'guard_failed': '直连维护屏障未能建立或核验，未开放推理入口。',
    'unknown': '无法确认服务控制结果，请刷新并检查受控服务状态。',
    'sample_missing': '没有可验证的持久显存样本；未启动模型。请恢复可信采样记录，或另行授权管理员冷采样恢复。',
    'profile_mismatch': '历史样本与当前模型、GPU或实际启动配置不匹配，不能用于本次启动。',
    'sample_unavailable': '无法采集并保存与当前运行配置匹配的显存样本，未停止模型。',
    'inspection_unavailable': '无法通过受控helper完整核验启动样本，尚未确认启动需求。',
    'sample_recovered': '已从可信持久记录恢复匹配样本。',
}


def read_state(reader):
    try:
        value = json.loads(reader(STATE))
        if (not isinstance(value, dict) or value.get('phase') not in PHASES
                or value.get('desired') not in {'running', 'stopped'}
                or type(value.get('generation')) is not int or value['generation'] < 1
                or type(value.get('guard')) is not bool
                or not isinstance(value.get('reason', ''), str)):
            raise ValueError('invalid lifecycle state')
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        return dict(phase='unknown', desired='stopped', generation=-1, guard=True, reason='unknown')


def held(state):
    return bool(state and (state['desired'] != 'running' or state['phase'] != 'running' or state['guard']))


def estimate(sample, saved, policy, identity):
    measured = sample.get('model_mib')
    if (sample.get('configuration') != saved or sample.get('policy') != policy.digest
            or sample.get('gpu_index') != policy.gpu_index or type(measured) is not int or measured <= 0
            or sample.get('profile_version') != SAMPLE_VERSION
            or sample.get('identity') != public_identity(identity)
            or sample.get('unit_profile') != identity.get('fingerprint')):
        return None
    margin = max(1024, math.ceil(measured * .03))
    return dict(measured_model_mib=measured, safety_margin_mib=margin, required_mib=measured + margin,
                sampled_at=sample.get('sampled_at'), estimated=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def exec_configuration(raw):
    """systemctl's ExecStart also contains execution bookkeeping. Never hash it."""
    match = re.fullmatch(r'(?P<stable>.*?;\s*ignore_errors=(?:yes|no))\s*;\s*start_time=\[(?P<start>[^]]*)\]\s*;\s*stop_time=\[(?P<stop>[^]]*)\]\s*;\s*pid=(?P<pid>[0-9]+)\s*;\s*code=[^;]*;\s*status=[^}]*\}\s*', raw)
    if not match:
        raise ValueError('unsupported fixed ExecStart representation')
    stable = match['stable'].strip()
    command = re.fullmatch(r'\{\s*path=(.*?)\s*;\s*argv\[\]=(.*?)\s*;\s*ignore_errors=(?:yes|no)', stable)
    if not command:
        raise ValueError('ambiguous fixed command')
    def epoch(value):
        try:return datetime.strptime(value.rsplit(' ', 1)[0], '%a %Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:return None
    return dict(stable=stable, executable=command[1], arguments=shlex.split(command[2]),
                start_epoch=epoch(match['start']), stop_epoch=epoch(match['stop']), pid=int(match['pid']))


def configuration_identity(control, policy, snapshot, gpu):
    if not gpu or not gpu.get('_uuid'):
        raise ValueError('GPU identity unavailable')
    raw = control._fixed_command([control.SYSTEMCTL, 'show', '--property=ExecStart', '--value', '--', control.MODEL_UNIT])
    execution = exec_configuration(raw)
    keys = ('Environment', 'EnvironmentFiles', 'FragmentPath', 'DropInPaths', 'User', 'Group', 'WorkingDirectory',
            'RootDirectory', 'RootImage', 'MemoryMax', 'LimitMEMLOCK')
    text = control._fixed_command([control.SYSTEMCTL, 'show', '--property=' + ','.join(keys), '--', control.MODEL_UNIT])
    props = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    if not set(keys) <= props.keys():raise ValueError('incomplete unit configuration')
    changed = []
    def file_identity(value):
        path = Path(value)
        if not path.is_absolute():path = Path(props['WorkingDirectory']) / path
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):raise ValueError('model/backend is not a regular file')
        changed.extend((info.st_mtime, info.st_ctime))
        for parent in (path, *path.parents):
            if parent.is_symlink():
                link = parent.lstat();changed.extend((link.st_mtime, link.st_ctime))
        return dict(path=str(path.resolve()), device=info.st_dev, inode=info.st_ino,
                    bytes=info.st_size, modified_ns=info.st_mtime_ns, changed_ns=info.st_ctime_ns)
    args = execution['arguments']; weights = []
    for index, value in enumerate(args):
        if value in {'-m', '--model'} and index + 1 < len(args):weights.append(args[index + 1])
        elif value.startswith('--model='):weights.append(value.split('=', 1)[1])
    if len(weights) != 1:raise ValueError('cannot identify the fixed model file')
    weight = file_identity(weights[0]); executable = file_identity(execution['executable'])
    name = Path(weights[0]).name
    quantization = re.search(r'(?:^|[-.])(IQ[0-9]_[A-Z0-9_]+|Q[0-9]_[A-Z0-9_]+|BF16|F16|F32)(?=\.)', name, re.I)
    if not quantization:raise ValueError('quantization identity unavailable')
    files = []
    for value in shlex.split(props['FragmentPath']) + shlex.split(props['DropInPaths']):
        # Fingerprint effective settings, not comments/private file formatting.
        # Stat still proves that a legacy configuration predates its execution.
        path = Path(value);info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise ValueError('untrusted unit source')
        changed.extend((info.st_mtime, info.st_ctime));files.append(str(path))
    env_files = []
    remaining = props['EnvironmentFiles']
    for match in re.finditer(r'(.*?) \(ignore_errors=(yes|no)\)(?: |$)', remaining):
        path = Path(match[1].strip())
        if not path.is_absolute():raise ValueError('ambiguous environment file')
        try:
            data = control._root_file(path);info = path.stat();changed.extend((info.st_mtime, info.st_ctime))
            env_files.append((str(path), hashlib.sha256(data).hexdigest()))
        except FileNotFoundError:
            if match[2] != 'yes':raise
            env_files.append((str(path), None))
    if remaining and not env_files:raise ValueError('environment file list unavailable')
    driver = control._fixed_command([control.NVIDIA_SMI, '--id', str(policy.gpu_index),
        '--query-gpu=driver_version', '--format=csv,noheader,nounits'])
    if not driver or len(driver) > 100:raise ValueError('GPU driver identity unavailable')
    static = dict(exec=execution['stable'], environment=props['Environment'], environment_files=env_files,
                  files=files, user=props['User'], group=props['Group'], directory=props['WorkingDirectory'],
                  root=props['RootDirectory'], image=props['RootImage'], memory_max=props['MemoryMax'], memlock=props['LimitMEMLOCK'],
                  settings=control.context_env(snapshot), policy=policy.digest, weights=weight, backend=executable,
                  gpu=dict(index=policy.gpu_index, uuid=gpu['_uuid'], name=gpu['name'], total_mib=gpu['total_mib'], driver=driver))
    return dict(version=SAMPLE_VERSION, fingerprint=digest(static), model_id=policy.model_id,
                weights_name=name, quantization=quantization[1].upper(), weights_identity=digest(weight),
                backend_identity=digest(executable), gpu=static['gpu'], configuration=static['settings'],
                _latest_change=max(changed), _execution=execution, _legacy_display=raw)


def public_identity(identity):
    return {key:value for key,value in identity.items() if not key.startswith('_')}


def make_sample(measured, sampled_at, saved, policy, identity, source='live_nvml'):
    return dict(model_mib=measured, sampled_at=sampled_at, configuration=saved, policy=policy.digest,
                gpu_index=policy.gpu_index, unit_profile=identity['fingerprint'], profile_version=SAMPLE_VERSION,
                identity=public_identity(identity), source=source)


def legacy_sample(state, saved, policy, identity, snapshot):
    sample = (state or {}).get('sample') or {};measurement = sample.get('model_mib');stamp = sample.get('sampled_at')
    execution = identity.get('_execution') or {};after = (state or {}).get('gpu_after_stop') or {};gpu = identity['gpu']
    if (sample.get('configuration') != saved or sample.get('policy') != policy.digest
            or sample.get('gpu_index') != policy.gpu_index or type(measurement) is not int or measurement <= 0
            or not isinstance(stamp, (int, float)) or state.get('model_name') != identity['weights_name']
            or any(after.get(k) != gpu.get(k) for k in ('index', 'name', 'total_mib'))
            or execution.get('start_epoch') is None or execution.get('stop_epoch') is None
            or not execution['start_epoch'] <= stamp <= execution['stop_epoch'] + 1
            or identity['_latest_change'] > execution['start_epoch'] + 1):
        return None
    # v1's protected hash can be verified using this SAME execution's retained
    # bookkeeping. Do not guess a PID/time or accept a manually supplied MiB.
    before = re.sub(r'stop_time=\[[^]]*\]', 'stop_time=[n/a]', identity['_legacy_display'])
    before = re.sub(r'code=[^; }]+', 'code=(null)', before)
    before = re.sub(r'status=[^; }]+', 'status=0/0', before)
    if hashlib.sha256(before.encode() + snapshot).hexdigest() != sample.get('unit_profile'):return None
    recovered = make_sample(measurement, stamp, saved, policy, identity, 'verified_legacy_stop_record')
    recovered.update(legacy_unit_profile=sample['unit_profile'], recovered_at=time())
    return recovered


def startup_assessment(host, state, saved, policy, reclaim_mib=0):
    """The sole matching/estimate function used by inspect AND actual start."""
    gpu = host.gpu()
    result = dict(estimate=None, sample_status='unverified', reason='inspection_unavailable')
    if not gpu or type(gpu.get('free_mib')) is not int:return dict(result, reason='gpu_unknown')
    try:
        identity = host.configuration_identity(gpu)
        candidate = (state or {}).get('sample') or {}
        source = 'state'
        if not candidate:
            candidate = host.sample_record() or {};source = 'root_sample_record'
        if not candidate:return dict(result, sample_status='missing', reason='sample_missing')
        if not isinstance(candidate, dict):return dict(result, sample_status='mismatch', reason='profile_mismatch')
        if candidate.get('profile_version') != SAMPLE_VERSION:
            candidate = legacy_sample(state, saved, policy, identity, host.snapshot())
            source = 'verified_legacy_stop_record'
        budget = estimate(candidate or {}, saved, policy, identity)
        if not budget:return dict(result, sample_status='mismatch', reason='profile_mismatch')
        budget['source'] = source
        reason = '' if gpu['free_mib'] + reclaim_mib >= budget['required_mib'] else 'gpu_insufficient'
        return dict(estimate=budget, sample_status='matched', reason=reason, sample=candidate,
                    available_mib=gpu['free_mib'], identity=identity)
    except (OSError, ValueError, KeyError, TypeError):return result


def unit_status(control):
    fields = ('ActiveState', 'SubState', 'MainPID', 'ExecMainStartTimestamp', 'Result')
    try:
        raw = control._fixed_command([control.SYSTEMCTL, 'show', '--property=' + ','.join(fields), '--', control.MODEL_UNIT])
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if not set(fields) <= values.keys():
            return {}
        return values
    except (OSError, ValueError, control.subprocess.SubprocessError):
        return {}


def describe(ctl, runtime, policy, saved):
    from . import model_control as control
    state = read_state(control._root_file)
    unit = unit_status(control) if policy else {}
    active = unit.get('ActiveState')
    phase = (state or {}).get('phase')
    if active == 'deactivating':
        phase = 'stopping'
    elif active == 'activating':
        phase = 'starting'
    elif phase not in {'starting', 'stopping', 'start_failed', 'error', 'unknown'}:
        phase = ('running' if active == 'active' and runtime['status'] == 'running'
                 and unit.get('MainPID') == str(runtime.get('_pid')) and not held(state) else
                 'stopped' if active == 'inactive' and not runtime.get('_pid') else
                 'start_failed' if active == 'failed' else 'error' if active else 'unknown')
    gpu = runtime.get('gpu') or {}
    usage = gpu.get('model_process_mib') or gpu.get('_model_mib')
    pid = runtime.get('_pid')
    inspection = ctl.service_inspection(policy) if policy and saved else {}
    budget = inspection.get('estimate')
    reason = (state or {}).get('reason', '')
    detail = MESSAGES.get(reason, '') or {
        'unknown': '无法完整读取固定模型服务状态，尚未核验。',
        'error': 'systemd、模型PID或API健康状态不一致；未开放服务控制，请核查后刷新。',
        'start_failed': MESSAGES['start_failed'],
        'starting': '正在加载模型并核验实际配置，推理入口尚未开放。',
        'stopping': '正在检查空闲并停止固定模型服务，暂不接受新任务。',
    }.get(phase, '')
    if phase in {'stopped', 'start_failed'}:
        current_reason = inspection.get('reason')
        if current_reason:detail = MESSAGES.get(current_reason, MESSAGES['inspection_unavailable'])
        elif budget:detail = '匹配的历史显存样本已核验，启动仍需当前显存足够及管理员确认。'
    return dict(state=phase, state_label=LABELS[phase], backend='llama.cpp', gpu_index=policy.gpu_index if policy else ctl.gpu_index,
                model_name=(state or {}).get('model_name') or runtime.get('model_name'),
                api_available=runtime['status'] == 'running' and phase == 'running',
                pid=pid, model_process_mib=usage if pid else None, estimated_release_mib=usage if pid else None,
                last_started_at=unit.get('ExecMainStartTimestamp') or (state or {}).get('last_started_at'),
                observed_released_mib=(state or {}).get('observed_released_mib'),
                checked_at=time(), reason=reason, detail=detail,
                startup_estimate=budget, gpu_free_mib=gpu.get('free_mib'),
                sample_status=inspection.get('sample_status', 'unverified'),
                available=bool(policy), generation=(state or {}).get('generation', 0),
                can_start=bool(policy and phase in {'stopped', 'start_failed'}),
                can_stop=bool(policy and phase == 'running'), can_restart=bool(policy and phase == 'running'))


def audit(ctl, actor, operation, outcome, reason=''):
    with closing(sqlite3.connect(ctl.db)) as db, db:
        db.execute('INSERT INTO model_service_audit(occurred,actor,operation,outcome,reason) VALUES(?,?,?,?,?)',
                   (time(), actor, operation, outcome, reason))


def preview(ctl, actor, operation):
    from . import model_control as control
    if not isinstance(operation, str) or operation not in {'start', 'stop', 'restart'}:
        raise control.ModelControlError('无效的模型服务操作。', 400)
    policy = ctl._policy()
    model = ctl.status()
    service = model['model_service']
    if not service['can_' + operation] or ctl._maintenance:
        raise control.ModelControlError('当前服务状态不允许此操作，请刷新。', 409)
    ticket = control.secrets.token_urlsafe(32)
    payload = dict(operation=operation, policy=policy.digest, version=model['configuration_version'],
                   expected_generation=service['generation'])
    with closing(sqlite3.connect(ctl.db)) as db, db:
        db.execute('DELETE FROM model_confirmations WHERE actor=? OR expires<=?', (actor, time()))
        db.execute('INSERT INTO model_confirmations VALUES(?,?,?,?)',
                   (ctl._digest(ticket), actor, time() + control.CONFIRM_SECONDS, json.dumps(payload)))
    return dict(confirmation=ticket, operation=operation, service=service, running_tasks=ctl.in_flight,
                expires_in=control.CONFIRM_SECONDS,
                impact='维护期间拒绝新任务和新模型请求；存在活动任务则拒绝操作。停止后保持离线，Gateway及账号额度服务继续运行。')


def apply(ctl, actor, confirmation):
    from . import model_control as control
    with ctl._gate:
        if ctl._maintenance:
            raise control.ModelControlError('已有模型配置或服务控制事务正在执行。', 409)
        ctl._maintenance = True
    operation = None
    try:
        payload = ctl._consume(actor, confirmation)
        operation = payload.get('operation')
        if operation not in {'start', 'stop', 'restart'}:
            raise control.ModelControlError('确认票据不属于服务控制操作。', 409)
        audit(ctl, actor, operation, 'requested')
        if ctl.in_flight:
            raise control.ModelControlError(MESSAGES['busy'], 409)
        policy = ctl._policy()
        if (payload['policy'] != policy.digest or payload['version'] != ctl._version(ctl._runtime(policy))
                or control.ROLLBACK_PENDING.exists() or control.DEPLOYMENT_HOLD.exists()):
            raise control.ModelControlError(MESSAGES['state_changed'], 409)
        result = ctl._invoke(dict(operation=operation, policy=policy.digest,
                                  expected_generation=payload['expected_generation'], operation_id=ctl._digest(confirmation)))
        audit(ctl, actor, operation, result['status'], result.get('reason', ''))
        if result['status'] != 'applied':
            message = MESSAGES.get(result.get('reason'), MESSAGES['unknown'])
            if result.get('reason') == 'gpu_insufficient':
                current = ctl.status()['model_service']
                message += f" 当前可用 {current.get('gpu_free_mib')} MiB；估算需求 {(current.get('startup_estimate') or {}).get('required_mib')} MiB（含余量）。"
            raise control.ModelControlError(message, 503)
        model = ctl.status()
        expected = 'stopped' if operation == 'stop' else 'running'
        if model['model_service']['state'] != expected:
            raise control.ModelControlError('服务控制返回后真实状态未匹配，请刷新；入口保护保持。', 503)
        return dict(status='applied', operation=operation, model=model)
    except control.ModelControlError:
        if operation:
            audit(ctl, actor, operation, 'refused')
        raise
    finally:
        with ctl._gate:
            ctl._maintenance = False
