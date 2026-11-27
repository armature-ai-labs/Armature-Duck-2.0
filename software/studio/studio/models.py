"""Studio user model library; runtime configuration is verified against robotd readback."""
import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

from .model_package import ACTION_NAME, PackageError, extract_action_package, extract_package, validate_policy
from .rpc import call_once
from .tune import sanitize_model_name

SCHEMA = 'microduck-studio-model-library/v1'
META = '.studio-model.json'
BASE = '/opt/robot/feetech-ft5-r5'
PREFLIGHT = BASE + '/robotd'
SLOTS = ('walk', 'sitstand', 'ground_pick', 'kick_left', 'kick_right', 'roulade', 'stand')
LABELS = dict(zip(SLOTS, ('Walk / Hold', 'Sit / Stand', 'Ground Pick', 'Left Kick', 'Right Kick', 'Roulade', 'Stand (Standalone)')))
SKILLS = {'sitstand': 'sit_toggle', 'ground_pick': 'ground_pick', 'kick_left': 'kick_left',
          'kick_right': 'kick_right', 'roulade': 'roulade'}
DEFAULT_KEYS = {'a': 'ground_pick', 'x': 'roulade', 'lb': 'kick_left', 'rb': 'kick_right', 'dpad_down': 'sit_toggle'}
BUTTONS = {'a': 'A', 'x': 'X', 'lb': 'LB', 'rb': 'RB', 'dpad_down': 'D-Pad Down'}
JOINTS = ('left_hip_yaw,left_hip_roll,left_hip_pitch,left_knee,left_ankle,neck_pitch,'
          'head_pitch,head_yaw,head_roll,right_hip_yaw,right_hip_roll,right_hip_pitch,right_knee,right_ankle').split(',')
# 'More Actions': robotd supports at most 30 actions; leave 2 slots for factory/manual entries.
MAX_ACTIONS = 28
# Reserved names that cannot be used for custom actions.
RESERVED = frozenset(SLOTS) | frozenset(SKILLS.values()) | frozenset(DEFAULT_KEYS.values()) | {
    'stand_test', 'roller_stand', 'swizzle', 'crouch_roller', 'spin'}
READBACK_TIMEOUT = 25.0
READBACK_POLL = 0.2


