"""Fixed, recoverable robotd transaction, loaded by the verified root helper only.

No device request can choose a file, service, executable or release. The package
builder imports only the manifest/ELF functions; it never runs a transaction.
"""
import hashlib
import json
import math
import os
import re
import shutil
import socket
import stat
import tomllib
import types
import uuid

SCHEMA = 'microduck-native-update/1'
PAYLOAD_SCHEMA = 'microduck-native-payload/1'
REVIEWED_SHA256 = 'e5ff7fb429fd0dfb762eb4da1bcc5a26b48b6d2dfdae4bb4904213e37e022ed0'
REVIEWED_SIZE = 7275792
LEGACY_OLD_SHA256 = 'edf182edb7bf11aeeeedfdab98170ab2ec48a9ec18417f9b68c744b0a60c6d16'
LEGACY_TARGET_SHA256 = 'febf44e2e5d4e573f4513f25a1ae6480b976c3988dd3d5e7cdfcf84d2a2d5063'
LEGACY_STARTUP_SHA256 = '3dbbf1723c0315afc15fd9f28787c0cfc194622a67802adcd1f5aa397ea47422'
LEGACY_PUBLISHED_SHA256 = '725b74cd93660a312970a44879abe5b75e8becf108499a1b8798961e48ca5f00'
SUPPORTED_SHA256 = (LEGACY_OLD_SHA256, LEGACY_TARGET_SHA256, LEGACY_STARTUP_SHA256, LEGACY_PUBLISHED_SHA256)
PREDECESSOR_IPC_API = {LEGACY_OLD_SHA256: 40, LEGACY_TARGET_SHA256: 40, LEGACY_STARTUP_SHA256: 41,
                       LEGACY_PUBLISHED_SHA256: 42}
LEGACY_TRANSACTIONS = {(LEGACY_OLD_SHA256, LEGACY_TARGET_SHA256): '.45',
                       (LEGACY_OLD_SHA256, LEGACY_STARTUP_SHA256): '.46',
                       (LEGACY_TARGET_SHA256, LEGACY_STARTUP_SHA256): '.46',
                       **{(old, LEGACY_PUBLISHED_SHA256): '.47' for old in
                          (LEGACY_OLD_SHA256, LEGACY_TARGET_SHA256, LEGACY_STARTUP_SHA256)}}
