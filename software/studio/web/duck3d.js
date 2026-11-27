// 3D Engineering Studio for Armature x Microduck.
// Official CAD mesh (model/NOTICE.md) with dirty-flag rendering, calibration grid,
// X-Ray shell, thermal/torque servo overlay, CoM plumb-line, gravity gizmo, and ghost pose.
import * as THREE from './vendor/three/three.module.js';
import { OrbitControls } from './vendor/three/addons/OrbitControls.js';
import { RoomEnvironment } from './vendor/three/addons/RoomEnvironment.js';
import { toCreasedNormals } from './vendor/three/addons/BufferGeometryUtils.js';

export const JOINT_NAMES = ['left_hip_yaw', 'left_hip_roll', 'left_hip_pitch', 'left_knee', 'left_ankle',
  'neck_pitch', 'head_pitch', 'head_yaw', 'head_roll', 'mouth',
  'right_hip_yaw', 'right_hip_roll', 'right_hip_pitch', 'right_knee', 'right_ankle'];

const VIEWS = {
  front34: [-30, 12],
  front: [0, 8],
  side: [90, 10],
  top: [0, 82],
  back: [180, 12],
  back34: [205, 14],
};

function materialFor(rgba) {
  const [r, g, b] = rgba;
  const near = (a, bb, c) => Math.abs(r - a) < 0.04 && Math.abs(g - bb) < 0.04 && Math.abs(b - c) < 0.04;
  let color, rough = 0.5;
  if (near(0.98, 0.71, 0.0)) { color = '#e89a2c'; rough = 0.42; }
  else if (near(0.29, 0.29, 0.29)) { color = '#4c5056'; rough = 0.58; }
  else if (near(0.35, 0.38, 0.40)) { color = '#3e4248'; rough = 0.55; }
  else if (near(0.26, 0.28, 0.30)) { color = '#282b30'; rough = 0.4; }
  else if (near(0.54, 0.85, 0.83)) { color = '#86D3CC'; rough = 0.6; }
  else if (r > 0.75 && g > 0.75 && b > 0.75) { color = '#ede7da'; rough = 0.5; }
  else color = new THREE.Color(r, g, b).getStyle();
  return new THREE.MeshStandardMaterial({ color: new THREE.Color(color), roughness: rough, metalness: 0.05, envMapIntensity: 0.6 });
}

let modelCache = null;
function loadInWorker(base) {
  return new Promise((resolve, reject) => {
    let w;
    try { w = new Worker(new URL('./model-worker.js', import.meta.url)); } catch (e) { reject(e); return; }
    w.onmessage = (e) => {
      w.terminate();
      if (!e.data.ok) { reject(new Error(e.data.error)); return; }
      const geoms = {};
      for (const [name, m] of Object.entries(e.data.meshes)) {
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.BufferAttribute(m.position, 3));
        g.setAttribute('normal', new THREE.BufferAttribute(m.normal, 3));
        if (m.index) g.setIndex(new THREE.BufferAttribute(m.index, 1));
        geoms[name] = g;
      }
      window.__model = { cached: !!e.data.cached, at: performance.now() };
      resolve({ mj: e.data.mj, geoms });
    };
    w.onerror = (e) => { w.terminate(); reject(new Error(e.message || 'worker error')); };
    w.postMessage({ base: new URL(base, location.href).href });
  });
}

async function loadModel(base) {
  if (!modelCache) {
    modelCache = loadInWorker(base).catch(() => (async () => {
      const mj = await (await fetch(base + 'model.json', { cache: 'no-cache' })).json();
      const bin = await (await fetch(base + 'meshes.bin')).arrayBuffer();
      const geoms = {};
      for (const [name, m] of Object.entries(mj.meshes)) {
        const q = new Int16Array(bin, m.voff, m.nverts * 3);
        const pos = new Float32Array(m.nverts * 3);
        for (let i = 0; i < m.nverts; i++) for (let k = 0; k < 3; k++) {
          const span = m.bbox_max[k] - m.bbox_min[k] || 1;
          pos[i * 3 + k] = m.bbox_min[k] + (q[i * 3 + k] + 32768) / 65535 * span;
        }
        const idx = m.index_bytes === 2 ? new Uint16Array(bin, m.ioff, m.ntris * 3) : new Uint32Array(bin, m.ioff, m.ntris * 3);
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        g.setIndex(new THREE.BufferAttribute(idx, 1));
        geoms[name] = toCreasedNormals(g, Math.PI / 6);
      }
      return { mj, geoms };
    })()).catch((e) => { modelCache = null; throw e; });
  }
  return modelCache;
}