class ModelError(ValueError):
    def __init__(self, message, *, code=None, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _json(path, limit=8 << 20):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('File is a symlink or exceeds size limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def _atomic_json(path, value):
    """Write to a temp file in the same directory, fsync, and atomically replace."""
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            dfd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _signature(contract):
    """HOME and P are process-wide startup invariants that cannot change on hot-swap."""
    try:
        if (type(contract.get('input_dim')) is not int or contract['input_dim'] != 61 or
                type(contract.get('output_dim')) is not int or contract['output_dim'] != 14 or
                contract.get('obs_normalizer') != 'baked_in_onnx'):
            raise ValueError('Observation interface must be normalized 61-dim input / 14-dim action')
        model = contract['model']
        names = contract['onnx_metadata']['joint_names'].split(',')
        if names != JOINTS:
            raise ValueError('Joint order mismatch')
        rows = model['joints']
        if not isinstance(rows, list):
            raise ValueError('Incomplete HOME joint list')
        homes = {}
        for name in JOINTS:
            matches = [r for r in rows if isinstance(r, dict) and r.get('name') == name]
            if len(matches) != 1:
                raise ValueError('Missing or duplicate HOME joint: ' + name)
            homes[name] = matches[0]['home_rad']
        if any(isinstance(homes[n], bool) or not isinstance(homes[n], (float, int))
               or not math.isfinite(homes[n]) for n in JOINTS):
            raise ValueError('HOME contains invalid angle')
        actuators = model['actuators']
        if not isinstance(actuators, list) or len(actuators) != 1:
            raise ValueError('Incomplete servo P parameters')
        kp = actuators[0]['kp_fw']
        if isinstance(kp, bool) or not isinstance(kp, (int, float)) or not math.isfinite(kp) or int(kp) != kp:
            raise ValueError('Invalid servo P parameter')
        return {'joints': names, 'home': [float(homes[n]) for n in JOINTS], 'kp_fw': int(kp)}
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        raise ModelError(f'Cannot verify model control contract (HOME / servo P / joint order): {exc}') from exc


class Models:
    def __init__(self, rd, log, state_dir, tune=None, root='/', validator=None, rpc=None, skill_table=None):
        self.rd, self.log, self.tune = rd, log, tune
        # Updating the action table requires root via helper; tests inject a mock
        self.skill_table = skill_table
        self.root = Path(root)
        self.directory = Path(state_dir) / 'models'
        self.catalog = self.directory / 'library.json'
        self.validator, self.rpc = validator, rpc
        self._lock = asyncio.Lock()
        self._disk_lock = threading.RLock()

    def _local(self, path):
        if not isinstance(path, str) or not path.startswith('/'):
            raise ModelError('robotd model path must be absolute')
        return self.root / path.lstrip('/')

    def _remote(self, path):
        path = Path(path).resolve()
        if str(self.root) == '/':
            return str(path)
        try:
            return '/' + path.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            # Desktop standalone state_dir; production state lives under /var/lib
            return path.as_posix()

    def _managed(self, path):
        try:
            self._local(path).resolve().relative_to(self.directory.resolve())
            return True
        except (ValueError, ModelError, OSError):
            return False

    def _init_dir(self):
        if self.directory.is_symlink():
            raise ModelError('Model library directory cannot be a symlink')
        self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def actions_dir(self):
        # Store 'More Actions' in a separate subdirectory so older Studio versions only scan top-level models.
        return self.directory / 'actions'

    def _init_actions_dir(self):
        self._init_dir()
        if self.actions_dir.is_symlink():
            raise ModelError('Actions directory cannot be a symlink')
        self.actions_dir.mkdir(exist_ok=True)

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80 or any(ord(c) < 32 for c in value):
            raise ModelError('Model name must be 1-80 visible characters')
        return value.strip()

    async def _call(self, method, params=None, timeout=2.0):
        if self.rpc is not None:
            return await self.rpc(method, params or {}, timeout=timeout)
        return await call_once(self.rd.addr, method, params, timeout=timeout, name='models', log=self.log)

    async def _snapshot(self):
        names = ('robot.policies', 'robot.busStatus', 'pad.bindings', 'robot.skills')
        values = await asyncio.gather(*(self._call(n) for n in names), return_exceptions=True)
        snap, errors = {}, []
        for name, val in zip(('policies', 'bus', 'bindings', 'skills'), values):
            if self._report_valid(name, val):
                snap[name] = val
            else:
                snap[name] = None
                errors.append(name)
        snap['error'] = 'robotd connection or readback failed; cannot verify active configuration' if errors else None
        return snap

    @staticmethod
    def _report_valid(name, value):
        """Reject empty or partial reports as authoritative readback."""
        if not isinstance(value, dict):
            return False
        def path_ok(path):
            return path is None or isinstance(path, str) and path.startswith('/')
        def rows_ok(key, check):
            rows = value.get(key)
            return isinstance(rows, list) and all(isinstance(row, dict) and check(row) for row in rows)
        if name == 'policies':
            if not isinstance(value.get('enabled'), bool) or not isinstance(value.get('skills'), list) or not all(isinstance(s, str) for s in value['skills']):
                return False
            if not rows_ok('slots', lambda r: isinstance(r.get('slot'), str) and r['slot'] in SLOTS and
                           path_ok(r.get('path')) and isinstance(r.get('overridden'), bool)):
                return False
            return sorted(r['slot'] for r in value['slots']) == sorted(SLOTS)
        if name == 'bus':
            lm = value.get('loaded_models')
            return isinstance(value.get('mode'), str) and isinstance(lm, dict) and all(
                isinstance(k, str) and (v is None or isinstance(v, dict) and path_ok(v.get('path'))) for k, v in lm.items())
        if name == 'bindings':
            return rows_ok('bindings', lambda r: isinstance(r.get('button'), str) and r['button'] in BUTTONS and
                           isinstance(r.get('skill'), str) and isinstance(r.get('overridden'), bool)) and sorted(
                               r['button'] for r in value['bindings']) == sorted(BUTTONS)
        if name == 'skills':
            return rows_ok('skills', lambda r: isinstance(r.get('name'), str) and path_ok(r.get('path'))) and isinstance(
                value.get('built_in'), list) and all(isinstance(n, str) for n in value['built_in'])
        return False

    @staticmethod
    def _idle_reason(snap):
        bus, pol = snap.get('bus'), snap.get('policies')
        if not isinstance(bus, dict) or not isinstance(pol, dict):
            return 'robotd connection or readback failed; cannot verify torque state'
        native = bus.get('native')
        if not isinstance(native, dict) or native.get('torque_enabled') is not False or native.get('torque_state_confirmed') is not False:
            return 'Release torque (E-Stop) and wait for confirmation before modifying models or bindings'
        if bus.get('policy_enabled') is not False:
            return 'Stop the active policy and release torque before modifying models or bindings'
        if bus.get('mode') != 'motion' or pol.get('enabled') is not True:
            return 'Model runner is not active in current mode; switch to motion mode with torque off'
        if snap.get('error'):
            return snap['error']
        return None

    async def _require_idle(self):
        snap = await self._snapshot()
        reason = self._idle_reason(snap)
        if reason:
            raise ModelError(reason, status=409, code='not_idle')
        return snap

    @staticmethod
    def _rows(value, key):
        rows = value.get(key) if isinstance(value, dict) else None
        return rows if isinstance(rows, list) else []

    def _load_catalog(self):
        if not self.catalog.exists():
            return {'schema': SCHEMA, 'defaults': {}}, None
        try:
            data = _json(self.catalog, 1 << 20)
            if data.get('schema') != SCHEMA or not isinstance(data.get('defaults'), dict):
                raise ValueError('Catalog schema mismatch')
            for slot, d in data['defaults'].items():
                if slot not in SLOTS or not isinstance(d, dict) or d.get('id') != 'builtin-' + slot:
                    raise ValueError('Invalid default model entry')
                path = d.get('path')
                disabled = d.get('disabled') is True and slot != 'walk' and path is None
                if not disabled and (not isinstance(path, str) or not path.startswith('/') or self._managed(path)):
                    raise ValueError('Invalid default restore path or points into managed library')
                self._name(d.get('name'))
                if d.get('sha256') is not None and (not isinstance(d['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', d['sha256'])):
                    raise ValueError('Invalid default model SHA256')
                sig = d.get('signature')
                if sig is not None:
                    if not isinstance(sig, dict) or sig.get('joints') != JOINTS or not isinstance(sig.get('home'), list) or len(sig['home']) != len(JOINTS):
                        raise ValueError('Invalid default HOME contract record')
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in sig['home']):
                        raise ValueError('Default HOME contains invalid angle')
                    if type(sig.get('kp_fw')) is not int or not 1 <= sig['kp_fw'] <= 32:
                        raise ValueError('Invalid default servo P record')
            return data, None
        except (OSError, ValueError, RecursionError, OverflowError) as exc:
            return {'schema': SCHEMA, 'defaults': {}}, f'Default model catalog corrupted; config writes disabled: {exc}'

    def _read_library(self, snap=None):
        with self._disk_lock:
            self._init_dir()
            catalog, error = self._load_catalog()
            if not self.catalog.exists() and snap and not snap.get('error'):
                defaults = self._capture_defaults(snap)
                if defaults:
                    catalog['defaults'] = defaults
                    _atomic_json(self.catalog, catalog)
            entries = []
            folders = [(child, False) for child in sorted(self.directory.iterdir())]
            if self.actions_dir.is_symlink():
                error = error or 'Actions directory cannot be a symlink'
            elif self.actions_dir.is_dir():
                folders += [(child, True) for child in sorted(self.actions_dir.iterdir())]
            for child, action in folders:
                if not re.fullmatch(r'[a-f0-9]{32}', child.name) or child.is_symlink() or not child.is_dir():
                    continue
                try:
                    entry = _json(child / META, 64 << 10)
                    if action:
                        # Actions have no fixed slot; suggestions are prefill hints only.
                        if entry.get('kind') != 'action' or entry.get('slot') is not None:
                            raise ValueError('Invalid action record')
                        entry['slot'] = None
                        if not isinstance(entry.get('suggest'), dict):
                            entry['suggest'] = {}
                    elif entry.get('kind') is not None or entry.get('slot') not in SLOTS:
                        raise ValueError('Invalid model identity record')
                    if entry.get('id') != child.name or not isinstance(entry.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', entry['sha256']):
                        raise ValueError('Invalid model identity record')
                    entry['name'] = sanitize_model_name(self._name(entry.get('name')))
                    entry['path'] = self._remote(child / 'policy.onnx')
                    entry['source'] = 'imported'
                    entry['installed'] = self._local(entry['path']).is_file() and not self._local(entry['path']).is_symlink()
                    entries.append(entry)
                except (ValueError, OSError, RecursionError) as exc:
                    error = error or f'Cannot read metadata for model {child.name[:8]}: {exc}'
            builtins = [dict(d, name=sanitize_model_name(d.get('name')), source='builtin', slot=s,
                             installed=self._local(d['path']).is_file() and not self._local(d['path']).is_symlink())
                        for s, d in catalog['defaults'].items() if not d.get('disabled')]
            return catalog, builtins + entries, error

    def _capture_defaults(self, snap):
        """Prefer image management-profile.json; record original explicit paths on first library init."""
        candidates, names = {}, {}
        try:
            profile = _json(self._local(BASE + '/management-profile.json'))
            for row in self._rows(profile, 'builtin_models'):
                if not isinstance(row, dict) or row.get('slot') not in SLOTS:
                    continue
                ident = row.get('id')
                if isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9_-]+', ident):
                    candidates[row['slot']] = BASE + '/models/' + ident + '/policy.onnx'
                    names[row['slot']] = row.get('name')
        except (OSError, ValueError):
            pass
        for row in self._rows(snap.get('policies'), 'slots'):
            if isinstance(row, dict) and row.get('slot') in SLOTS:
                candidates.setdefault(row['slot'], row.get('path'))
        defaults = {}
        tune_names = (getattr(self.tune, 'disk', {}) or {}).get('builtin_names', {})
        for slot, path in candidates.items():
            if not isinstance(path, str) or not path.startswith('/') or self._managed(path):
                continue
            local = self._local(path)
            if not local.is_file() or local.is_symlink():
                continue
            sig, task, sha = None, None, None
            try:
                con = _json(local.with_name('deployment-contract.json'))
                sig, task = _signature(con), con.get('task')
            except (OSError, ValueError):
                pass
            try:
                sha = hashlib.sha256(local.read_bytes()).hexdigest()
            except OSError:
                pass
            defaults[slot] = {'id': 'builtin-' + slot, 'path': path,
                              'name': sanitize_model_name(names.get(slot) or tune_names.get(local.parent.name) or LABELS[slot]),
                              'task': task, 'sha256': sha, 'signature': sig}
        for row in self._rows(snap.get('policies'), 'slots'):
            slot = row.get('slot')
            if slot in SLOTS and slot != 'walk' and slot not in defaults and row.get('path') is None:
                defaults[slot] = {'id': 'builtin-' + slot, 'path': None, 'name': LABELS[slot] + ' (Not installed)',
                                  'task': None, 'sha256': None, 'signature': None, 'disabled': True}
        return defaults

    def _validator_available(self):
        return self.validator is not None or self._local(PREFLIGHT).is_file()

    def _runtime_env(self):
        env = dict(os.environ)
        if str(self.root) != '/' or os.name == 'nt':
            return env
        try:
            result = subprocess.run(['systemctl', 'show', 'robotd.service', '--property=Environment', '--value'],
                                    capture_output=True, text=True, timeout=3, shell=False)
            if result.returncode == 0:
                for item in shlex.split(result.stdout):
                    key, sep, value = item.partition('=')
                    if sep and key in ('ORT_DYLIB_PATH', 'LD_LIBRARY_PATH'):
                        env[key] = value
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return env

    def _preflight(self, path):
        if self.validator is not None:
            result = self.validator(path)
        else:
            if not self._validator_available():
                raise ModelError('No trusted model runner available locally for ONNX preflight', status=503)
            result = validate_policy(path, str(self._local(PREFLIGHT)), env=self._runtime_env())
        if not isinstance(result, dict) or result.get('valid') is not True:
            raise ModelError('Model runner preflight failed: ' + str((result or {}).get('error', 'not confirmed valid') if isinstance(result, dict) else 'invalid response'))

    def _install(self, path, name):
        ident = uuid.uuid4().hex
        stage, final = self.directory / ('.stage-' + ident), self.directory / ident
        try:
            self._init_dir()
            data = extract_package(Path(path), stage)
            self._preflight(stage / data['onnx'])
            slot = data.get('slot')
            if slot not in SLOTS:
                raise ModelError('Unrecognized model action slot; cannot install')
            record = {'id': ident, 'name': self._name(name or data.get('name') or LABELS[slot]),
                      'task': data['task'], 'slot': slot, 'sha256': data['sha256'], 'files': data['files']}
            _atomic_json(stage / META, record)
            os.replace(stage, final)
        except (PackageError, OSError, KeyError, TypeError) as exc:
            raise ModelError(f'Model import failed: {exc}') from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def _install_action(self, path, name):
        """Import action into library without registering it yet; keep model isolated with extras/."""
        ident = uuid.uuid4().hex
        stage, final = self.actions_dir / ('.stage-' + ident), self.actions_dir / ident
        try:
            self._init_actions_dir()
            data = extract_action_package(Path(path), stage)
            self._preflight(stage / data['onnx'])
            suggest = data.get('suggest') or {}
            record = {'id': ident, 'kind': 'action',
                      'name': self._name(name or suggest.get('title') or suggest.get('name') or 'New Action'),
                      'task': None, 'sha256': data['sha256'], 'files': data['files'], 'suggest': suggest}
            _atomic_json(stage / META, record)
            os.replace(stage, final)
        except (PackageError, OSError, KeyError, TypeError) as exc:
            raise ModelError(f'Action import failed: {exc}') from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    async def import_package(self, path, name='', kind=None):
        if not isinstance(name, str):
            raise ModelError('Model name must be text')
        if kind not in (None, 'action'):
            raise ModelError('Unrecognized import kind')
        if name:
            self._name(name)
        async with self._lock:
            snap = await self._snapshot()
            _, _, error = await self._disk(self._read_library, snap)
            if error:
                raise ModelError(error, status=409)
            await asyncio.to_thread(self._install_action if kind == 'action' else self._install, path, name)
        return await self.view()

    @staticmethod
    def _references(snap):
        paths = set()
        for key, field in (('policies', 'slots'), ('skills', 'skills')):
            for row in Models._rows(snap.get(key), field):
                if isinstance(row, dict) and isinstance(row.get('path'), str):
                    paths.add(row['path'])
        lm = (snap.get('bus') or {}).get('loaded_models')
        if isinstance(lm, dict):
            paths.update(v['path'] for v in lm.values() if isinstance(v, dict) and isinstance(v.get('path'), str))
        return paths

    def _needs_skill_table(self):
        """Image robotd.toml may write inline `"skill" = [{...}]`, whereas robotd only edits [[policy.skill]]."""
        path = self._local('/etc/robot/feetech-ft5/robotd.toml')
        try:
            text = path.read_text(encoding='utf-8')
            policy = tomllib.loads(text).get('policy')
        except (OSError, ValueError, UnicodeError):
            return False
        skills = policy.get('skill') if isinstance(policy, dict) else None
        return isinstance(skills, list) and bool(skills) and not re.search(
            r'^\s*\[\[\s*"?policy"?\s*\.\s*"?skill"?\s*\]\]', text, re.M)

    async def _ensure_skill_table(self):
        if not await asyncio.to_thread(self._needs_skill_table):
            return
        fixer = self.skill_table
        if fixer is None:
            from . import helper
            fixer = lambda: helper.skill_table('Studio Actions')
        ok, result, _ = await asyncio.to_thread(fixer)
        if not ok or await asyncio.to_thread(self._needs_skill_table):
            raise ModelError(f'Failed to convert robotd.toml skill table format ({result}); duck actions unchanged',
                             status=503, code='skill_table')
        if self.log:
            self.log.write('Converted robotd.toml skill table to [[policy.skill]] format (contents unchanged)', cat='models')

    def _configured_refs(self):
        """Read persisted TOML references so restart-referenced models are never deleted."""
        path = self._local('/etc/robot/feetech-ft5/robotd.toml')
        try:
            if path.stat().st_size > 2 << 20:
                raise ValueError('Config exceeds size limit')
            with path.open('rb') as f:
                data = tomllib.load(f)
            policy = data.get('policy')
            if not isinstance(policy, dict):
                raise ValueError('Missing policy section')
            rows = policy.get('skill', [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError('Invalid policy.skill format')
            paths = [policy.get(s) for s in SLOTS] + [r.get('path') for r in rows]
            if any(p is not None and (not isinstance(p, str) or p != 'none' and not p.startswith('/')) for p in paths):
                raise ValueError('Invalid model path in config')
            return {p for p in paths if p and p != 'none'}, None
        except (OSError, ValueError, RecursionError) as exc:
            return set(), f'Cannot read persisted robotd config to verify safe deletion: {exc}'

    def _normalize_refs(self, refs):
        try:
            return {self._remote(self._local(path).resolve()) for path in refs}
        except (OSError, RuntimeError) as exc:
            raise ModelError(f'Cannot resolve referenced model path to verify safe deletion: {exc}') from exc

    @staticmethod
    def _delete_reason(item, snap, refs, config_error):
        if item['source'] == 'builtin':
            return 'Built-in default models cannot be deleted'
        if snap['error'] or config_error:
            return snap['error'] or config_error
        policies = snap.get('policies') or {}
        if policies.get('change_error') or any(r.get('error') for r in Models._rows(policies, 'slots')):
            return 'robotd has a model load error; resolve it before deleting models'
        if item['path'] in refs:
            return 'Model is still referenced by a slot, skill, or persisted config; switch or restore defaults first'
        return None

    async def view(self):
        snap = await self._snapshot()
        try:
            catalog, entries, library_error = await asyncio.to_thread(self._read_library, snap)
        except (OSError, ValueError) as exc:
            catalog, entries, library_error = {'defaults': {}}, [], f'Cannot read model library: {exc}'
        reason = library_error or self._idle_reason(snap)
        by_path = {i['path']: i['id'] for i in entries}
        slot_rows = {r.get('slot'): r for r in self._rows(snap.get('policies'), 'slots') if isinstance(r, dict)}
        refs = self._references(snap)
        configured, config_error = await asyncio.to_thread(self._configured_refs)
        refs.update(configured)
        try:
            refs = await asyncio.to_thread(self._normalize_refs, refs)
        except ModelError as exc:
            config_error = str(exc)
        slots = []
        for slot in SLOTS:
            row = slot_rows.get(slot, {})
            default = catalog['defaults'].get(slot, {})
            slots.append({'slot': slot, 'label': LABELS[slot], 'model_id': by_path.get(row.get('path')),
                          'path': row.get('path'), 'error': row.get('error'), 'default_id': default.get('id'),
                          'default_disabled': default.get('disabled') is True,
                          'is_default': bool(default and row and row.get('path') == default.get('path') and not row.get('error'))})
        slots_by_name = {s['slot']: s for s in slots}
        bindings = []
        for row in self._rows(snap.get('bindings'), 'bindings'):
            if not isinstance(row, dict) or row.get('button') not in BUTTONS:
                continue
            b, skill = row['button'], row.get('skill')
            slot = next((s for s, sk in SKILLS.items() if sk == skill), None)
            bindings.append({'button': b, 'label': BUTTONS[b], 'slot': slot, 'skill': skill,
                             'model_id': slots_by_name.get(slot, {}).get('model_id'), 'default_skill': DEFAULT_KEYS[b],
                             'overridden': row.get('overridden'), 'error': row.get('error')})
        actions, manual = self._actions_view(entries, snap, refs, config_error)
        names = {a['skill']: a['name'] for a in actions + manual if a.get('skill')}
        for b in bindings:
            if b['slot'] is None and b.get('skill') in names:
                b['action_name'] = names[b['skill']]
        items = []
        active = (snap.get('bus') or {}).get('active_model')
        for entry in entries:
            if entry.get('kind') == 'action':
                continue
            path, slot = entry['path'], entry['slot']
            chosen = [s['slot'] for s in slots if s.get('path') == path]
            delete_reason = self._delete_reason(entry, snap, refs, config_error)
            file_reason = None if entry['installed'] else 'Model policy.onnx file is missing or unsafe; please re-import'
            items.append({'id': entry['id'], 'name': sanitize_model_name(entry['name']), 'source': entry['source'], 'task': entry.get('task'),
                          'suggested_slot': slot, 'allowed_slots': [slot], 'slots': chosen,
                          'running': bool(isinstance(active, dict) and active.get('path') == path),
                          'buttons': [b['button'] for b in bindings if b.get('model_id') == entry['id']],
                          'can_delete': delete_reason is None, 'reason': file_reason or delete_reason,
                          'sha12': (entry.get('sha256') or '')[:12] or None, 'path': path, 'installed': entry['installed']})
        available = await asyncio.to_thread(self._validator_available)
        reason = reason or (None if available else 'No trusted model runner available locally for ONNX preflight')
        # Action durations for UI list; built-in controller skills (ground_pick, sit_toggle) have no fixed duration.
        durations = {r['name']: r['duration'] for r in self._rows(snap.get('skills'), 'skills')
                     if isinstance(r, dict) and isinstance(r.get('name'), str) and isinstance(r.get('duration'), (int, float))
                     and not isinstance(r.get('duration'), bool)}
        return {'items': items, 'slots': slots, 'bindings': bindings, 'actions': actions, 'manual_actions': manual,
                'action_limit': MAX_ACTIONS, 'skill_durations': durations,
                'loaded_skills': list((snap.get('policies') or {}).get('skills') or []),
                'buttons': [{'button': b, 'label': l} for b, l in BUTTONS.items()],
                'capabilities': {'import': available and not library_error, 'select': reason is None,
                                 'rename': not library_error, 'delete': not snap['error'] and not config_error and not library_error,
                                 'reason': reason, 'import_reason': library_error or (None if available else 'No trusted model runner available locally for ONNX preflight')},
                'error': library_error or snap['error']}

    async def _library(self, snap):
        catalog, entries, error = await self._disk(self._read_library, snap)
        if error:
            raise ModelError(error, status=409, code='library_corrupt')
        return catalog, entries

    @staticmethod
    async def _disk(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except ModelError:
            raise
        except (OSError, ValueError) as exc:
            raise ModelError(f'Model file read/write failed: {exc}', status=503) from exc

    @staticmethod
    def _item(entries, ident):
        item = next((i for i in entries if i['id'] == ident), None)
        if item is None:
            raise ModelError('Model not found; please refresh the list')
        return item

    def _rename(self, item, catalog, name):
        with self._disk_lock:
            if item['source'] == 'builtin':
                catalog['defaults'][item['slot']]['name'] = name
                _atomic_json(self.catalog, catalog)
            else:
                path = (self.actions_dir if item.get('kind') == 'action' else self.directory) / item['id'] / META
                record = _json(path, 64 << 10)
                record['name'] = name
                _atomic_json(path, record)

    def _delete(self, item):
        if not re.fullmatch(r'[a-f0-9]{32}', item['id']):
            raise ModelError('Invalid model directory ID')
        parent = self.actions_dir if item.get('kind') == 'action' else self.directory
        target = parent / item['id']
        if target.is_symlink() or target.resolve().parent != parent.resolve():
            raise ModelError('Unsafe model directory path')
        shutil.rmtree(target)

    def _check_compatibility(self, item, catalog, snap):
        baseline = (catalog['defaults'].get('walk') or {}).get('signature')
        if not isinstance(baseline, dict):
            raise ModelError('Missing boot walk model HOME / servo P contract; cannot verify safe hot-swap')
        target = _signature(_json(self._local(item['path']).with_name('deployment-contract.json')))
        current_path = next((r.get('path') for r in self._rows(snap['policies'], 'slots')
                             if isinstance(r, dict) and r.get('slot') == 'walk'), None)
        if not current_path:
            raise ModelError('Cannot verify control contract of current walk model')
        current = _signature(_json(self._local(current_path).with_name('deployment-contract.json')))
        for candidate in (current, target):
            if candidate.get('joints') != baseline.get('joints'):
                raise ModelError('Model joint order does not match boot control contract; cannot hot-swap')
            if candidate.get('kp_fw') != baseline.get('kp_fw'):
                raise ModelError('Model servo P differs from boot control contract; redeploy and restart robotd')
            a, b = candidate.get('home'), baseline.get('home')
            if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b) or any(abs(x - y) > 1e-8 for x, y in zip(a, b)):
                raise ModelError('Model HOME pose differs from boot control contract; redeploy and restart robotd')
        if item['source'] == 'imported':
            digest = hashlib.sha256(self._local(item['path']).read_bytes()).hexdigest()
            if digest != item['sha256']:
                raise ModelError('Imported model file changed (SHA256 mismatch); please re-import')
        self._preflight(self._local(item['path']))

    def _check_selection_files(self, item, catalog, snap):
        try:
            self._check_compatibility(item, catalog, snap)
        except ModelError:
            raise
        except (OSError, ValueError, OverflowError) as exc:
            raise ModelError(f'Failed to read or preflight model files and contract: {exc}') from exc

    async def _write_rpc(self, method, params):
        result = await self._call(method, params, timeout=8.0)
        if not isinstance(result, dict) or result.get('accepted') is not True:
            raise ModelError('robotd rejected config change: ' + str((result or {}).get('reason', 'not accepted') if isinstance(result, dict) else 'no reply'), status=409)

    async def _wait_loaded(self, slot, path, expected_skill=None):
        deadline = time.monotonic() + READBACK_TIMEOUT
        last = 'Model load not yet confirmed on readback'
        while True:
            snap = await self._snapshot()
            rows = {r.get('slot'): r for r in self._rows(snap.get('policies'), 'slots') if isinstance(r, dict)}
            row = rows.get(slot, {})
            lm = (snap.get('bus') or {}).get('loaded_models')
            lm = lm if isinstance(lm, dict) else {}
            actual = lm.get(slot)
            expected = None if path == 'none' else path
            error = snap['error'] or (snap.get('policies') or {}).get('change_error') or row.get('error')
            skill_ok = True
            if expected_skill:
                sk, skpath = expected_skill
                cfg = next((r for r in self._rows(snap.get('skills'), 'skills') if isinstance(r, dict) and r.get('name') == sk), {})
                loaded = lm.get(sk)
                skill_ok = cfg.get('path') == skpath and isinstance(loaded, dict) and loaded.get('path') == skpath
            loaded_ok = actual is None if expected is None else isinstance(actual, dict) and actual.get('path') == expected
            if not error and row and row.get('path') == expected and isinstance(row.get('overridden'), bool) and loaded_ok and skill_ok:
                return
            last = str(error or ('Slot path or loaded model mismatch' if not loaded_ok else 'Skill model or slot readback mismatch'))
            if time.monotonic() >= deadline:
                raise ModelError('Model load unconfirmed: ' + last, status=503, code='load_unconfirmed')
            await asyncio.sleep(READBACK_POLL)

    async def _select(self, item, slot, catalog, snap):
        if slot not in SLOTS or item['slot'] != slot:
            raise ModelError('Model purpose does not match selected slot')
        if not item.get('installed'):
            raise ModelError('Model policy.onnx file is missing or unsafe; please re-import')
        await asyncio.to_thread(self._check_selection_files, item, catalog, snap)
        # Preflight can take seconds; re-verify torque is off before writing config.
        snap = await self._require_idle()
        old = next((r.get('path') for r in self._rows(snap['policies'], 'slots')
                    if isinstance(r, dict) and r.get('slot') == slot), None)
        if old is None and slot == 'walk':
            raise ModelError('Current walk model path unknown; cannot save rollback target')
        canonical = SKILLS.get(slot)
        if canonical and canonical not in snap['policies'].get('skills', []) and canonical not in snap['skills'].get('built_in', []):
            raise ModelError('robotd does not provide this action skill; cannot activate via slot alone')
        skill = next((r for r in self._rows(snap['skills'], 'skills')
                      if isinstance(r, dict) and r.get('name') == canonical and isinstance(r.get('path'), str)), None)
        changed_skill = skill is not None and slot not in ('sitstand', 'ground_pick') and skill['path'] != item['path']
        expected_skill = (canonical, item['path']) if changed_skill else None
        if changed_skill:
            await self._ensure_skill_table()
        try:
            await self._write_rpc('robot.loadPolicy', {'slot': slot, 'path': item['path']})
            if changed_skill:
                await self._require_idle()
                await self._write_rpc('robot.setSkill', {'name': canonical, 'path': item['path']})
            await self._wait_loaded(slot, item['path'], expected_skill)
        except Exception as exc:
            try:
                await self._require_idle()
                await self._write_rpc('robot.loadPolicy', {'slot': slot, 'path': old or 'none'})
                if changed_skill:
                    await self._write_rpc('robot.setSkill', {'name': canonical, 'path': skill['path']})
                await self._wait_loaded(slot, old or 'none', (canonical, skill['path']) if changed_skill else None)
            except Exception as rollback:
                raise ModelError(f'Model switch failed: {exc}; rollback also unconfirmed: {rollback}', status=503, code='rollback_failed') from exc
            raise ModelError(f'Model switch failed: {exc}; rolled back to previous model', status=503, code='load_failed') from exc

    async def _restore_disabled(self, slot, snap):
        old = next((r.get('path') for r in self._rows(snap['policies'], 'slots') if r['slot'] == slot), None)
        try:
            await self._require_idle()
            await self._write_rpc('robot.loadPolicy', {'slot': slot, 'path': 'none'})
            await self._wait_loaded(slot, 'none')
        except Exception as exc:
            try:
                await self._require_idle()
                await self._write_rpc('robot.loadPolicy', {'slot': slot, 'path': old or 'none'})
                await self._wait_loaded(slot, old or 'none')
            except Exception as rollback:
                raise ModelError(f'Restore default failed: {exc}; rollback also unconfirmed: {rollback}', status=503, code='rollback_failed') from exc
            raise ModelError(f'Restore default failed: {exc}; rolled back', status=503, code='load_failed') from exc

    async def _wait_bindings(self, expected):
        deadline = time.monotonic() + READBACK_TIMEOUT
        while True:
            try:
                data = await self._call('pad.bindings')
                rows = {r.get('button'): r for r in self._rows(data, 'bindings') if isinstance(r, dict)}
                if all(b in rows and rows[b].get('skill') == spec['skill'] and
                       rows[b].get('overridden') is spec['overridden'] and not rows[b].get('error')
                       for b, spec in expected.items()):
                    return
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise ModelError('Button binding change unconfirmed on readback', status=503)
            await asyncio.sleep(READBACK_POLL)

    async def _bind(self, args, snap):
        op = args['op']
        rows = {r.get('button'): r for r in self._rows(snap['bindings'], 'bindings') if isinstance(r, dict)}
        changes = []
        if op == 'reset_bindings':
            changes = [(b, None) for b in BUTTONS]
        else:
            button = args.get('button')
            if not isinstance(button, str) or button not in BUTTONS:
                raise ModelError('Only A, X, LB, RB, and D-Pad Down buttons are supported')
            skill = ''
            if op == 'bind' and args.get('skill') is not None:
                # Custom action: only allow skills currently loaded by robotd.
                skill = args.get('skill')
                if not isinstance(skill, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', skill) or skill in RESERVED:
                    raise ModelError('Not a bindable custom action')
                if skill not in ((snap.get('policies') or {}).get('skills') or []):
                    raise ModelError('Action not loaded by robotd yet; install and verify in More Actions first')
                current = rows.get(button, {}).get('skill')
                if current and current != skill and args.get('replace') is not True:
                    label = next((LABELS[s] for s, sk in SKILLS.items() if sk == current), current)
                    raise ModelError(f'{BUTTONS[button]} is currently bound to \'{label}\'; confirm replacement', status=409, code='binding_conflict')
            elif op == 'bind':
                slot = args.get('slot')
                if not isinstance(slot, str) or slot not in SKILLS:
                    raise ModelError('Walk and standalone Stand are not bindable one-shot skills')
                selected = next((r for r in self._rows(snap['policies'], 'slots')
                                 if isinstance(r, dict) and r.get('slot') == slot), {})
                if not selected.get('path') or selected.get('error'):
                    raise ModelError('No usable model selected for this action yet')
                skill = SKILLS[slot]
                current = rows.get(button, {}).get('skill')
                if current and current != skill and args.get('replace') is not True:
                    label = next((LABELS[s] for s, sk in SKILLS.items() if sk == current), current)
                    raise ModelError(f'{BUTTONS[button]} is currently bound to \'{label}\'; confirm replacement', status=409, code='binding_conflict')
            changes = [(button, skill)]
        if any(b not in rows or not isinstance(rows[b].get('skill'), str) or not isinstance(rows[b].get('overridden'), bool) for b, _ in changes):
            raise ModelError('Incomplete binding readback; cannot save rollback state', status=503)
        attempted = []
        try:
            expected = {}
            for button, skill in changes:
                await self._require_idle()
                attempted.append(button)
                params = {'button': button}
                if skill is not None:
                    params['skill'] = skill
                await self._write_rpc('pad.bind', params)
                target_skill = DEFAULT_KEYS[button] if skill is None else skill
                expected[button] = {'skill': target_skill, 'overridden': target_skill != DEFAULT_KEYS[button]}
            await self._wait_bindings(expected)
        except Exception as exc:
            try:
                for button in reversed(attempted):
                    await self._require_idle()
                    params = {'button': button}
                    if rows[button]['overridden']:
                        params['skill'] = rows[button]['skill']
                    await self._write_rpc('pad.bind', params)
                await self._wait_bindings({b: rows[b] for b in attempted})
            except Exception as rollback:
                raise ModelError(f'Binding update failed: {exc}; rollback also unconfirmed: {rollback}', status=503, code='rollback_failed') from exc
            raise ModelError(f'Binding update failed: {exc}; restored previous bindings', status=503, code='binding_failed') from exc

    # ── More Actions: kind=action library entries <-> robotd skill table ────────

    def _same_path(self, a, b):
        if not isinstance(a, str) or not isinstance(b, str):
            return False
        if a == b:
            return True
        try:
            return self._remote(self._local(a).resolve()) == self._remote(self._local(b).resolve())
        except (OSError, RuntimeError, ModelError):
            return False

    def _action_rows(self, snap, path):
        return [r for r in self._rows(snap.get('skills'), 'skills')
                if isinstance(r, dict) and self._same_path(r.get('path'), path)]

    @staticmethod
    def _skill_params(row):
        keys = ('duration', 'command', 'unwind', 'unwind_s', 'chain', 'action_scale', 'gain_ratio')
        return {k: row[k] for k in keys if row.get(k) is not None}

    def _actions_view(self, entries, snap, refs, config_error):
        loaded = set((snap.get('policies') or {}).get('skills') or [])
        change_error = (snap.get('policies') or {}).get('change_error')
        bound = {}
        for row in self._rows(snap.get('bindings'), 'bindings'):
            if isinstance(row, dict) and row.get('button') in BUTTONS:
                bound.setdefault(row.get('skill'), []).append(row['button'])
        actions, mine = [], set()
        for entry in entries:
            if entry.get('kind') != 'action':
                continue
            rows = self._action_rows(snap, entry['path'])
            row = rows[0] if rows else None
            name = row.get('name') if row else None
            if name:
                mine.add(name)
            state = 'none' if not row else 'loaded' if name in loaded else 'failed'
            delete_reason = None
            if row:
                delete_reason = 'Installed on the duck; uninstall before deleting'
            else:
                delete_reason = self._delete_reason(entry, snap, refs, config_error)
            actions.append({
                'id': entry['id'], 'name': entry['name'], 'kind': 'action', 'skill': name, 'state': state,
                'params': self._skill_params(row) if row else None, 'suggest': entry.get('suggest') or {},
                'error': (str(change_error or 'robotd has not loaded this action yet') if state == 'failed' else None),
                'buttons': bound.get(name, []) if name else [], 'extra_names': [r.get('name') for r in rows[1:]],
                'installed': entry['installed'], 'can_delete': delete_reason is None, 'reason': delete_reason,
                'sha12': entry['sha256'][:12], 'path': entry['path'], 'files': entry.get('files') or []})
        manual = []
        for row in self._rows(snap.get('skills'), 'skills'):
            name = row.get('name') if isinstance(row, dict) else None
            # Manually edited entries in robotd.toml: read-only listing, bindable, not deletable here.
            if (not isinstance(name, str) or name in RESERVED or name in mine or row.get('overridden') is not True
                    or (isinstance(row.get('path'), str) and self._managed(row['path']))):
                continue
            manual.append({'name': name, 'skill': name, 'state': 'loaded' if name in loaded else 'failed',
                           'params': self._skill_params(row), 'path': row.get('path'), 'buttons': bound.get(name, [])})
        return actions, manual

    @staticmethod
    def _action_spec(args):
        """Build complete action registration payload (robotd keeps omitted fields from previous value)."""
        def number(key, low, high, default=None, low_open=False):
            value = args.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelError(f'{key} must be a number')
            value = float(value)
            if not math.isfinite(value) or value > high or (value <= low if low_open else value < low):
                raise ModelError(f'{key} is out of range')
            return value

        def triple(key):
            value = args.get(key, [0.0, 0.0, 0.0])
            if not isinstance(value, list) or len(value) != 3:
                raise ModelError(f'{key} must have 3 numbers')
            out = []
            for v in value:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or abs(float(v)) > 2:
                    raise ModelError(f'Each element of {key} must be between -2 and 2')
                out.append(float(v))
            return out

        name = args.get('name')
        if not isinstance(name, str) or not re.fullmatch(ACTION_NAME, name):
            raise ModelError('Action name must use lowercase letters, digits, and underscores (max 32 chars)')
        if name in RESERVED:
            raise ModelError(f'\'{name}\' is a reserved built-in or system name')
        chain = args.get('chain', False)
        if not isinstance(chain, bool):
            raise ModelError('chain must be a boolean')
        return {'name': name, 'duration': number('duration', 0.2, 60.0),
                'command': triple('command'), 'unwind': triple('unwind'),
                'unwind_s': number('unwind_s', 0.0, 10.0, 0.0), 'chain': chain,
                'action_scale': number('action_scale', 0.0, 2.0, 1.0, low_open=True),
                'gain_ratio': number('gain_ratio', 0.2, 2.0, 1.0)}

    async def _wait_actions(self, present, absent):
        """Verify present skills are configured & loaded and absent skills are gone from both."""
        deadline = time.monotonic() + READBACK_TIMEOUT
        last = 'Action load not yet confirmed on readback'
        while True:
            snap = await self._snapshot()
            policies = snap.get('policies') or {}
            loaded = set(policies.get('skills') or [])
            config = {r.get('name'): r for r in self._rows(snap.get('skills'), 'skills') if isinstance(r, dict)}
            error = snap['error'] or policies.get('change_error')
            ok = (not error and all(n in loaded and self._same_path(config.get(n, {}).get('path'), p) for n, p in present.items())
                  and all(n not in loaded and n not in config for n in absent))
            if ok:
                return
            last = str(error or 'Configured or loaded skill list mismatch')
            if time.monotonic() >= deadline:
                raise ModelError('Action load unconfirmed: ' + last, status=503, code='load_unconfirmed')
            await asyncio.sleep(READBACK_POLL)

    def _check_action_file(self, item):
        if not item.get('installed'):
            raise ModelError('Action model file is missing or unsafe; please re-import')
        digest = hashlib.sha256(self._local(item['path']).read_bytes()).hexdigest()
        if digest != item['sha256']:
            raise ModelError('Action model file changed (SHA256 mismatch); please re-import')
        if any(p.name == 'deployment-contract.json' for p in self._local(item['path']).parent.iterdir()):
            raise ModelError('deployment-contract.json found alongside action model; delete and re-import to prevent boot failure')
        self._preflight(self._local(item['path']))

    async def _action_install(self, item, args, snap):
        if item.get('kind') != 'action':
            raise ModelError('This model is not a More Actions entry; use Activate instead')
        spec = self._action_spec(args)
        policies = snap.get('policies') or {}
        if policies.get('change_error'):
            raise ModelError('Resolve previous robotd skill load error first: ' + str(policies['change_error']), status=409)
        try:
            await asyncio.to_thread(self._check_action_file, item)
        except (OSError, ValueError) as exc:
            raise ModelError(f'Failed to read or preflight action model: {exc}') from exc
        snap = await self._require_idle()
        old_rows = self._action_rows(snap, item['path'])
        old_names = {r.get('name') for r in old_rows}
        config = {r.get('name'): r for r in self._rows(snap.get('skills'), 'skills') if isinstance(r, dict)}
        taken = config.get(spec['name'])
        if taken is not None and spec['name'] not in old_names:
            raise ModelError(f'An action named \'{spec["name"]}\' already exists on the duck; choose another name', status=409, code='name_taken')
        if spec['name'] in set((snap['skills'] or {}).get('built_in') or []):
            raise ModelError(f'\'{spec["name"]}\' is a built-in robotd action; choose another name', status=409, code='name_taken')
        renamed = sorted(n for n in old_names if n != spec['name'])
        bound = [r.get('button') for r in self._rows(snap.get('bindings'), 'bindings')
                 if isinstance(r, dict) and r.get('skill') in renamed]
        if bound:
            raise ModelError('Unbind this action from gamepad buttons before renaming: ' + ', '.join(BUTTONS.get(b, b) for b in bound), status=409)
        if spec['name'] not in old_names and len(config) >= MAX_ACTIONS:
            raise ModelError(f'{len(config)} actions already registered (limit {MAX_ACTIONS}); uninstall one first', status=409, code='too_many')
        params = dict(spec, path=item['path'])
        await self._ensure_skill_table()
        try:
            await self._write_rpc('robot.setSkill', params)
            for name in renamed:
                await self._require_idle()
                await self._write_rpc('robot.removeSkill', {'name': name})
            await self._wait_actions({spec['name']: item['path']}, renamed)
        except Exception as exc:
            try:
                await self._require_idle()
                if spec['name'] in old_names:
                    previous = next(r for r in old_rows if r.get('name') == spec['name'])
                    await self._write_rpc('robot.setSkill', dict(self._skill_params(previous), name=spec['name'], path=previous['path']))
                else:
                    await self._write_rpc('robot.removeSkill', {'name': spec['name']})
                for row in old_rows:
                    if row.get('name') in renamed:
                        await self._write_rpc('robot.setSkill', dict(self._skill_params(row), name=row['name'], path=row['path']))
                await self._wait_actions({r['name']: r['path'] for r in old_rows},
                                         [] if spec['name'] in old_names else [spec['name']])
            except Exception as rollback:
                raise ModelError(f'Action install failed: {exc}; rollback also unconfirmed: {rollback}', status=503, code='rollback_failed') from exc
            raise ModelError(f'Action install failed: {exc}; rolled back to previous actions', status=503, code='load_failed') from exc

    async def _action_uninstall(self, item, snap):
        if item.get('kind') != 'action':
            raise ModelError('This model is not a More Actions entry')
        old_rows = self._action_rows(snap, item['path'])
        if not old_rows:
            raise ModelError('This action is not installed on the duck', status=409)
        names = {r['name'] for r in old_rows}
        rows = {r.get('button'): r for r in self._rows(snap['bindings'], 'bindings') if isinstance(r, dict)}
        buttons = [b for b, r in rows.items() if r.get('skill') in names]
        if any(not isinstance(rows[b].get('overridden'), bool) for b in buttons):
            raise ModelError('Incomplete binding readback; cannot save rollback state', status=503)
        await self._ensure_skill_table()
        try:
            # Restore bound buttons to factory defaults before removing the skill.
            for button in buttons:
                await self._require_idle()
                await self._write_rpc('pad.bind', {'button': button})
            if buttons:
                await self._wait_bindings({b: {'skill': DEFAULT_KEYS[b], 'overridden': False} for b in buttons})
            for name in sorted(names):
                await self._require_idle()
                await self._write_rpc('robot.removeSkill', {'name': name})
            await self._wait_actions({}, sorted(names))
        except Exception as exc:
            try:
                await self._require_idle()
                snap = await self._snapshot()
                config = {r.get('name') for r in self._rows(snap.get('skills'), 'skills') if isinstance(r, dict)}
                for row in old_rows:
                    if row['name'] not in config:
                        await self._write_rpc('robot.setSkill', dict(self._skill_params(row), name=row['name'], path=row['path']))
                await self._wait_actions({r['name']: r['path'] for r in old_rows}, [])
                for button in buttons:
                    await self._write_rpc('pad.bind', {'button': button, 'skill': rows[button]['skill']})
                if buttons:
                    await self._wait_bindings({b: rows[b] for b in buttons})
            except Exception as rollback:
                raise ModelError(f'Action uninstall failed: {exc}; rollback also unconfirmed: {rollback}', status=503, code='rollback_failed') from exc
            raise ModelError(f'Action uninstall failed: {exc}; restored previous state', status=503, code='unload_failed') from exc

    async def handle(self, args, by=None):
        if not isinstance(args, dict) or args.get('op') not in ('rename', 'select', 'restore', 'delete', 'bind', 'unbind', 'reset_bindings',
                                                                'action_install', 'action_uninstall'):
            raise ModelError('Unsupported model management operation')
        op = args['op']
        async with self._lock:
            snap = await self._snapshot()
            catalog, entries = await self._library(snap)
            if op == 'rename':
                item = self._item(entries, args.get('id'))
                await self._disk(self._rename, item, catalog, self._name(args.get('name')))
            elif op == 'delete':
                item = self._item(entries, args.get('id'))
                refs, config_error = await asyncio.to_thread(self._configured_refs)
                refs.update(self._references(snap))
                refs = await asyncio.to_thread(self._normalize_refs, refs)
                reason = self._delete_reason(item, snap, refs, config_error)
                if reason:
                    raise ModelError(reason, status=409, code='model_in_use')
                await self._disk(self._delete, item)
            else:
                reason = self._idle_reason(snap)
                if reason:
                    raise ModelError(reason, status=409, code='not_idle')
                if op == 'action_install':
                    await self._action_install(self._item(entries, args.get('id')), args, snap)
                elif op == 'action_uninstall':
                    await self._action_uninstall(self._item(entries, args.get('id')), snap)
                elif op in ('select', 'restore'):
                    slot = args.get('slot')
                    if slot not in SLOTS:
                        raise ModelError('Unrecognized model slot')
                    ident = args.get('id') if op == 'select' else (catalog['defaults'].get(slot) or {}).get('id')
                    if not ident:
                        raise ModelError('No built-in default model saved for this slot')
                    if op == 'restore' and catalog['defaults'][slot].get('disabled'):
                        await self._restore_disabled(slot, snap)
                    else:
                        item = self._item(entries, ident)
                        await self._select(item, slot, catalog, snap)
                else:
                    await self._bind(args, snap)
        if self.log:
            self.log.write('Model management configuration confirmed', cat='models', raw=f'op={op}; by={by or "web"}')
        return await self.view()
