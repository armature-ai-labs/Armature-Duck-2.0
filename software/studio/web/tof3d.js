// 3D Volumetric 8x8 ToF Depth Frustum Visualizer (Armature x Microduck Studio)
import * as THREE from './vendor/three/three.module.js';
import { OrbitControls } from './vendor/three/addons/OrbitControls.js';

const FOV_DEG = 45;

function depthColor(mm) {
  if (!Number.isFinite(mm) || mm <= 0) return new THREE.Color(0x3a3935);
  const t = Math.max(0, Math.min(1, (mm - 80) / 1400));
  // Near = hot brick/amber (#e0703a -> #e89a2c), Far = moss/slate (#9bc28a -> #4c6b5a)
  const c = new THREE.Color();
  if (t < 0.35) c.lerpColors(new THREE.Color('#c44a2a'), new THREE.Color('#e89a2c'), t / 0.35);
  else c.lerpColors(new THREE.Color('#e89a2c'), new THREE.Color('#9bc28a'), (t - 0.35) / 0.65);
  return c;
}

export class Tof3D {
  constructor(canvas) {
    this.canvas = canvas;
    this.dirty = true;
    this._dead = false;
    this._onScreen = true;

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setClearColor(0x000000, 0);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.02, 12);
    this.camera.position.set(1.25, 0.9, 1.4);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.target.set(0, 0, -0.55);
    this.controls.addEventListener('change', () => { this.dirty = true; });

    const grid = new THREE.GridHelper(2.0, 10, 0xe0703a, 0x33322e);
    grid.position.set(0, -0.35, -0.8);
    this.scene.add(grid);

    // Sensor origin marker
    const origin = new THREE.Mesh(
      new THREE.BoxGeometry(0.06, 0.04, 0.03),
      new THREE.MeshBasicMaterial({ color: 0xe89a2c }),
    );
    this.scene.add(origin);

    // 64 instanced voxel tiles + 64 ray lines
    const tileGeo = new THREE.PlaneGeometry(0.055, 0.055);
    const tileMat = new THREE.MeshBasicMaterial({ side: THREE.DoubleSide });
    this.tiles = new THREE.InstancedMesh(tileGeo, tileMat, 64);
    this.scene.add(this.tiles);

    const rayPos = new Float32Array(64 * 2 * 3);
    const rayGeo = new THREE.BufferGeometry();
    rayGeo.setAttribute('position', new THREE.BufferAttribute(rayPos, 3));
    this.rays = new THREE.LineSegments(rayGeo, new THREE.LineBasicMaterial({ color: 0xe0703a, transparent: true, opacity: 0.25 }));
    this.scene.add(this.rays);

    if (typeof IntersectionObserver !== 'undefined') {
      this._io = new IntersectionObserver((es) => {
        for (const e of es) this._onScreen = e.isIntersecting;
        if (this._onScreen) this.dirty = true;
      });
      this._io.observe(canvas);
    }
    this._resize = new ResizeObserver(() => this.resize());
    this._resize.observe(canvas);
    this._loop = this._loop.bind(this);
    this.resize();
    requestAnimationFrame(this._loop);
  }

  setGrid(cellsMm) {
    if (!Array.isArray(cellsMm) || cellsMm.length < 64) return;
    const dummy = new THREE.Object3D();
    const rayPos = this.rays.geometry.attributes.position.array;
    const halfFov = THREE.MathUtils.degToRad(FOV_DEG / 2);
    const maxTan = Math.tan(halfFov);

    for (let idx = 0; idx < 64; idx++) {
      const r = Math.floor(idx / 8);
      const c = idx % 8;
      const mm = cellsMm[idx];
      const valid = Number.isFinite(mm) && mm > 0;
      const z = valid ? Math.min(2.2, Math.max(0.06, mm / 1000)) : 0.06;
      const nx = ((c - 3.5) / 3.5) * maxTan;
      const ny = ((3.5 - r) / 3.5) * maxTan;
      const x = nx * z;
      const y = ny * z;

      dummy.position.set(x, y, -z);
      const s = valid ? Math.max(0.4, z * 1.1) : 0.001;
      dummy.scale.set(s, s, s);
      dummy.updateMatrix();
      this.tiles.setMatrixAt(idx, dummy.matrix);
      this.tiles.setColorAt(idx, depthColor(mm));

      const off = idx * 6;
      rayPos[off] = 0; rayPos[off + 1] = 0; rayPos[off + 2] = 0;
      rayPos[off + 3] = x; rayPos[off + 4] = y; rayPos[off + 5] = -z;
    }
    this.tiles.instanceMatrix.needsUpdate = true;
    if (this.tiles.instanceColor) this.tiles.instanceColor.needsUpdate = true;
    this.rays.geometry.attributes.position.needsUpdate = true;
    this.dirty = true;
  }

  resize() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
    this.dirty = true;
  }

  _loop() {
    if (this._dead) return;
    if (this.controls.update()) this.dirty = true;
    if (this.dirty && !document.hidden && this._onScreen && this.canvas.offsetParent !== null) {
      this.renderer.render(this.scene, this.camera);
      this.dirty = false;
    }
    requestAnimationFrame(this._loop);
  }

  dispose() {
    this._dead = true;
    this._resize.disconnect();
    if (this._io) this._io.disconnect();
    this.controls.dispose();
    this.tiles.geometry.dispose();
    this.tiles.material.dispose();
    this.rays.geometry.dispose();
    this.rays.material.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }
}
