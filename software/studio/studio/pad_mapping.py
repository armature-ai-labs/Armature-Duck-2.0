"""Learn physical controls, while padd owns the authoritative output gate and files.

No I/O from snapshot(). Only worker calls poll/handle; conn owns a native lease.
"""
import copy
import json
import math
import socket
import threading
import time

BUTTONS = ('south','east','west','north','lb','rb','select','start','du','dd','dl','dr','l3','r3','home')
AXES = ('lx','ly','rx','ry','lt','rt')
ROLES = BUTTONS + AXES
REQUIRED = ('select','start','lx','ly','rx','ry')
LABEL = dict(zip(ROLES, ('A (South)','B (East)','X (West)','Y (North)','LB','RB','Select / Back','Start',
    'D-Pad Up','D-Pad Down','D-Pad Left','D-Pad Right','L3','R3','Home','Left Stick X','Left Stick Y','Right Stick X','Right Stick Y','LT','RT')))


class MappingError(ValueError):
    pass


class MappingClient:
    def __init__(self, address='unix:/run/padd/mapping.sock', timeout=.8):
        self.address, self.timeout = address, timeout

    def call(self, request):
        try:
            if self.address.startswith('tcp:'):
                host, port = self.address[4:].rsplit(':',1)
                if host != '127.0.0.1': raise OSError('mapping test TCP must be loopback')
                connection = socket.create_connection((host,int(port)),self.timeout)
            else:
                connection = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                connection.settimeout(self.timeout)
                connection.connect(self.address.removeprefix('unix:'))
            with connection:
                payload=json.dumps(request,separators=(',',':'),allow_nan=False).encode()+b'\n'
                if len(payload)>16384: raise MappingError('Mapping configuration payload too large')
                connection.sendall(payload)
                data=bytearray(); deadline=time.monotonic()+self.timeout
                while b'\n' not in data:
                    remaining=deadline-time.monotonic()
                    if remaining<=0: raise TimeoutError()
                    connection.settimeout(remaining)
                    chunk=connection.recv(8192)
                    if not chunk: raise OSError('mapping socket closed')
                    data.extend(chunk)
                    if len(data)>262144: raise MappingError('Gamepad service response exceeded size limit')
                result=json.loads(data.split(b'\n',1)[0])
        except MappingError:
            raise
        except (OSError,ValueError,AttributeError,RecursionError):
            raise MappingError('Gamepad mapping service unavailable; verify padd is running and retry') from None
        if not isinstance(result,dict) or result.get('version')!=1:
            raise MappingError('Gamepad daemon does not support this mapping API version; update padd')
        if result.get('ok') is not True:
            raise MappingError(str(result.get('error') or 'Gamepad daemon rejected request')[:300])
        if not isinstance(result.get('status'),dict): raise MappingError('Incomplete gamepad mapping status; retry')
        return result