const same = (a, b, eps) => a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < eps);

function thermalColor(tempC, torqueOn) {
  if (tempC == null || !Number.isFinite(tempC)) return torqueOn ? '#e89a2c' : '#4c5056';
  if (tempC >= 58) return '#c44a2a';
  if (tempC >= 46) return '#e0703a';
  if (tempC >= 36) return '#e89a2c';
  return '#9bc28a';
}

export class Duck3D {
  constructor(canvas, { view = 'front34', centerX = 0.61, centerY = 0.5, fill = 0.92, pageScroll = false, grid = true } = {}) {
    this.canvas = canvas;
    canvas.__duck3d = this;
    this.frames = 0;
    this.view = view;
    this.centerX = centerX;
    this.centerY = centerY;
    this.fill = fill;
    this.joints = {};
    this.dirty = true;
    this.ready = false;
    this._xray = false;
    this._gridOn = grid;
    this._comOn = false;
    this._thermalOn = false;
    this._gizmoOn = false;
    this._telemetry = null;
    this._groundZ = 0;

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();

    this.camera = new THREE.PerspectiveCamera(30, 1, 0.01, 20);
    this.camera.up.set(0, 0, 1);

    this.trunk = new THREE.Group();
    this.content = new THREE.Group();
    this.trunk.add(this.content);
    this.scene.add(this.trunk);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.enablePan = false;
    this.controls.addEventListener('change', () => { this.dirty = true; });
    this.controls.addEventListener('start', () => { this._snapAt = null; this.onUserMove && this.onUserMove(); });
    this.controls.addEventListener('end', () => { if (this._sel && !this._xray) this._seeThrough(); });
    if (pageScroll) {
      this.controls.enableZoom = false;
      this.controls.touches = { ONE: null, TWO: THREE.TOUCH.DOLLY_ROTATE };
      canvas.style.touchAction = 'pan-y';
    }

    this._onScreen = true;
    if (typeof IntersectionObserver !== 'undefined') {
      this._io = new IntersectionObserver((es) => {
        for (const e of es) this._onScreen = e.isIntersecting;
        if (this._onScreen) this.dirty = true;
      });
      this._io.observe(canvas);
    }

    let down = null;
    canvas.addEventListener('pointerdown', (e) => { down = [e.clientX, e.clientY]; });
    canvas.addEventListener('pointerup', (e) => {
      if (!down || !this.onPick || !this.ready) return;
      const moved = Math.hypot(e.clientX - down[0], e.clientY - down[1]);
      down = null;
      if (moved < 6) {
        const j = this.pick(e.clientX, e.clientY);
        if (j) this.onPick(j);
      }
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!this.onHover || !this.ready) return;
      const j = this.pick(e.clientX, e.clientY);
      this.onHover(j, e.clientX, e.clientY);
    });