SOURCE = 'eecac1878136d3188370a5f6460b7d0521cf845d'
BUILD = 'replica-imu-verify-control.48'
IPC_API = 43
TARGET = '/opt/robot/feetech-ft5-r5/robotd'
CONFIG = '/etc/robot/feetech-ft5/robotd.toml'
CALIB = '/etc/robot/feetech-ft5/hd1910-calibration.toml'
USER_FILES = (CONFIG, CONFIG.replace('.toml', '.saved.toml'), CALIB, CALIB.replace('.toml', '.saved.toml'))
ARGS = [TARGET, '--socket', '/run/robotd.sock', '--params', CONFIG, '--port', '/dev/ttyS2']
STATE = '/var/lib/microduck-helper/native-update'
JOURNAL = STATE + '/journal.json'
ACTIVE = STATE + '/active'
RESTORE_PERMIT = '/run/microduck-helper/native-update-restore'
GUARD = '/etc/systemd/system/robotd.service.d/zzzzzzz-microduck-native-update.conf'
MOTION_LOCK = '/run/microduck-updater/motion.lock'
SOFTWARE_MARKER = '/run/microduck-updater/installing'
PROTECTED = '/run/microduck-updater/protected-services.json'
CONTROLLERS = ('padd', 'gamesir-xboxd', 'duck-echo')
UNIT_PROPERTIES = ['LoadState', 'ActiveState', 'SubState', 'MainPID', 'InvocationID', 'ExecMainStartTimestampMonotonic']
PUBLIC = ('active', 'phase', 'transaction_id', 'available', 'installed_sha256', 'target_sha256', 'running_verified', 'mode', 'reason')
PHASES = frozenset(('prepared','guarding','guarded','replacing','replaced','verified_commissioning',
                   'restoring_normal','verified_motion','factory_handoff_pending','completed','rolled_back',
                   'recovering','old_restored','recovery_required'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def is_aarch64(data):
    return (len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01'
            and data[16:18] in (b'\x02\x00',b'\x03\x00') and data[18:20] == b'\xb7\x00')


def native_manifest(version, data):
    return {'schema': PAYLOAD_SCHEMA, 'architecture': 'aarch64', 'software_version': version,
            'source_revision': SOURCE, 'build': BUILD, 'official_version': '0.15.1',
            'model_api': 2, 'ipc_api': IPC_API, 'size': len(data), 'sha256': sha(data),
            'supported_installed_sha256': list(SUPPORTED_SHA256)}


def validate_payload(data, manifest, version):
    expected = native_manifest(version, data)
    if (not is_aarch64(data) or len(data) != REVIEWED_SIZE or sha(data) != REVIEWED_SHA256
            or manifest != expected):
        raise ValueError('native payload is not the reviewed AArch64 build or manifest')


def json_socket(path, body):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(path)
        connection.sendall(json.dumps(body).encode() + b'\n')
        with connection.makefile('rb') as stream:
            raw = stream.readline(8193)
            if len(raw) > 8192 or not raw.endswith(b'\n'):
                raise ValueError('readiness response exceeded bounds')
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError('invalid readiness response')
            return value


class Transaction:
    def __init__(self, helper, op, release):
        self.h, self.op, self.ctx, self.release = helper, op, op.ctx, release

    def refuse(self, reason, result='native_refused'):
        raise self.h.Stop(self.h.EXIT_STATE, result, reason)

    def read(self, path, limit=32*1024*1024):
        # Every parent must be a real root-owned directory, including backups.
        pieces = path.strip('/').split('/')
        for i in range(1, len(pieces)+1):
            st = os.lstat(self.ctx.p('/' + '/'.join(pieces[:i])))
            if ((i < len(pieces) and not stat.S_ISDIR(st.st_mode))
                    or (i == len(pieces) and not stat.S_ISREG(st.st_mode))
                    or (self.ctx.check_owner and (st.st_uid != 0 or st.st_mode & 0o022))):
                self.refuse('robotd binary/dir ownership or permissions unsafe', 'native_tampered')
        if st.st_size > limit: self.refuse('robotd binary exceeds size limit', 'native_tampered')
        with open(self.ctx.p(path), 'rb') as stream: return stream.read(limit+1)

    def present(self, path):
        return os.path.lexists(self.ctx.p(path))

    def sync_dir(self,path):
        if os.name!='posix':return
        descriptor=os.open(self.ctx.p(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)

    def write(self,path,data,mode):
        self.h.write_atomic(self.ctx,path,data,mode)
        # The general helper tolerates directory-fsync errors for legacy verbs;
        # this transaction must stop on a failed persistence boundary.
        self.sync_dir(os.path.dirname(path))

    def directory(self, path, mode=0o755):
        pieces=path.strip('/').split('/')
        for i in range(1,len(pieces)+1):
            name='/'+'/'.join(pieces[:i])
            try: st=os.lstat(self.ctx.p(name))
            except FileNotFoundError:
                os.mkdir(self.ctx.p(name),mode if i==len(pieces) else 0o755)
                self.sync_dir(os.path.dirname(name))
                st=os.lstat(self.ctx.p(name))
            if (not stat.S_ISDIR(st.st_mode) or (self.ctx.check_owner and (st.st_uid!=0 or st.st_mode&0o022))):
                self.refuse('Update dir ownership/permissions unsafe','native_tampered')

    def save(self, m, phase, **fields):
        m.update(phase=phase, **fields)
        self.write(JOURNAL, (json.dumps(m, ensure_ascii=False, allow_nan=False)+'\n').encode(), 0o644)
        self.op.emit('native', 'info', phase, {key:m.get(key) for key in PUBLIC if key in m})

    def load(self):
        if not self.present(JOURNAL): return None
        try:
            m = json.loads(self.read(JOURNAL, 65536))
            if (m.get('schema') != SCHEMA or type(m.get('active')) is not bool
                    or type(m.get('restore_pending',False)) is not bool
                    or type(m.get('cleanup_pending',False)) is not bool or m.get('phase') not in PHASES
                    or (m.get('reason') is not None and (not isinstance(m['reason'],str) or len(m['reason'])>300))
                    or (os.name=='posix' and m.get('binary_mode') not in (0o755,0o555))
                    or not re.fullmatch('[0-9a-f]{32}', m.get('transaction_id', ''))
                    or m.get('old_sha256') not in (*SUPPORTED_SHA256, REVIEWED_SHA256)
                    or m.get('target_sha256') not in (REVIEWED_SHA256, LEGACY_TARGET_SHA256,
                                                       LEGACY_STARTUP_SHA256, LEGACY_PUBLISHED_SHA256)
                    or m.get('mode') not in ('motion', 'startup', 'commissioning')
                    or set(m.get('controllers', {})) != set(CONTROLLERS)
                    or any(type(v) is not bool for v in m['controllers'].values())
                    or ('controller_states' in m and (set(m['controller_states'])!=set(CONTROLLERS)
                        or any(state not in ('active','inactive','failed','absent') for state in m['controller_states'].values())
                        or any(m['controllers'][u]!=(state=='active') for u,state in m['controller_states'].items())))
                    or [row.get('path') for row in m.get('files', [])] != list(USER_FILES)):
                raise ValueError('journal schema mismatch')
            if m['target_sha256'] != REVIEWED_SHA256:
                self.legacy_terminal(m)
            if m['active'] or m.get('restore_pending') or m.get('cleanup_pending') or self.has_exclusion():
                directory = STATE + '/' + m['transaction_id']
                if sha(self.read(directory+'/robotd')) != m['old_sha256']:
                    raise ValueError('binary backup changed')
                for i, row in enumerate(m['files']):
                    if type(row.get('existed')) is not bool or (not row['existed'] and row['path']!=USER_FILES[3]):
                        raise ValueError('invalid optional backup entry')
                    if row['existed'] and sha(self.read(directory+'/user-'+str(i))) != row.get('sha256'):
                        raise ValueError('user file backup changed')
            return m
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self.refuse('robotd update record unreadable; preserving state', 'native_tampered')

    def legacy_terminal(self, m):
        # Only actual known predecessor transactions may become history. Their
        # in-flight recovery belongs to the original signed updater and payload.
        build = LEGACY_TRANSACTIONS.get((m['old_sha256'], m['target_sha256']))
        if build is None:
            raise ValueError('unknown predecessor transaction')
        if (m['active'] or m.get('restore_pending') is not False or m.get('cleanup_pending') is not False
                or m['phase'] not in ('completed', 'rolled_back') or self.has_exclusion()):
            self.refuse('Previous '+build+' robotd update incomplete: restore with its signed updater first',
                        'native_recovery_required')
        installed = m['target_sha256'] if m['phase'] == 'completed' else m['old_sha256']
        if (m.get('completion') != m['phase'] or m.get('installed_sha256') != installed
                or m.get('commit_sha256') != installed or m.get('running_verified') is not True
                or sha(self.read(TARGET)) != installed):
            raise ValueError('predecessor terminal identity mismatch')
        for row in m['files']:
            if (type(row.get('existed')) is not bool
                    or (row['existed'] and not re.fullmatch('[0-9a-f]{64}', row.get('sha256', '')))
                    or (not row['existed'] and (row['path'] != USER_FILES[3] or row.get('sha256') is not None))):
                raise ValueError('invalid predecessor file record')

    def archive_legacy(self, m):
        raw = self.read(JOURNAL, 65536)
        if json.loads(raw) != m: self.refuse('Previous update record changed', 'native_tampered')
        directory = STATE + '/' + m['transaction_id']
        self.directory(directory, 0o700)
        history = directory + '/journal.json'
        if self.present(history):
            if self.read(history, 65536) != raw: self.refuse('Previous update history changed', 'native_tampered')
            self.sync_dir(directory)
        else:
            self.write(history, raw, 0o600)

    def payload(self):
        base = self.release + '/studio/deploy/native/'
        if not self.present(base+'robotd') and not self.present(base+'manifest.json'): return None
        data = self.read(base+'robotd')
        manifest = json.loads(self.read(base+'manifest.json', 8192))
        validate_payload(data, manifest, self.release.rsplit('/',1)[-1])
        return data, manifest

    def args(self):
        args = self.h.imu_redo_arguments(self.h.exec_start(self.op))
        if args not in (ARGS, ARGS+['--commissioning']): self.refuse('ExecStart does not match supported fixed install')
        dropins = self.h.show(self.op, 'robotd', ['DropInPaths']).get('DropInPaths', '').split()
        allowed = {'/etc/systemd/system/robotd.service.d/zzzz-r17-0151.conf', self.h.DROPIN, self.h.COMMISSIONING_DROPIN, GUARD}
        if not dropins or set(dropins)-allowed: self.refuse('Unsupported robotd service drop-in overrides')
        for path in dropins: self.read(path, 65536)
        return args

    def unit(self, name):
        return self.h.show(self.op, name, UNIT_PROPERTIES)

    def process(self, expected, args):
        before = self.unit('robotd')
        if before.get('ActiveState') != 'active' or before.get('SubState') != 'running':
            self.refuse('robotd process not stably active')
        pid = before.get('MainPID', '')
        if not pid.isdigit() or int(pid) <= 0 or not before.get('InvocationID'):
            self.refuse('robotd PID / invocation ID unreadable')
        path = self.ctx.p('/proc/'+pid)
        with open(path+'/stat', encoding='utf-8') as stream: start = stream.read().rsplit(')',1)[1].split()[19]
        with open(path+'/cmdline', 'rb') as stream: actual_args = stream.read(8193).rstrip(b'\0').split(b'\0')
        if actual_args != [arg.encode() for arg in args]: self.refuse('Running robotd args do not match fixed ExecStart')
        with open(path+'/exe', 'rb') as stream:
            st = os.fstat(stream.fileno()); actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        target = os.stat(self.ctx.p(TARGET), follow_symlinks=False)
        if ((st.st_dev,st.st_ino) != (target.st_dev,target.st_ino) or actual != expected
                or sha(self.read(TARGET)) != expected or self.unit('robotd') != before):
            self.refuse('Running robotd binary digest / invocation mismatch')
        return {'unit':before, 'start_ticks':start, 'boot_id':self.ctx.boot_id(), 'sha256':actual}

    def unchanged(self, m):
        for row in m['files']:
            if (self.present(row['path']) != row['existed']
                    or (row['existed'] and sha(self.read(row['path'])) != row['sha256'])):
                self.refuse('User params or calibration modified; refusing to overwrite', 'native_user_files_changed')

    def config(self):
        for path in USER_FILES:
            if path==USER_FILES[3] and not self.present(path): continue
            raw = self.read(path, 2*1024*1024)
            data = tomllib.loads(raw.decode('utf-8'))
            if path in USER_FILES[:2]:
                if (data.get('feetech_calibration') != CALIB or (data.get('bus') or {}).get('protocol') != 'feetech'
                        or (data.get('trunk_imu') or {}).get('source','bus') not in ('bus','bno08x','lsm6dsv16x')):
                    self.refuse('Current config is not a supported Feetech install')

    def readiness(self):
        try:
            studio = json_socket(self.ctx.p('/run/microduck-studio/update.sock'), {'op':'readiness'})
            audio = json_socket(self.ctx.p('/run/duck-echo/control.sock'), {'method':'status'})
            if studio.get('ready') is not True: self.refuse('Studio control or recording active; stop first')
            if (audio.get('ok') is not True or audio.get('status',{}).get('listening') is not False
                    or audio.get('status',{}).get('music') is not False): self.refuse('Disable voice listening and music mode first')
        except (OSError, ValueError, TypeError, AttributeError): self.refuse('Cannot verify Studio / voice idle; aborting update')

    def numeric(self, v):
        return type(v) in (int,float) and math.isfinite(v)

    def gate(self, allow_startup=False):
        old, mode, lifecycle = None, None, None
        for index in range(2):
            if index: self.ctx.sleep(.3)
            bus = self.ctx.rpc('robot.busStatus')
            current = bus.get('mode'); phase = bus.get('phase')
            if mode is not None and current != mode: self.refuse('robotd mode changed during precheck')
            mode = current
            category = 'startup' if phase in ('configuring_bus','waiting_for_bus') else 'feedback'
            if lifecycle is not None and category!=lifecycle: self.refuse('Startup lifecycle changed during precheck; wait until stable')
            lifecycle = category
            if allow_startup and current in ('motion','commissioning') and phase in ('configuring_bus','waiting_for_bus'):
                if (any(bus.get(k) for k in ('policy_enabled','policy_motion_enabled','motion','home_recovery','hold'))
                        or self.unit('robotd').get('ActiveState') != 'active'):
                    self.refuse('Cannot confirm supported startup lifecycle')
                continue
            if (bus.get('torque_off_pending') or bus.get('home_recovery') or bus.get('motion')
                    or bus.get('hold') or bus.get('policy_enabled') is True or bus.get('policy_motion_enabled') is True):
                self.refuse('Support the duck and release torque first')
            if current == 'motion':
                native = bus.get('native') or {}; hd = native.get('hd1910') or {}
                if (phase!='control' or bus.get('policy_enabled') is not False or native.get('torque_state_confirmed') is not False
                        or hd.get('restore_pending') is not False or hd.get('temporary_gains_applied') is not False
                        or bus.get('fresh_sample') is not True or bus.get('all_joints_fresh') is not True):
                    self.refuse('robotd has not confirmed fresh 15-joint feedback and torque off')
                rows = native.get('joints',[])
                if any(r.get('feedback_fresh') is not True or not self.numeric(r.get('sample_age_ms')) or not 0<=r['sample_age_ms']<=250
                       or not self.numeric(r.get('velocity_rad_s')) or abs(r['velocity_rad_s'])>.08
                       or not self.numeric(r.get('position_rad')) for r in rows): self.refuse('Joints still moving or feedback stale')
                positions = {r.get('id'):r['position_rad'] for r in rows}
                cycle = bus.get('cycle')
                if not self.numeric(cycle) or cycle<=0 or (old and cycle<=old[1]):
                    self.refuse('robotd did not return two consecutive fresh snapshots')
            elif current == 'commissioning':
                sample = bus.get('last_sample') or {}
                if (phase!='ready' or bus.get('torque_state')!='off' or bus.get('power_pending')
                        or bus.get('policy_motion_enabled') is not False
                        or not self.numeric(bus.get('updated_at_us')) or not self.numeric(sample.get('at_us'))
                        or not 0<=bus['updated_at_us']-sample['at_us']<=250000):
                    self.refuse('Commissioning did not confirm fresh feedback and torque off')
                rows = sample.get('servos',[])
                if any(r.get('moving') is not False or r.get('speed_raw')!=0 or not self.numeric(r.get('position_raw')) for r in rows):
                    self.refuse('Commissioning joints still moving')
                positions = {r.get('id'):r['position_raw']*2*math.pi/4096 for r in rows}
                cycle = sample.get('cycle')
                if not self.numeric(cycle) or cycle<=0 or (old and cycle<=old[1]): self.refuse('Commissioning returned duplicate stale snapshot')
            else: self.refuse('robotd in unknown mode')
            if len(rows)!=15 or set(positions)!=self.h.JOINT_IDS: self.refuse('Incomplete 15-joint feedback')
            if old and any(abs(positions[k]-old[0][k])>.02 for k in positions): self.refuse('Joint motion detected')
            old = (positions, cycle)
        return 'startup' if allow_startup and old is None else mode

    def resources(self):
        health = self.ctx.rpc('robot.health'); battery = health.get('battery') or {}
        if (not self.numeric(battery.get('volts')) or battery['volts']<7.0
                or not self.numeric(battery.get('percent')) or battery['percent']<25): self.refuse('Low battery (<25%) or power state unconfirmed')
        if shutil.disk_usage(self.ctx.p('/var/lib')).free<512*1024*1024: self.refuse('Less than 512 MiB free disk space')

    def controller_state(self,unit):
        status=self.unit(unit)
        load,active,sub=(status.get(key) for key in ('LoadState','ActiveState','SubState'))
        if (load,active,sub)==('not-found','inactive','dead'):return 'absent'
        if load=='loaded' and (active,sub) in (('active','running'),('inactive','dead'),('failed','failed')):
            return active
        self.refuse('Control service in unknown/transitioning state: '+unit)

    def stop_controllers(self):
        states={unit:self.controller_state(unit) for unit in CONTROLLERS}
        for unit,state in states.items():
            if state=='active':
                if self.ctx.run(['systemctl','stop',unit])[0]: self.refuse('Control service failed to stop')
        self.assert_controllers_stopped()

    def assert_controllers_stopped(self):
        if any(self.controller_state(u)=='active' for u in CONTROLLERS): self.refuse('Control service started externally during transaction')

    def guard(self, m):
        return ('# Owned by microduck-helper robotd-update: '+m['transaction_id']+'\n[Service]\nExecStart=\nExecStart='
                +' '.join(self.h.imu_redo_quote(a) for a in ARGS+['--commissioning'])+'\n').encode()

    def controller_guard_paths(self):
        return ['/etc/systemd/system/'+u+'.service.d/zzzzzzz-microduck-native-update.conf' for u in CONTROLLERS]

    def has_exclusion(self):
        return any(self.present(p) for p in [ACTIVE,RESTORE_PERMIT,GUARD]+self.controller_guard_paths())

    def controller_guard(self, m):
        # Trigger conditions are ORed: a boot-local permit lets this verified
        # process restore recorded services while the persistent guard remains.
        # Reboot loses the permit and blocks controllers until recover rechecks.
        return ('# Owned by microduck-helper robotd-update: '+m['transaction_id']+'\n[Unit]\nConditionPathExists=|!'+ACTIVE
                +'\nConditionPathExists=|'+RESTORE_PERMIT+'\n').encode()

    def exclude_controller_starts(self,m):
        if self.present(RESTORE_PERMIT):
            if self.read(RESTORE_PERMIT,128)!=m['transaction_id'].encode():self.refuse('Controller restore permit does not belong to transaction')
            self.h.remove(self.ctx,RESTORE_PERMIT);self.sync_dir(os.path.dirname(RESTORE_PERMIT))
        if self.present(ACTIVE) and self.read(ACTIVE,128)!=m['transaction_id'].encode():
            self.refuse('Controller update guard marker changed','native_tampered')
        for path in self.controller_guard_paths():
            if self.present(path):
                if self.read(path,1024)!=self.controller_guard(m):self.refuse('Controller update guard file changed','native_tampered')
            else:
                self.directory(os.path.dirname(path))
                self.write(path,self.controller_guard(m),0o644)
        if not self.present(ACTIVE):self.write(ACTIVE,m['transaction_id'].encode(),0o644)
        if self.ctx.run(['systemctl','daemon-reload'])[0]:self.refuse('Controller update guard not applied')

    def remove_controller_exclusion(self,m):
        # Completion/pending is durable before any controller can start. On a
        # reboot recover first re-verifies the actual normal process and unload.
        if self.present(ACTIVE) and self.read(ACTIVE,128)!=m['transaction_id'].encode():self.refuse('Controller guard marker does not belong to transaction')
        for path in self.controller_guard_paths():
            if self.present(path) and self.read(path,1024)!=self.controller_guard(m):self.refuse('Controller guard file does not belong to transaction')
        self.h.remove(self.ctx,ACTIVE);self.sync_dir(STATE)
        for path in self.controller_guard_paths():
            self.h.remove(self.ctx,path);self.sync_dir(os.path.dirname(path))
        if self.ctx.run(['systemctl','daemon-reload'])[0]:self.refuse('Failed to remove controller guard')
        if self.present(RESTORE_PERMIT):
            if self.read(RESTORE_PERMIT,128)!=m['transaction_id'].encode():self.refuse('Controller restore permit does not belong to transaction')
            self.h.remove(self.ctx,RESTORE_PERMIT);self.sync_dir(os.path.dirname(RESTORE_PERMIT))

    def owned(self, m):
        if self.present(GUARD) and self.read(GUARD,65536)!=self.guard(m): self.refuse('Update guard file modified externally', 'native_tampered')
        self.args()
        if sha(self.read(TARGET)) not in (m['old_sha256'],m['target_sha256']): self.refuse('robotd binary replaced externally', 'native_tampered')

    def install_guard(self, m):
        self.owned(m)
        if not self.present(GUARD): self.write(GUARD,self.guard(m),0o644)
        if self.ctx.run(['systemctl','daemon-reload'])[0] or self.args()!=ARGS+['--commissioning']:
            self.refuse('Update guard ExecStart not applied')

    def stop(self):
        if self.ctx.run(['systemctl','stop','robotd'],timeout=20)[0]: self.refuse('Failed to stop robotd')
        st = self.unit('robotd')
        if st.get('ActiveState') not in ('inactive','failed'): self.refuse('robotd still running')

    def start_verify(self, m, expected, mode):
        self.unchanged(m); self.assert_controllers_stopped()
        if self.ctx.run(['systemctl','start','robotd'],timeout=20)[0]: self.refuse('Failed to start robotd')
        deadline = self.ctx.now()+40
        while True:
            try:
                self.assert_controllers_stopped()
                identity = self.process(expected, ARGS+(['--commissioning'] if mode=='commissioning' else []))
                hello = self.ctx.rpc('hello')
                # Rollback verifies each reviewed binary's original contract.
                expected_ipc = IPC_API if expected==REVIEWED_SHA256 else PREDECESSOR_IPC_API.get(expected)
                if expected_ipc is None: self.refuse('No reviewed IPC version for robotd digest')
                if hello.get('api_version')!=expected_ipc: self.refuse('robotd IPC version mismatch')
                if expected==REVIEWED_SHA256:
                    rc,out,_ = self.ctx.run([TARGET,'deployment-info'])
                    if rc or json.loads(out)!={'build':BUILD,'model_api':2,'official_version':'0.15.1'}:
                        self.refuse('robotd deployment-info mismatch')
                if self.gate()!=mode: self.refuse('robotd did not enter expected mode')
                self.assert_controllers_stopped()
                self.unchanged(m)
                if self.process(expected,ARGS+(['--commissioning'] if mode=='commissioning' else []))!=identity:
                    self.refuse('robotd process replaced during verification')
                self.save(m,'verified_'+mode,running_verified=True,verified_process=identity)
                return identity
            except (self.h.Stop,self.h.RpcError,OSError,ValueError):
                if self.ctx.now()>=deadline: raise
                self.ctx.sleep(.5)

    def restore_controllers(self, m):
        self.assert_controllers_stopped()
        identities = {}
        for unit, was in m['controllers'].items():
            if was:
                if self.ctx.run(['systemctl','start',unit])[0] or self.controller_state(unit)!='active':
                    self.refuse('Failed to restore control services')
                identities[unit] = self.unit(unit)
        return identities

    def finish_mode(self, m, expected):
        self.unchanged(m); self.owned(m);self.assert_controllers_stopped()
        if m['mode']=='motion':
            self.save(m,'restoring_normal')
            self.stop()
            self.h.remove(self.ctx,GUARD); self.sync_dir(os.path.dirname(GUARD))
            if self.ctx.run(['systemctl','daemon-reload'])[0] or self.args()!=ARGS: self.refuse('Failed to restore normal ExecStart')
            identity = self.start_verify(m,expected,'motion')
            return identity
        else:
            # The standard factory flow owns the next calibration. Keep voice and
            # pads stopped; startup gates also protect them following a reboot.
            if not self.present(self.h.COMMISSIONING_DROPIN):
                self.save(m,'factory_handoff_pending')
                self.write(self.h.COMMISSIONING_DROPIN,self.factory(m),0o644)
            elif (sha(self.read(self.h.COMMISSIONING_DROPIN,65536))!=m.get('factory_sha256')
                  and self.read(self.h.COMMISSIONING_DROPIN,65536)!=self.factory(m)):
                self.refuse('Factory commissioning file modified externally')
            self.h.remove(self.ctx,GUARD); self.sync_dir(os.path.dirname(GUARD))
            if self.ctx.run(['systemctl','daemon-reload'])[0] or self.args()!=ARGS+['--commissioning']:
                self.refuse('Factory commissioning handover failed')

    def complete(self,m,expected,phase,reason=None):
        # Keep the public busy guard through controller restoration, including
        # the narrow interval where active=false is already durable.
        self.save(m,phase,active=False,restore_pending=True,completion=phase,commit_sha256=expected,
                  cleanup_pending=True,running_verified=True,installed_sha256=expected,reason=reason)
        self.unchanged(m)
        identity=self.process(expected,ARGS+(['--commissioning'] if m['mode']!='motion' else []))
        self.directory(os.path.dirname(RESTORE_PERMIT))
        self.write(RESTORE_PERMIT,m['transaction_id'].encode(),0o600)
        controllers=self.restore_controllers(m) if m['mode']=='motion' else {}
        self.unchanged(m)
        if self.process(expected,ARGS+(['--commissioning'] if m['mode']!='motion' else []))!=identity:
            self.refuse('robotd process modified externally after restore')
        if m['mode']=='motion':
            current=json.loads(self.read(PROTECTED,65536)) if self.present(PROTECTED) else None
            allowed=[m['protected']]
            if isinstance(m.get('protected_next'),dict):allowed.append(m['protected_next'])
            last_boot=m.get('baseline_boot_id',m['original_process']['boot_id'])
            lost_on_reboot=current is None and bool(self.ctx.boot_id()) and self.ctx.boot_id()!=last_boot
            if current not in allowed and not lost_on_reboot:self.refuse('Updater service baseline modified externally')
            if 'padd' in controllers:
                if self.unit('padd')!=controllers['padd']:self.refuse('padd process changed externally')
                baseline={'robotd':identity['unit'],'padd':controllers['padd']}
                self.save(m,phase,protected_next=baseline,baseline_boot_id=self.ctx.boot_id())
                self.write(PROTECTED,json.dumps(baseline).encode(),0o600)
        self.save(m,phase,restore_pending=False)
        self.remove_controller_exclusion(m)
        self.save(m,phase,cleanup_pending=False)

    def factory(self, m):
        return ('# Owned by microduck-helper robotd-update factory handoff: '+m['transaction_id']+'\n[Service]\nExecStart=\nExecStart='
                +' '.join(self.h.imu_redo_quote(a) for a in ARGS+['--commissioning'])+'\n').encode()

    def protect_failure(self,m):
        """Attempt every fixed safety action, even when an earlier one fails.

        A controller may have passed its start condition before a restore
        timed out. Conditions do not stop it: protect and stop robotd first,
        then independently stop each controller. Unknown query results never
        skip a stop attempt, and errors forbid normal-mode restoration.
        """
        errors=[]
        def attempt(label,action):
            try:action()
            except Exception:errors.append(label)
        attempt('journal',lambda:self.save(m,'recovering',active=True,restore_pending=False,running_verified=False))
        attempt('controller_exclusion',lambda:self.exclude_controller_starts(m))
        attempt('commissioning_guard',lambda:self.install_guard(m))
        attempt('robotd_stop',self.stop)
        for unit in CONTROLLERS:
            try:state=self.controller_state(unit)
            except Exception:state=None
            if state!='absent':
                def stop_controller(unit=unit):
                    if self.ctx.run(['systemctl','stop',unit],timeout=20)[0]:self.refuse('Failed to stop control service: '+unit)
                attempt(unit+'_stop',stop_controller)
        for unit in CONTROLLERS:
            def confirm_controller(unit=unit):
                if self.controller_state(unit)=='active':self.refuse('Control service still running: '+unit)
            attempt(unit+'_terminal',confirm_controller)
        def confirm_robotd():
            if self.unit('robotd').get('ActiveState') not in ('inactive','failed'):self.refuse('robotd restarted unexpectedly while guarded')
        attempt('robotd_terminal',confirm_robotd)
        return errors

    def rollback(self, m):
        errors=self.protect_failure(m)
        if errors:self.refuse('Recovery required after guard/stop errors: '+','.join(errors),'native_recovery_required')
        self.owned(m);self.assert_controllers_stopped()
        # Never restore config/calibration from backups. A later user change
        # blocks normal mode and remains untouched even when binary restore works.
        old = self.read(STATE+'/'+m['transaction_id']+'/robotd')
        if sha(old)!=m['old_sha256']: self.refuse('Old robotd backup changed')
        self.write(TARGET,old,m['binary_mode'])
        self.save(m,'old_restored')
        self.start_verify(m,m['old_sha256'],'commissioning')
        self.finish_mode(m,m['old_sha256'])
        self.complete(m,m['old_sha256'],'rolled_back','Restored previous robotd')

    def status(self):
        m = self.load()
        data = {key:m.get(key) for key in PUBLIC if key in m} if m else {'active':False,'phase':'idle','running_verified':False}
        data['active']=data['active'] or bool(m and (m.get('restore_pending') or m.get('cleanup_pending'))) or self.has_exclusion()
        payload = self.payload()
        data['available'] = payload is not None
        data['target_sha256'] = REVIEWED_SHA256 if payload else None
        try:
            current = sha(self.read(TARGET)); data['installed_sha256'] = current
            self.process(current,self.args())
            data['running_verified'] = current in (*SUPPORTED_SHA256,REVIEWED_SHA256)
            if not data['running_verified']:data['reason']='Current robotd not in supported range for auto-install'
        except (OSError,ValueError,self.h.Stop):
            data['running_verified']=False
            data['reason']='robotd runtime identity / ExecStart unverified'
        return self.op.finish(0,'native_status',detail=data)

    def run(self, action):
        if action=='status': return self.status()
        self.op.take_lock('robotd update')
        extra = []
        try:
            for path in (self.h.SERVICE_LOCK,MOTION_LOCK):
                # The service-management lock is created on first use, like
                # existing helper verbs. The motion lock must already be the
                # controller's fixed lock; never substitute another lock.
                if path==self.h.SERVICE_LOCK and not self.present(path):self.directory(os.path.dirname(path))
                else:self.read_lock(path)
                fd = self.ctx.lock(path)
                if fd is None: raise self.h.Stop(4,'busy','Another update or control operation is active')
                extra.append(fd)
                if path==self.h.SERVICE_LOCK:self.read_lock(path)
            if self.present(SOFTWARE_MARKER): raise self.h.Stop(4,'busy','Studio / voice software update in progress')
            self.h.imu_redo_conflict(self.op)
            if self.present(self.h.FLAG) or self.present(self.h.MARKER): self.refuse('Servo bus still held')
            m = self.load()
            if action=='recover':
                if not m:
                    if self.has_exclusion():self.refuse('Guard present without record','native_tampered')
                    return self.op.finish(0,'native_recovered',detail={'active':False})
                pending=m.get('restore_pending') or m.get('cleanup_pending') or self.has_exclusion()
                if not m['active'] and not pending:
                    return self.op.finish(0,'native_recovered',detail={'active':False})
                try:
                    if not m['active'] and pending:
                        self.owned(m);self.exclude_controller_starts(m);self.stop_controllers()
                        expected=m['commit_sha256']
                        if expected not in (m['old_sha256'],m['target_sha256']):self.refuse('Committed robotd digest mismatch')
                        self.start_verify(m,expected,'motion' if m['mode']=='motion' else 'commissioning')
                        self.complete(m,expected,m['completion'],m.get('reason'))
                    else:self.rollback(m)
                    return self.op.finish(0,'native_recovered',detail={k:m.get(k) for k in PUBLIC})
                except Exception as error:
                    self.protect_failure(m)
                    self.save(m,'recovery_required',active=True,running_verified=False,reason=str(error)[:300])
                    return self.op.finish(5,'native_recovery_required',detail={k:m.get(k) for k in PUBLIC})
            if m and (m['active'] or m.get('restore_pending') or m.get('cleanup_pending')):
                self.refuse('Previous robotd update unrecovered', 'native_recovery_required')
            if self.has_exclusion():
                self.refuse('Unowned robotd update guard file present', 'native_tampered')
            payload = self.payload()
            if payload is None: self.refuse('No robotd update in this package', 'native_unavailable')
            self.readiness()  # Before our own persistent busy journal.
            self.config(); self.resources()
            args = self.args(); old = self.read(TARGET)
            if os.name=='posix' and stat.S_IMODE(os.lstat(self.ctx.p(TARGET)).st_mode) not in (0o755,0o555):
                self.refuse('Unexpected robotd binary permissions')
            old_hash = sha(old)
            if not is_aarch64(old) or old_hash not in (*SUPPORTED_SHA256,REVIEWED_SHA256): self.refuse('Current robotd is not a reviewed supported build')
            process = self.process(old_hash,args)
            if old_hash==REVIEWED_SHA256: return self.op.finish(0,'native_current',detail={'active':False,'running_verified':True})
            mode = self.gate(allow_startup=True)
            controller_states={unit:self.controller_state(unit) for unit in CONTROLLERS}
            factory = self.present(self.h.COMMISSIONING_DROPIN)
            if (args==ARGS+['--commissioning']) != factory: self.refuse('Commissioning boot state mismatches persistent mode file')
            protected = json.loads(self.read(PROTECTED,65536)) if self.present(PROTECTED) else None
            if protected is not None and protected!={'robotd':self.unit('robotd'),'padd':self.unit('padd')}:
                self.refuse('Recorded robotd / padd process changed')
            self.directory(STATE)
            ident = uuid.uuid4().hex; directory = STATE+'/'+ident
            self.directory(directory,0o700)
            self.write(directory+'/robotd',old,0o600)
            rows = []
            for i,path in enumerate(USER_FILES):
                present = self.present(path)
                raw = self.read(path) if present else None
                if present: self.write(directory+'/user-'+str(i),raw,0o600)
                rows.append({'path':path,'existed':present,'sha256':sha(raw) if present else None})
            if m and m['target_sha256'] != REVIEWED_SHA256:
                self.archive_legacy(m)
            m = {'schema':SCHEMA,'active':True,'transaction_id':ident,'mode':mode,
                 'available':True,'old_sha256':old_hash,'target_sha256':REVIEWED_SHA256,'installed_sha256':old_hash,
                 'running_verified':False,'binary_mode':stat.S_IMODE(os.lstat(self.ctx.p(TARGET)).st_mode),
                 'files':rows,'controllers':{u:state=='active' for u,state in controller_states.items()},'controller_states':controller_states,
                 'original_process':process,'protected':protected,
                 'factory_sha256':sha(self.read(self.h.COMMISSIONING_DROPIN,65536)) if factory else None}
            self.save(m,'prepared')
            try:
                self.exclude_controller_starts(m)
                self.stop_controllers()
                if self.gate(allow_startup=mode!='motion')!=mode: self.refuse('robotd mode changed at final check')
                self.assert_controllers_stopped()
                if self.process(old_hash,args)!=process: self.refuse('robotd process changed at final check')
                self.unchanged(m)
                self.save(m,'guarding')
                self.install_guard(m)
                self.save(m,'guarded')
                self.stop(); self.unchanged(m); self.assert_controllers_stopped()
                if sha(self.read(TARGET))!=old_hash: self.refuse('robotd binary changed before replacement')
                self.save(m,'replacing')
                self.write(TARGET,payload[0],m['binary_mode'])
                self.save(m,'replaced',installed_sha256=REVIEWED_SHA256)
                self.start_verify(m,REVIEWED_SHA256,'commissioning')
                self.finish_mode(m,REVIEWED_SHA256)
                self.complete(m,REVIEWED_SHA256,'completed')
                return self.op.finish(0,'native_installed',detail={k:m.get(k) for k in PUBLIC})
            except Exception as error:
                try: self.rollback(m)
                except Exception as rollback_error:
                    self.protect_failure(m)
                    self.save(m,'recovery_required',active=True,running_verified=False,reason=str(rollback_error)[:300])
                return self.op.finish(5,'native_failed',reason=str(error)[:300],detail={k:m.get(k) for k in PUBLIC})
        finally:
            for fd in reversed(extra): self.ctx.unlock(fd)
            self.op.release_lock()

    def read_lock(self,path):
        self.read(path,65536)


def handle(helper, op, args, release):
    # The helper injects this dependency only after compiling suite-hash-bound
    # bytes. Never import a managed module through sys.path in a root process.
    dependency=globals().get('CONTROLLER_BUNDLE')
    members=('padd','controller-manifest.json')
    bundle=any(os.path.lexists(op.ctx.p(release+'/studio/deploy/native/'+name)) for name in members)
    journal=any(os.path.lexists(op.ctx.p('/var/lib/microduck-helper/controller-update/'+name)) for name in ('journal.json','prepared.json'))
    # A pending v1 transaction always retains its exact original recover path.
    previous=Transaction(helper,op,release).load()
    legacy_pending=bool(previous and (previous['active'] or previous.get('restore_pending') or previous.get('cleanup_pending')))
    if (bundle or journal) and not legacy_pending:
        if dependency is None:raise helper.Stop(helper.EXIT_STATE,'native_untrusted_release','Companion update requires signed release verification')
        return dependency.handle(helper,op,args,release,types.SimpleNamespace(**globals()))
    if (set(args)-{'by','action','confirmed'} or args.get('by')!='Studio' or args.get('action') not in ('status','install','recover')
            or (args.get('action')!='status' and args.get('confirmed') is not True)
            or ('confirmed' in args and type(args['confirmed']) is not bool)):
        raise helper.Stop(2,'bad_args','robotd-update accepts only fixed Studio actions with explicit confirmation')
    op.by = 'Studio'
    return Transaction(helper,op,release).run(args['action'])