class PadMapping:
    def __init__(self, client=None, *, guard=None, clock=time.monotonic):
        self.client, self.guard, self.clock = client, guard or (lambda:None), clock
        self._lk, self._io = threading.RLock(), threading.Lock()
        self._state, self._at, self._next = {}, None, 0.
        self._owner, self._token, self._device = None, None, None
        self._draft, self._capture, self._error, self._message = {}, None, None, None
        self._available = False
        self._closing = False
        self._starting = False
        self._uncertain = False

    @property
    def blocked(self):
        with self._lk: return self._starting or self._uncertain or self._owner is not None or self._state.get('output_blocked') is True

    def snapshot(self, conn=None):
        with self._lk:
            stale=self._at is None or self.clock()-self._at>1.5
            status=copy.deepcopy(self._state)
            mine=self._owner is not None and conn==self._owner
            for key in ('raw',): status.pop(key,None)
            if stale:
                status.update(mapped=None,preview=None)
            return {**status,'available':self._available and not stale,'stale':stale,'mine':mine,
                    'owned':self._owner is not None,'error':self._error,'message':self._message,
                    'draft':copy.deepcopy(self._draft) if mine else {},
                    'capture':copy.deepcopy(self._capture) if mine else None,
                    'required_missing':[role for role in REQUIRED if role not in self._draft] if mine else [],
                    'output_blocked':self._starting or self._uncertain or self._owner is not None or status.get('output_blocked') is True}

    def _rpc(self, op, **values):
        if self.client is None: raise MappingError('Gamepad mapping service is not configured')
        result=self.client.call({'op':op,**values})
        if result.get('ok') is not True or result.get('version')!=1 or not isinstance(result.get('status'),dict):
            raise MappingError('Gamepad mapping service API mismatch; update padd')
        return result

    def _receive(self, result):
        with self._lk:
            self._state=copy.deepcopy(result['status']); self._at=self.clock(); self._available=True
            if type(self._state.get('output_blocked')) is bool:self._uncertain=False
            if self._owner is not None:
                current=(self._state.get('device') or {}).get('id')
                if current!=self._device:
                    self._owner=self._token=None
                    self._capture=None; self._error='Gamepad disconnected or changed; cancel and restart mapping'
                elif self._state.get('session') is None or self._state['session'].get('phase')!='learning':
                    self._owner=self._token=None; self._capture=None
                    self._message='Mapping session ended; release all buttons and center sticks to resume control'

    def poll(self, force=False):
        if self._closing or self.client is None or (not force and self.clock()<self._next): return
        if not self._io.acquire(blocking=False): return
        try:
            self._next=self.clock()+.2
            self._receive(self._rpc('status'))
            with self._lk: changed=self._learn()
            if changed: self._preview()
        except MappingError as error:
            with self._lk:
                self._available=False; self._error=str(error); self._capture=None
            self._next=self.clock()+3
        finally: self._io.release()

    def _owner_check(self, conn):
        if conn is None or self._owner!=conn or not self._token: raise MappingError('Mapping session is controlled by another browser tab')
        if (self._state.get('device') or {}).get('id')!=self._device: raise MappingError('Gamepad disconnected or changed; cancel and restart')

    def _preview(self):
        with self._lk: token,bindings=self._token,copy.deepcopy(self._draft)
        self._receive(self._rpc('preview',token=token,bindings=bindings))

    def _put(self, role, binding):
        source=lambda b: (b['kind'],b['code'],b.get('value'))
        for other, previous in self._draft.items():
            # An analog axis is a single physical control, even if source kinds differ.
            same=source(previous)==source(binding) or (previous['code']==binding['code'] and
                 previous['kind'] in ('axis','axis_button') and binding['kind'] in ('axis','axis_button') and
                 'axis' in (previous['kind'],binding['kind']))
            if other!=role and same:
                raise MappingError(f'Input already bound to {LABEL[other]}; re-record or clear it first')
        self._draft[role]=binding; self._capture=None; self._error=None
        self._message=f'{LABEL[role]} captured; select the next control'

    def _learn(self):
        c=self._capture
        if not c or c['state']=='error': return False
        raw=self._state.get('raw') or {}
        events=[e for e in raw.get('events',[]) if isinstance(e,dict) and type(e.get('seq')) is int and e['seq']>c['cursor']]
        if (any(e.get('kind')==0 and e.get('code')==3 for e in events)
                or (events and min(e['seq'] for e in events)>c['cursor']+1)):
            c['state']='error'; self._error='Input stream interrupted; release controls and retry this item';return False
        c['cursor']=max(c['cursor'],raw.get('seq',c['cursor']))
        axes={a['code']:a for a in (self._state.get('device') or {}).get('axes',[])}
        buttons={a['code'] for a in (self._state.get('device') or {}).get('buttons',[])}
        try:
            if c['role'] in BUTTONS or c['role'] in ('lt','rt'):
                keys={e['code'] for e in events if e.get('kind')==1 and e.get('value')==1 and e.get('code') in buttons}
                hats={(e['code'],e['value']) for e in events if e.get('kind')==3 and e.get('value') in (-1,1)
                      and e.get('code') in axes and axes[e['code']]['min']==-1 and axes[e['code']]['max']==1}
                if len(keys)+len(hats)>1: raise MappingError('Multiple buttons detected; release and capture one control at a time')
                if keys:
                    self._put(c['role'],{'kind':'key','code':next(iter(keys))}); return True
                if hats and c['role'] in BUTTONS:
                    code,value=next(iter(hats)); self._put(c['role'],{'kind':'axis_button','code':code,'value':value}); return True
            if c['role'] not in AXES: return False
            for e in events:
                a=axes.get(e.get('code'))
                if e.get('kind')!=3 or not a or a['max']<=a['min']: continue
                code=e['code']; baseline=c['baseline'].get(str(code),a['value']); span=a['max']-a['min']
                value=e.get('value')
                if type(value) is not int or not a['min']<=value<=a['max']: continue
                seen=c['sweep'].setdefault(str(code),{'min':baseline,'max':baseline})
                seen['min']=min(seen['min'],value); seen['max']=max(seen['max'],value)
                if abs(value-baseline)>=span*.25:
                    if c['code'] is not None and c['code']!=code: raise MappingError('Multiple axes moved; move only the target axis and retry')
                    if c['code'] is None:
                        c.update(code=code,invert=value<baseline,state='sweeping')
            return False
        except MappingError as error:
            self._error=str(error); c['state']='error'; return False

    def _confirm_axis(self):
        c=self._capture
        if not c or c.get('code') is None or c['state']=='error': raise MappingError('No single analog axis detected yet; re-record as prompted')
        role,code=c['role'],c['code']; raw=self._state.get('raw') or {}
        a=next(a for a in self._state['device']['axes'] if a['code']==code)
        center=c['baseline'].get(str(code),a['value']); sweep=c['sweep'][str(code)]
        lo,hi=sweep['min'],sweep['max']; span=a['max']-a['min']
        current=raw.get('axes',{}).get(str(code),center)
        if hi-lo<span*.5: raise MappingError('Incomplete travel range; push fully to the limit and return to center')
        if role in ('lx','ly','rx','ry') and (center-lo<span*.2 or hi-center<span*.2):
            raise MappingError('Stick must be pushed fully in both directions before centering')
        if abs(current-center)>max(1,(hi-lo)*.12): raise MappingError('Release and center the stick/trigger before confirming')
        self._put(role,{'kind':'axis','code':code,'min':lo,'max':hi,'center':center,
                        'invert':c['invert'],'deadzone':.12 if role in ('lx','ly','rx','ry') else .04})

    def handle(self, args, conn):
        action=args.get('action','status')
        allowed={'status','begin','learn','confirm_axis','skip','clear','invert','deadzone','save','cancel','reset','heartbeat'}
        if action not in allowed: raise MappingError('Unsupported mapping action')
        with self._io:
            try:
                if action=='status': self._receive(self._rpc('status'))
                elif action=='begin':
                    if conn is None: raise MappingError('Start mapping from the Gamepad page')
                    with self._lk:
                        if self._owner is not None: raise MappingError('Another page is already mapping; finish or cancel it first')
                        self._starting=True
                    self.guard(); self._receive(self._rpc('status'))
                    with self._lk:
                        dev=self._state.get('device') or {}; device_id=dev.get('id')
                        if not device_id: raise MappingError('No physical gamepad detected; connect a controller first')
                    # A timeout cannot prove that padd did not apply begin.
                    # Keep the web gate closed until a fresh status settles it.
                    with self._lk:self._uncertain=True
                    r=self._rpc('begin',device_id=device_id)
                    if not r.get('token') or r['status'].get('output_blocked') is not True:
                        raise MappingError('Gamepad daemon did not confirm output pause; cannot begin learning')
                    self._receive(r)
                    with self._lk:
                        self._owner,self._token,self._device=conn,r['token'],device_id
                        self._draft=copy.deepcopy((r['status'].get('profile') or {}).get('bindings',{}))
                        self._capture=self._error=None; self._message='Gamepad control paused; release all buttons and center sticks'
                else:
                    with self._lk:
                        if action=='cancel' and self._owner==conn:
                            token=self._token
                        else:
                            self._owner_check(conn); token=self._token
                    if action=='heartbeat': self._receive(self._rpc('renew',token=token))
                    elif action in ('cancel','reset'):
                        self._receive(self._rpc(action,token=token))
                        with self._lk:
                            self._owner=self._token=None; self._capture=None
                            self._message='Restored default mapping; release all buttons and center sticks' if action=='reset' else 'Cancelled; kept previous mapping — release buttons and center sticks'
                    elif action=='save':
                        self.guard()
                        with self._lk:
                            missing=[LABEL[r] for r in REQUIRED if r not in self._draft]
                            if missing: raise MappingError('Complete required controls first: '+', '.join(missing))
                            if self._capture is not None: raise MappingError('Confirm or cancel the active axis capture first')
                            name=args.get('name') or self._state['device']['name']
                            if not isinstance(name,str) or not name.strip() or len(name)>60: raise MappingError('Profile name must be 1–60 characters')
                            draft=copy.deepcopy(self._draft)
                        self._receive(self._rpc('save',token=token,name=name.strip(),bindings=draft))
                        with self._lk:
                            if (self._state.get('profile') or {}).get('bindings')!=draft: raise MappingError('Saved mapping readback mismatch; keep paused and retry')
                            self._owner=self._token=None; self._capture=None; self._message='Mapping saved; release buttons and center sticks to resume control'
                    else:
                        if action=='learn':
                            # Arming is a new edge boundary, not the last poll's
                            # cached frame (a held key could arrive between them).
                            self._receive(self._rpc('status'))
                        with self._lk:
                            self._owner_check(conn)
                            role=args.get('role')
                            if action!='confirm_axis' and role not in ROLES: raise MappingError('Select a standard button or stick axis')
                            self._error=None
                            if action=='learn':
                                raw=self._state.get('raw') or {}
                                if any(raw.get('buttons',{}).values()): raise MappingError('Release all buttons before capturing')
                                self._capture={'role':role,'state':'waiting','cursor':raw.get('seq',0),
                                    'baseline':copy.deepcopy(raw.get('axes',{})),'code':None,'invert':False,'sweep':{}}
                                self._message=None
                            elif action=='confirm_axis': self._confirm_axis()
                            elif action in ('skip','clear'):
                                if action=='skip' and role in REQUIRED: raise MappingError('This control is required and cannot be skipped')
                                self._draft.pop(role,None); self._capture=None
                            elif action in ('invert','deadzone'):
                                b=self._draft.get(role)
                                if not b or b.get('kind')!='axis': raise MappingError('Calibrate this axis first')
                                if action=='invert': b['invert']=not b['invert']
                                else:
                                    v=args.get('value')
                                    if type(v) not in (int,float) or not math.isfinite(v) or not .02<=v<=.3: raise MappingError('Deadzone range is 2%–30%')
                                    b['deadzone']=float(v)
                        if action!='learn': self._preview()
                return self.snapshot(conn)
            except MappingError as error:
                with self._lk: self._error=str(error)
                raise
            finally:
                if action=='begin':
                    with self._lk:self._starting=False

    def leave(self, conn):
        with self._lk:
            if self._owner is None or self._owner!=conn: return
            token=self._token; self._owner=self._token=None; self._capture=None
        # Event-loop callers only mark state; blocking cancellation belongs to a worker.
        def cancel():
            try:
                with self._io: self._receive(self._rpc('cancel',token=token))
            except MappingError: pass  # Native lease expires independently; output stays gated until neutral.
        threading.Thread(target=cancel,name='pad-mapping-cancel',daemon=True).start()

    def close(self):
        with self._lk: owner=self._owner
        self.leave(owner); self._closing=True