    this.servoOf = {};
    this._shellMeshes = [];
    this._resize = new ResizeObserver(() => this.resize());
    this._resize.observe(canvas);
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  async load(base = 'model/') {
    const { mj, geoms } = await loadModel(base);
    if (this._dead) return;
    const mjq = (q) => new THREE.Quaternion(q[1], q[2], q[3], q[0]);
    const objs = [];
    const jointOf = [];
    const root = mj.bodies.find((b) => b.parent < 0);
    if (root) {
      this.trunk.position.set(...root.pos);
      this.content.position.copy(this.trunk.position).negate();
    }
    for (const b of mj.bodies) {
      const node = new THREE.Group();
      node.position.set(...b.pos);
      node.quaternion.copy(mjq(b.quat));
      let inner = node;
      if (b.joint) {
        const pivot = new THREE.Group();
        node.add(pivot);
        inner = pivot;
        this.joints[b.joint.name] = { pivot, axis: new THREE.Vector3(...b.joint.axis).normalize() };
      }
      jointOf.push(b.joint ? b.joint.name : (b.parent >= 0 ? jointOf[b.parent] : null));
      for (const g of b.geoms) {
        const mesh = new THREE.Mesh(geoms[g.mesh], materialFor(g.rgba));
        mesh.userData.jointOf = jointOf[jointOf.length - 1];
        mesh.userData.servo = g.mesh === 'xl330';
        if (mesh.userData.servo && g.servo_joint) mesh.userData.servoJoint = g.servo_joint;
        mesh.position.set(...g.pos);
        mesh.quaternion.copy(mjq(g.quat));
        inner.add(mesh);
        if (!mesh.userData.servo) (this._shellMeshes ||= []).push(mesh);
      }
      objs.push({ inner });
      if (b.parent < 0) this.content.add(node); else objs[b.parent].inner.add(node);
    }
    this._lights();
    this._mapServos();
    this._initEngineeringOverlays();
    this.ready = true;
    this._frame();
    this._updateOverlays();
    this.dirty = true;
  }

