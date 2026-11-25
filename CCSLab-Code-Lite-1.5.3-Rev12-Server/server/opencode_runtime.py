"""Read-only v2 runtime verification for the operator-enrolled OpenCode file.

No process is started/stopped and no runtime mutation is sent here. v2.3.0's
location.reload cancels pending permissions/forms and has no atomic idle-only
precondition, so an interactive instance must be reloaded by its operator.
"""
from base64 import b64encode
import json
import os
from pathlib import Path
import time
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


CONFIG = Path('/home/server/.config/opencode/opencode.json')
BINARY = Path('/home/server/.opencode/bin/opencode')


class VerificationError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise VerificationError('运行接口发生重定向，未跟随。')


class OpenCodeRuntime:
    def __init__(self, config=CONFIG, *, binary=BINARY, proc=Path('/proc')):
        self.config, self.binary, self.proc = Path(config), Path(binary), Path(proc)

    def _instances(self):
        uid = self.config.stat().st_uid
        if uid != os.geteuid():
            raise VerificationError('Gateway 无权核对独立 OpenCode 的进程身份。')
        rows = []
        for path in self.proc.iterdir():
            if not path.name.isdigit():
                continue
            try:
                if path.stat().st_uid != uid:
                    continue
                # Same-UID sshd/systemd helpers can be non-dumpable. Their exe
                # denial says nothing about OpenCode access. Identify candidates
                # first, but never ignore an unreadable possible OpenCode.
                comm = (path / 'comm').read_text().strip()
                args = (path / 'cmdline').read_bytes().decode('utf-8').strip('\0').split('\0')
                candidate = comm in {'opencode', 'bun'} or Path(args[0]).name == 'opencode'
                try:
                    executable = (path / 'exe').readlink()
                except PermissionError:
                    if candidate:
                        raise VerificationError(f'无权读取 OpenCode 候选进程 PID {path.name} 的 exe，尚未核验。')
                    continue
                if str(executable) == str(self.binary) + ' (deleted)':
                    raise VerificationError('旧独立 OpenCode 可执行文件已被替换，但实例仍在运行，尚未核验。')
                if executable != self.binary:
                    continue
                stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                if stat[0] == 'Z':
                    continue
                command = next((arg for arg in args[1:] if not arg.startswith('-')), '')
                # CLI metadata calls/attached frontends are not additional servers.
                if command in {'api', 'reload', 'service', 'models', 'debug', 'session', 'stats', 'auth', 'upgrade'}:
                    continue
                rows.append({'pid': int(path.name), 'ppid': int(stat[1]), 'start': stat[19],
                             'path': path, 'args': args, 'server': 'serve' in args})
            except (FileNotFoundError, ProcessLookupError):
                continue
            except VerificationError:
                raise
            except (OSError, UnicodeError, ValueError, IndexError) as error:
                raise VerificationError(f'进程 PID {path.name} 的身份信息不可完整读取，不能将未知实例当作未启动。') from error
        parents = {row['ppid'] for row in rows}
        return [row for row in rows if row['server'] or row['pid'] not in parents]

    def _identity(self, row):
        path = row['path']
        stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        if (stat[19] != row['start'] or (path / 'exe').readlink() != self.binary
                or path.stat().st_uid != self.config.stat().st_uid):
            raise VerificationError('实例已退出或 PID 已变化，请刷新重试。')

    def _connection(self, row):
        # v2.3.0 interactive frontends spawn `serve --stdio` children which also
        # own an authenticated HTTP socket. Kernel ownership, not CLI flags,
        # determines whether the endpoint is safe to inspect.
        if (row['path'] / 'ns/net').readlink() != (self.proc / 'self/ns/net').readlink():
            raise VerificationError('实例处于不同网络命名空间，未向其他命名空间的同号端口发送认证信息。')
        env_raw = (row['path'] / 'environ').read_bytes()
        if len(env_raw) > 1024 * 1024:
            raise VerificationError('实例环境过大，未读取凭据。')
        env = dict(item.split(b'=', 1) for item in env_raw.split(b'\0') if b'=' in item)
        configured = env.get(b'OPENCODE_CONFIG')
        if configured:
            managed = Path(os.fsdecode(configured)) == self.config
        else:
            home = Path(os.fsdecode(env.get(b'HOME', b'/nonexistent')))
            base = Path(os.fsdecode(env.get(b'XDG_CONFIG_HOME', os.fsencode(home / '.config'))))
            managed = base / 'opencode/opencode.json' == self.config
        if not managed:
            raise VerificationError('实例未使用纳管配置来源，未将其他 OpenCode 纳入同步成功范围。')
        password = env.get(b'OPENCODE_PASSWORD') or env.get(b'OPENCODE_SERVER_PASSWORD')
        if not password or len(password) > 8192:
            raise VerificationError('实例认证信息不可取得；请在本机安全环境配置运行接口密码，不要发送密码。')
        username = b'opencode'  # v2.3.0 ServerProcess uses this fixed Basic user.
        # Accept only an IPv4 loopback LISTEN socket actually owned by this PID.
        inodes = set()
        for fd in (row['path'] / 'fd').iterdir():
            try:
                link = str(fd.readlink())
                if link.startswith('socket:['):
                    inodes.add(link[8:-1])
            except FileNotFoundError:
                continue
        ports = []
        for line in (row['path'] / 'net/tcp').read_text().splitlines()[1:]:
            fields = line.split()
            address, port = fields[1].split(':')
            if fields[3] == '0A' and fields[9] in inodes and address == '0100007F':
                ports.append(int(port, 16))
        if len(ports) != 1:
            raise VerificationError('未发现唯一且属于该 PID 的 127.0.0.1 监听接口；未探测任意端口。')
        return ports[0], 'Basic ' + b64encode(username + b':' + password).decode('ascii')

    def _get(self, port, authorization, route, timeout):
        opener = build_opener(ProxyHandler({}), NoRedirect())
        request = Request(f'http://127.0.0.1:{port}{route}', headers={'Authorization': authorization})
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise VerificationError('运行接口响应超过上限。')
        value = json.loads(raw)
        return value

    @staticmethod
    def _locations(value):
        if not isinstance(value, list) or not value or len(value) > 64:
            raise VerificationError('无法完整枚举运行实例的加载位置，尚未核验。')
        directories = []
        for item in value:
            directory = item.get('directory') if isinstance(item, dict) else None
            if not isinstance(directory, str) or not directory.startswith('/') or len(directory) > 4096:
                raise VerificationError('运行加载位置格式无效，尚未核验。')
            if directory in directories:
                raise VerificationError('运行加载位置重复，无法完整核验。')
            directories.append(directory)
        return sorted(directories)

    def _configuration(self, value):
        # Config.Entry[] is ordered from lowest to highest priority. Directory
        # entries are discovery roots, NOT loaded configurations. Inspect every
        # document, retaining only context numbers (never options/API keys).
        if not isinstance(value, list) or not value or len(value) > 256:
            raise VerificationError('/api/config 未返回有效的配置来源列表。')
        observed, managed, sources = None, False, []
        for index, entry in enumerate(value):
            if not isinstance(entry, dict):
                raise VerificationError('运行配置来源格式无效。')
            if entry.get('type') == 'directory' and 'info' not in entry:
                continue
            info = entry.get('info')
            if entry.get('type') != 'document' or not isinstance(info, dict):
                raise VerificationError('运行配置文档格式无效。')
            providers = info.get('providers', {})
            if not isinstance(providers, dict):
                raise VerificationError('运行配置 providers 格式无效。')
            provider = providers.get('ccslab', {})
            if not isinstance(provider, dict) or not isinstance(provider.get('models', {}), dict):
                raise VerificationError('运行配置 ccslab 模型格式无效。')
            model = provider.get('models', {}).get('lab-general', {})
            if not isinstance(model, dict) or not isinstance(model.get('limit', {}), dict):
                raise VerificationError('运行配置 lab-general limit 格式无效。')
            limit = model.get('limit', {})
            if 'context' not in limit:
                continue
            loaded = limit['context']
            if type(loaded) is not int or loaded <= 0:
                raise VerificationError('运行配置包含无效的上下文值。')
            is_managed = entry.get('path') == str(self.config)
            managed |= is_managed
            observed = loaded
            sources.append(dict(source_index=index, context=loaded, managed=is_managed))
        if not managed or observed is None:
            raise VerificationError('加载位置未返回纳管文件的 ccslab/lab-general 上下文，尚未核验。')
        return observed, sources

    def status(self, context):
        result = {'runtime_checked_at': time.time(), 'instances': [], 'effective_context_limit': None,
                  'runtime_scope': 'all_loaded_locations', 'automatic_reload': False}
        try:
            rows = self._instances()
        except (OSError, ValueError) as error:
            return dict(result, status='unverified', detail=str(error) if isinstance(error, VerificationError)
                        else '独立实例检测不可用，未将检测失败当作未启动。')
        if not rows:
            return dict(result, status='ready', instance_count=0, detail='配置已就绪，待启动。')
        if len(rows) > 32:
            return dict(result, status='unverified', instance_count=len(rows), detail='实例超过本次核验上限，未宣称全部同步。')
        deadline = time.monotonic() + 3
        for row in rows:
            item = {'pid': row['pid'], 'status': 'unverified', 'effective_context_limit': None}
            try:
                if time.monotonic() >= deadline:
                    raise VerificationError('本次运行核验时间已到，请刷新重试。')
                self._identity(row)
                port, auth = self._connection(row)
                def get(route):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VerificationError('本次运行核验时间已到，请刷新重试。')
                    return self._get(port, auth, route, min(.7, remaining))
                info = get('/api/info')
                if info.get('version') != '2.3.0' or info.get('pid') != row['pid']:
                    raise VerificationError('接口版本或 PID 与运行实例不一致。')
                directories = self._locations(get('/api/debug/location'))
                locations = []
                item['locations'] = locations
                for number, directory in enumerate(directories, 1):
                    query = '?' + urlencode({'location[directory]': directory})
                    configured, sources = self._configuration(get('/api/config' + query))
                    # Cross-check the provider's actually constructed model limits,
                    # rather than equating config documents with a loaded model.
                    models = get('/api/model' + query)['data']
                    if not isinstance(models, list) or not all(isinstance(model, dict) for model in models):
                        raise VerificationError('运行接口模型列表格式无效。')
                    selected = [m for m in models if m.get('providerID') == 'ccslab' and m.get('id') == 'lab-general']
                    loaded = selected[0]['limit']['context'] if len(selected) == 1 else None
                    if type(loaded) is not int or loaded <= 0:
                        raise VerificationError('接口未返回唯一有效的 lab-general 运行上下文。')
                    if configured != loaded:
                        raise VerificationError('运行配置文档与已加载模型值不一致，尚未完成可信核验。')
                    locations.append(dict(location_index=number, effective_context_limit=loaded,
                                          status='synced' if loaded == context else 'pending_reload', sources=sources))
                if self._locations(get('/api/debug/location')) != directories:
                    raise VerificationError('核验期间加载位置发生变化，请刷新重试。')
                self._identity(row)
                if self._connection(row) != (port, auth):
                    raise VerificationError('核验期间进程监听接口发生变化，请刷新重试。')
                observed = {location['effective_context_limit'] for location in locations}
                states = {location['status'] for location in locations}
                state = next(iter(states)) if len(states) == 1 else 'partial'
                item.update(effective_context_limit=observed.pop() if len(observed) == 1 else None,
                            location_count=len(locations), status=state,
                            detail=(f'全部 {len(locations)} 个加载位置的实际上下文已核验。' if state == 'synced' else
                                    '已核验加载位置中存在旧上下文；请在空闲时正常退出并重新启动。'))
            except VerificationError as error:
                item['detail'] = str(error)
            except HTTPError as error:
                item['detail'] = ('运行接口认证失败，未读取实际值。' if error.code in (401, 403) else
                                  f'运行接口返回 HTTP {error.code}，尚未核验；实例可能仍在启动。')
                error.close()
            except (URLError, TimeoutError):
                item['detail'] = '运行接口连接失败或读取超时；实例可能仍在启动，请刷新重试。'
            except FileNotFoundError:
                item['detail'] = '实例在核验期间退出，请刷新重新检测。'
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                item['detail'] = '运行接口响应格式无效或尚未就绪，未读取文件值冒充运行值。'
            except OSError:
                item['detail'] = '进程权限或本机连接不可用，尚未核验运行值。'
            result['instances'].append(item)
        try:
            after = self._instances()
            if {(r['pid'], r.get('start')) for r in after} != {(r['pid'], r.get('start')) for r in rows}:
                raise VerificationError('核验期间 OpenCode 实例退出或新增，请刷新重新检测。')
        except (OSError, ValueError) as error:
            return dict(result, status='unverified', instance_count=len(rows), detail=str(error)
                        if isinstance(error, VerificationError) else '无法重新核对实例清单，尚未核验。')
        states = [item['status'] for item in result['instances']]
        if all(state == 'synced' for state in states):
            status, actual = 'synced', context
        elif len(set(states)) > 1:
            status, actual = 'partial', None
        else:
            status, actual = states[0], None
        observed = {item['effective_context_limit'] for item in result['instances']}
        if set(states) <= {'synced', 'pending_reload'} and len(observed) == 1:
            actual = observed.pop()
        detail = '；'.join(f"PID {item['pid']}：{item['detail']}" for item in result['instances'])
        if status != 'synced':
            detail += ' v2.3.0 reload 会取消待审批/表单，且无原子空闲前置条件，Gateway 不自动调用。'
        return dict(result, status=status, instance_count=len(rows), effective_context_limit=actual, detail=detail)