  _lights() {
    const key = new THREE.DirectionalLight(0xfff0dc, 2.3); key.position.set(0.35, 0.3, 1.4);
    const fill = new THREE.DirectionalLight(0xe4ecff, 0.8); fill.position.set(0.3, -0.8, 0.35);
    const rim = new THREE.DirectionalLight(0xe89a2c, 1.1); rim.position.set(-0.7, -0.5, 0.6);
    this.scene.add(key, fill, rim, new THREE.HemisphereLight(0xffffff, 0x242320, 0.38));

    const c = document.createElement('canvas'); c.width = c.height = 256;
    const ctx = c.getContext('2d');
    const grd = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
    grd.addColorStop(0, 'rgba(0,0,0,0.55)'); grd.addColorStop(0.5, 'rgba(0,0,0,0.2)'); grd.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = grd; ctx.fillRect(0, 0, 256, 256);
    this.blob = new THREE.Mesh(
      new THREE.PlaneGeometry(0.3, 0.26),
      new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }),
    );
    this.scene.add(this.blob);
  }

  _initEngineeringOverlays() {
    this._gridGroup = new THREE.Group();
    const grid = new THREE.GridHelper(0.6, 12, 0xe0703a, 0x3b3a36);
    grid.rotation.x = Math.PI / 2;
    grid.material.transparent = true;
    grid.material.opacity = 0.42;
    this._gridGroup.add(grid);

    const axes = new THREE.AxesHelper(0.08);
    axes.position.set(-0.26, -0.26, 0.001);
    this._gridGroup.add(axes);
    this._gridGroup.visible = this._gridOn;
    this.scene.add(this._gridGroup);

    // CoM plumb-line + support polygon
    this._comGroup = new THREE.Group();
    const plumbGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -0.2)]);
    this._plumbLine = new THREE.Line(plumbGeo, new THREE.LineBasicMaterial({ color: 0xe89a2c }));
    const ringGeo = new THREE.RingGeometry(0.012, 0.018, 24);
    this._comRing = new THREE.Mesh(ringGeo, new THREE.MeshBasicMaterial({ color: 0xe89a2c, side: THREE.DoubleSide }));
    this._comDot = new THREE.Mesh(new THREE.SphereGeometry(0.008, 12, 12), new THREE.MeshBasicMaterial({ color: 0xe0703a }));
    const polyGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
    this._supportLine = new THREE.Line(polyGeo, new THREE.LineBasicMaterial({ color: 0x9bc28a }));
    this._comGroup.add(this._plumbLine, this._comRing, this._comDot, this._supportLine);
    this._comGroup.visible = this._comOn;
    this.scene.add(this._comGroup);

    // Gravity / IMU attitude gizmo
    this._gizmoGroup = new THREE.Group();
    this._gravArrow = new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(0, 0, 0.14), 0.11, 0xe0703a, 0.025, 0.014);
    const horizonGeo = new THREE.RingGeometry(0.055, 0.058, 36);
    const horizon = new THREE.Mesh(horizonGeo, new THREE.MeshBasicMaterial({ color: 0xf2e6cc, transparent: true, opacity: 0.45, side: THREE.DoubleSide }));
    this._gizmoGroup.add(this._gravArrow, horizon);
    this._gizmoGroup.visible = this._gizmoOn;
    this.scene.add(this._gizmoGroup);

    // Ghost reference pose skeleton
    this._ghostGroup = new THREE.Group();
    this._ghostLine = new THREE.LineSegments(
      new THREE.BufferGeometry(),
      new THREE.LineBasicMaterial({ color: 0xe89a2c, transparent: true, opacity: 0.55 }),
    );
    this._ghostGroup.add(this._ghostLine);
    this._ghostGroup.visible = false;
    this.scene.add(this._ghostGroup);
  }

  _updateOverlays() {
    if (!this.ready) return;
    this.scene.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(this.trunk);
    this._groundZ = box.min.z;
    const center = box.getCenter(new THREE.Vector3());
    if (this._gridGroup) {
      this._gridGroup.position.set(center.x, center.y, this._groundZ + 0.0004);
      this._gridGroup.visible = this._gridOn;
    }
    if (this._comGroup && this._comOn) {
      const com = center.clone();
      this._comDot.position.copy(com);
      this._comRing.position.set(com.x, com.y, this._groundZ + 0.0015);
      this._plumbLine.geometry.setFromPoints([com, new THREE.Vector3(com.x, com.y, this._groundZ + 0.0015)]);
      const lAnk = this.joints.left_ankle?.pivot.getWorldPosition(new THREE.Vector3());
      const rAnk = this.joints.right_ankle?.pivot.getWorldPosition(new THREE.Vector3());
      if (lAnk && rAnk) {
        lAnk.z = this._groundZ + 0.0015;
        rAnk.z = this._groundZ + 0.0015;
        this._supportLine.geometry.setFromPoints([lAnk, rAnk]);
      }
      this._comGroup.visible = true;
    } else if (this._comGroup) {
      this._comGroup.visible = false;
    }
    if (this._gizmoGroup) {
      this._gizmoGroup.position.set(center.x, center.y, this._groundZ + 0.002);
      this._gizmoGroup.visible = this._gizmoOn;
    }
  }

  setGrid(on) { this._gridOn = !!on; if (this._gridGroup) this._gridGroup.visible = this._gridOn; this.dirty = true; }
  setComOverlay(on) { this._comOn = !!on; this._updateOverlays(); this.dirty = true; }
  setImuGizmo(on) { this._gizmoOn = !!on; this._updateOverlays(); this.dirty = true; }

  setXray(on) {
    this._xray = !!on;
    for (const m of this._shellMeshes) {
      m.material.transparent = this._xray;
      m.material.opacity = this._xray ? 0.22 : 1;
      m.material.depthWrite = !this._xray;
      m.material.needsUpdate = true;
    }
    if (!this._xray && this._sel) this._seeThrough();
    this.dirty = true;
  }

  setThermalOverlay(on) {
    this._thermalOn = !!on;
    this._applyServoColors();
    this.dirty = true;
  }

  setServoTelemetry(rows) {
    this._telemetry = Array.isArray(rows) ? rows : null;
    if (this._thermalOn) {
      this._applyServoColors();
      this.dirty = true;
    }
  }

  _applyServoColors() {
    const byJoint = {};
    for (const r of this._telemetry || []) {
      const idx = [20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14].indexOf(r.id);
      if (idx >= 0) byJoint[JOINT_NAMES[idx]] = r;
    }
    for (const [name, mesh] of Object.entries(this.servoOf)) {
      if (mesh === this._sel) continue;
      if (this._thermalOn && byJoint[name]) {
        const r = byJoint[name];
        mesh.material.color.set(thermalColor(r.temp_c, !!r.torque));
      } else if (mesh.userData.baseColor) {
        mesh.material.color.copy(mesh.userData.baseColor);
      }
    }
  }

  setGhost(arr) {
    if (!this.ready || !this._ghostGroup) return;
    if (!Array.isArray(arr) || arr.length < 15) {
      this._ghostGroup.visible = false;
      this.dirty = true;
      return;
    }
    this.scene.updateMatrixWorld(true);
    const pts = [];
    const posOf = (name) => this.joints[name]?.pivot.getWorldPosition(new THREE.Vector3());
    const pairs = [
      ['left_hip_yaw', 'left_hip_roll'], ['left_hip_roll', 'left_hip_pitch'], ['left_hip_pitch', 'left_knee'], ['left_knee', 'left_ankle'],
      ['right_hip_yaw', 'right_hip_roll'], ['right_hip_roll', 'right_hip_pitch'], ['right_hip_pitch', 'right_knee'], ['right_knee', 'right_ankle'],
      ['left_hip_yaw', 'right_hip_yaw'], ['left_hip_yaw', 'neck_pitch'], ['right_hip_yaw', 'neck_pitch'],
      ['neck_pitch', 'head_pitch'], ['head_pitch', 'head_yaw'], ['head_yaw', 'head_roll'], ['head_roll', 'mouth'],
    ];
    for (const [a, b] of pairs) {
      const pa = posOf(a), pb = posOf(b);
      if (pa && pb) { pts.push(pa, pb); }
    }
    this._ghostLine.geometry.setFromPoints(pts);
    this._ghostGroup.visible = pts.length > 0;
    this.dirty = true;
  }

  setJoints(arr) {
    if (!this.ready || !Array.isArray(arr)) return;
    if (this._lastJoints && same(arr, this._lastJoints, 1e-4)) return;
    this._lastJoints = arr.slice();
    arr.forEach((rad, i) => {
      const j = this.joints[JOINT_NAMES[i]];
      if (j && typeof rad === 'number' && isFinite(rad)) j.pivot.quaternion.setFromAxisAngle(j.axis, rad);
    });
    this._updateOverlays();
    this.dirty = true;
  }

  setGravity(g) {
    if (!this.ready) return;
    if (!g) {
      if (this._lastGravity) {
        this._lastGravity = null;
        this.trunk.quaternion.identity();
        if (this._gravArrow) this._gravArrow.setDirection(new THREE.Vector3(0, 0, -1));
        this._updateOverlays();
        this.dirty = true;
      }
      return;
    }
    if (this._lastGravity && same(g, this._lastGravity, 1e-4)) return;
    this._lastGravity = g.slice();
    const v = new THREE.Vector3(...g);
    if (v.lengthSq() < 1e-6) return;
    const vn = v.clone().normalize();
    this.trunk.quaternion.setFromUnitVectors(vn, new THREE.Vector3(0, 0, -1));
    if (this._gravArrow) this._gravArrow.setDirection(vn);
    this._updateOverlays();
    this.dirty = true;
  }

  setView(view) { this.view = view; this._frame(); }

  _mapServos() {
    this.scene.updateMatrixWorld(true);
    const servos = [];
    this.content.traverse((o) => { if (o.isMesh && o.userData.servo) servos.push(o); });
    const usedM = new Set(), usedJ = new Set();
    const bind = (m, name) => {
      usedM.add(m); usedJ.add(name);
      m.userData.joint = name;
      m.userData.baseColor = m.material.color.clone();
      this.servoOf[name] = m;
    };
    this.servoOf = {};
    for (const m of servos) {
      const name = m.userData.servoJoint;
      if (!name) continue;
      if (!this.joints[name] || usedJ.has(name)) throw new Error(`Invalid or duplicate servo mesh binding: ${name}`);
      bind(m, name);
    }
    const pairs = [];
    for (const m of servos) {
      const c = new THREE.Box3().setFromObject(m).getCenter(new THREE.Vector3());
      for (const [name, j] of Object.entries(this.joints)) {
        pairs.push([c.distanceTo(j.pivot.getWorldPosition(new THREE.Vector3())), m, name]);
      }
    }
    pairs.sort((a, b) => a[0] - b[0]);
    for (const [, m, name] of pairs) {
      if (usedM.has(m) || usedJ.has(name)) continue;
      bind(m, name);
    }
  }

  pick(clientX, clientY) {
    const r = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2((clientX - r.left) / r.width * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    const servo = ray.intersectObjects(Object.values(this.servoOf), false)[0];
    if (servo) return servo.object.userData.joint;
    const meshes = [];
    this.content.traverse((o) => { if (o.isMesh) meshes.push(o); });
    const hit = ray.intersectObjects(meshes, false)[0];
    return hit ? hit.object.userData.jointOf || null : null;
  }

  _seeThrough() {
    if (this._xray) return;
    for (const m of this._ghosted || []) {
      m.material.transparent = false; m.material.opacity = 1; m.material.depthWrite = true; m.material.needsUpdate = true;
    }
    this._ghosted = [];
    if (!this._sel) return;
    this.scene.updateMatrixWorld(true);
    const c = new THREE.Box3().setFromObject(this._sel).getCenter(new THREE.Vector3());
    const dir = c.clone().sub(this.camera.position);
    const dist = dir.length();
    const ray = new THREE.Raycaster(this.camera.position, dir.normalize(), 0, dist);
    const meshes = [];
    this.content.traverse((o) => { if (o.isMesh && o !== this._sel) meshes.push(o); });
    for (const h of ray.intersectObjects(meshes, false)) {
      if (h.distance < dist - 0.004 && !this._ghosted.includes(h.object)) {
        const m = h.object;
        m.material.transparent = true; m.material.opacity = 0.22; m.material.depthWrite = false; m.material.needsUpdate = true;
        this._ghosted.push(m);
      }
    }
    this.dirty = true;
  }

  select(name, color) {
    if (this._sel && this._sel.userData.baseColor) this._sel.material.color.copy(this._sel.userData.baseColor);
    this._sel = name ? this.servoOf[name] || null : null;
    if (this._sel) this._sel.material.color.set(color || '#e0703a');
    if (this._thermalOn) this._applyServoColors();
    this._seeThrough();
    this.dirty = true;
  }

  resetView() { this._snapAt = performance.now(); this.dirty = true; }

  _frame() {
    if (!this.ready) return;
    this.scene.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(this.trunk);
    const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
    this.blob.position.set(center.x, center.y, box.min.z + 0.0008);
    const [azDeg, elDeg] = VIEWS[this.view] || VIEWS.front34;
    const az = THREE.MathUtils.degToRad(azDeg), el = THREE.MathUtils.degToRad(elDeg);
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const dist = (size.z / this.fill) / (2 * Math.tan(fov / 2));
    const look = new THREE.Vector3(center.x, center.y, box.min.z + size.z * 0.5);
    this.home = {
      pos: new THREE.Vector3(look.x + dist * Math.cos(el) * Math.cos(az), look.y + dist * Math.cos(el) * Math.sin(az), look.z + dist * Math.sin(el)),
      target: look,
    };
    this.camera.position.copy(this.home.pos);
    this.controls.target.copy(look);
    this.controls.update();
    this._applyOffset();
    this.dirty = true;
  }

  _applyOffset() {
    const w = this.canvas.clientWidth || 1, h = this.canvas.clientHeight || 1;
    this.camera.aspect = w / h;
    this.camera.setViewOffset(w, h, (0.5 - this.centerX) * w, (0.5 - this.centerY) * h, w, h);
    this.camera.updateProjectionMatrix();
  }

  resize() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this._applyOffset();
    this.dirty = true;
  }

  _loop(now) {
    if (this._dead) return;
    if (this._snapAt && now > this._snapAt && this.home) {
      this.camera.position.lerp(this.home.pos, 0.18);
      this.controls.target.lerp(this.home.target, 0.18);
      if (this.camera.position.distanceTo(this.home.pos) < 1e-4) this._snapAt = null;
      this.dirty = true;
    }
    if (this.controls.update()) this.dirty = true;
    if (this.dirty && this.ready && !document.hidden && this._onScreen && this.canvas.offsetParent !== null) {
      this.renderer.render(this.scene, this.camera);
      this.dirty = false;
      this.frames++;
    }
    requestAnimationFrame(this._loop);
  }

  dispose() {
    this._dead = true;
    this._resize.disconnect();
    if (this._io) this._io.disconnect();
    this.controls.dispose();
    this.scene.traverse((o) => {
      if (o.material) { o.material.map?.dispose(); o.material.dispose(); }
    });
    this.scene.environment?.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }
}
