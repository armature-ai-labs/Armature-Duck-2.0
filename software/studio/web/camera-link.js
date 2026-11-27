// LAN mediad consumer. This client never opens a robot control channel or sends control methods.
const MAX_MESSAGE = 256 * 1024;
const validId = id => typeof id === 'string' && id.length > 0 && id.length <= 256;

export class CameraLink {
  constructor(options = {}) {
    this.video = options.video;
    this.onState = options.onState || (() => {});
    this.WebSocket = options.WebSocket || globalThis.WebSocket;
    this.RTCPeerConnection = options.RTCPeerConnection || globalThis.RTCPeerConnection;
    this.MediaStream = options.MediaStream || globalThis.MediaStream;
    this.location = options.location || globalThis.location;
    this.now = options.now || (() => performance.now());
    this.setTimeout = options.setTimeout || globalThis.setTimeout.bind(globalThis);
    this.clearTimeout = options.clearTimeout || globalThis.clearTimeout.bind(globalThis);
    this.generation = 0;
    this.state = { state: 'stopped', error: null, width: null, height: null, frames: 0, metadata: null, producer: null };
  }

  update(patch) { this.state = { ...this.state, ...patch }; this.onState(this.state); }

  _attach(ws, generation, label) {
    this.chain = Promise.resolve();
    this.queuedMessages = 0;
    ws.onopen = () => { if (this.current(generation)) this.update({ state: 'signaling' }); };
    ws.onmessage = event => {
      if (!this.current(generation)) return;
      if (typeof event.data !== 'string' || event.data.length > MAX_MESSAGE || ++this.queuedMessages > 256) {
        this.fail('Camera signaling payload exceeded limit.'); return;
      }
      this.chain = this.chain.then(async () => {
        if (this.current(generation)) await this.message(JSON.parse(event.data), generation);
      }).catch(error => { if (this.current(generation)) this.fail(`Camera negotiation failed: ${error.message || 'invalid payload'}`); })
        .finally(() => { if (this.current(generation)) this.queuedMessages--; });
    };
    ws.onerror = () => { if (this.current(generation)) this.fail(`Cannot reach camera signaling ${label}; check mediad and LAN.`); };
    ws.onclose = () => { if (this.current(generation)) this.fail('Camera signaling disconnected; reconnect to retry.'); };
    this.lastFrameAt = this.startedAt = this.now();
    this.watch(generation);
  }

  start(port = 8443, path = null) {
    if (path) return this.startProxy(path);
    this.stop();
    const generation = this.generation;
    this.state = { state: 'connecting', error: null, width: null, height: null, frames: 0, metadata: null, producer: null };
    this.update({});
    if (!this.WebSocket || !this.RTCPeerConnection) return this.fail('WebRTC preview is not supported in this browser.');
    if (!Number.isInteger(port) || port < 1 || port > 65535) return this.fail('Invalid camera signaling port.');
    try {
      const hostname = this.location.hostname;
      const host = hostname.includes(':') && !hostname.startsWith('[') ? `[${hostname}]` : hostname;
      const scheme = this.location.protocol === 'https:' ? 'wss' : 'ws';
      this._attach(this.ws = new this.WebSocket(`${scheme}://${host}:${port}`), generation, `port ${port}`);
    } catch (error) { this.fail(`Camera connection failed: ${error.message}`); }
  }

  startProxy(path = '/media-ws') {
    this.stop();
    const generation = this.generation;
    this.state = { state: 'connecting', error: null, width: null, height: null, frames: 0, metadata: null, producer: null };
    this.update({});
    if (!this.WebSocket || !this.RTCPeerConnection) return this.fail('WebRTC preview is not supported in this browser.');
    if (typeof path !== 'string' || !path.startsWith('/')) return this.fail('Invalid camera signaling path.');
    try {
      const scheme = this.location.protocol === 'https:' ? 'wss' : 'ws';
      this._attach(this.ws = new this.WebSocket(`${scheme}://${this.location.host}${path}`), generation, path);
    } catch (error) { this.fail(`Camera connection failed: ${error.message}`); }
  }

  current(generation, pc = null) { return this.generation === generation && !!this.ws && (!pc || this.pc === pc); }
  send(message) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(message)); }

  async message(msg, generation) {
    if (!msg || typeof msg !== 'object') throw Error('Invalid signaling payload');
    if (msg.type === 'welcome') { this.send({ type: 'list' }); return; }
    if (msg.type === 'list') {
      if (this.sessionId || this.starting) return;
      if (!Array.isArray(msg.producers) || msg.producers.length > 64) throw Error('Invalid producer list');
      const producers = msg.producers.filter(p => validId(p?.id) && p.meta &&
        typeof p.meta.release === 'string' && /^\d+$/.test(String(p.meta.api_version || '')) &&
        (!p.meta.kind || p.meta.kind === 'microduck'));
      if (producers.length !== 1) {
        this.fail(producers.length ? 'Multiple video producers found; cannot disambiguate.' : 'No local video producer found; check mediad.'); return;
      }
      this.starting = true;
      this.update({ producer: producers[0].meta });
      this.send({ type: 'startSession', peerId: producers[0].id });
      return;
    }
    if (msg.type === 'sessionStarted') {
      if (!this.starting || this.sessionId) return;
      if (!validId(msg.sessionId)) throw Error('Invalid sessionId');
      this.starting = false;
      this.sessionId = msg.sessionId;
      this.update({ state: 'negotiating' });
      this.createPeer(generation);
      return;
    }
    if (msg.type === 'peer') {
      const pc = this.pc;
      if (!pc || msg.sessionId !== this.sessionId) return;
      if (msg.sdp) {
        if (msg.sdp.type !== 'offer' || typeof msg.sdp.sdp !== 'string' || msg.sdp.sdp.length > 128 * 1024) throw Error('Invalid SDP');
        await pc.setRemoteDescription({ type: 'offer', sdp: msg.sdp.sdp });
        if (!this.current(generation, pc)) return;
        for (const transceiver of pc.getTransceivers?.() || []) {
          if (transceiver.receiver?.track?.kind === 'audio') transceiver.direction = 'inactive';
        }
        const answer = await pc.createAnswer();
        if (!this.current(generation, pc)) return;
        await pc.setLocalDescription(answer);
        if (!this.current(generation, pc)) return;
        this.send({ type: 'peer', sessionId: this.sessionId, sdp: { type: 'answer', sdp: answer.sdp } });
        for (const ice of this.pendingIce.splice(0)) {
          if (!this.current(generation, pc)) return;
          await pc.addIceCandidate(ice);
        }
      } else if (msg.ice) {
        const ice = msg.ice;
        if (typeof ice.candidate !== 'string' || ice.candidate.length > 8192 ||
          !Number.isInteger(ice.sdpMLineIndex) || ice.sdpMLineIndex < 0 || ice.sdpMLineIndex > 32 || ++this.iceCount > 256) throw Error('Invalid ICE');
        const candidate = { candidate: ice.candidate, sdpMLineIndex: ice.sdpMLineIndex };
        if (pc.remoteDescription) await pc.addIceCandidate(candidate);
        else this.pendingIce.push(candidate);
      }
      return;
    }
    if (msg.type === 'endSession' && this.sessionId && msg.sessionId === this.sessionId) this.fail('Camera ended the video session; reconnect.');
    else if (msg.type === 'sessionRejected') this.fail('Camera rejected the video session; try again.');
    else if (msg.type === 'error') this.fail('Camera signaling reported an error; reconnect.');
  }

  createPeer(generation) {
    const pc = this.pc = new this.RTCPeerConnection({ iceServers: [] });
    this.pendingIce = []; this.iceCount = 0; this.tracks = new Set();
    this.lastPlaybackTime = -1; this.lastFrameCount = 0; this.hasTrack = false;
    pc.onicecandidate = event => {
      if (!this.current(generation, pc) || !event.candidate) return;
      const ice = event.candidate;
      this.send({ type: 'peer', sessionId: this.sessionId, ice: { candidate: ice.candidate, sdpMLineIndex: ice.sdpMLineIndex } });
    };
    pc.onconnectionstatechange = () => {
      if (!this.current(generation, pc)) return;
      if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) this.fail('Video connection lost; reconnect.');
    };
    pc.ontrack = event => {
      if (!this.current(generation, pc)) { event.track.stop(); return; }
      if (event.track.kind !== 'video') { event.track.stop(); return; }
      this.tracks.add(event.track);
      event.track.onended = () => { if (this.current(generation, pc)) this.fail('Camera video track ended.'); };
      this.hasTrack = true;
      this.lastFrameAt = this.now();
      this.video.srcObject = event.streams?.[0] || new this.MediaStream([event.track]);
      this.update({ state: 'receiving' });
      if (this.playing) this.video.removeEventListener('playing', this.playing);
      this.playing = () => { if (this.current(generation, pc)) this.samplePlayback(); };
      this.video.addEventListener('playing', this.playing);
      this.video.play()?.catch(() => { if (this.current(generation, pc)) this.fail('Playback did not start; click Reconnect.'); });
    };
    pc.ondatachannel = event => {
      if (!this.current(generation, pc) || this.channel) { event.channel.close(); return; }
      const channel = this.channel = event.channel;
      let requested = false;
      const requestId = `video-info-${generation}`;
      channel.onopen = () => {
        if (!this.current(generation, pc) || requested || channel.readyState !== 'open') return;
        requested = true;
        channel.send(JSON.stringify({ jsonrpc: '2.0', id: requestId, method: 'media.video', params: {} }));
      };
      channel.onmessage = event => {
        if (!this.current(generation, pc) || typeof event.data !== 'string' || event.data.length > 65536) return;
        let msg;
        try { msg = JSON.parse(event.data); } catch { return; }
        if (!msg || typeof msg !== 'object') return;
        const info = msg.id === requestId ? msg.result : msg.method === 'media.video' ? msg.params : null;
        if (info && Number.isInteger(info.width) && info.width > 0 && info.width <= 16384 &&
          Number.isInteger(info.height) && info.height > 0 && info.height <= 16384 && [0, 90, 180, 270].includes(info.rotate ?? 0)) {
          this.update({ metadata: { width: info.width, height: info.height, rotate: info.rotate ?? 0 } });
        }
      };
      if (channel.readyState === 'open') channel.onopen();
    };
  }

  samplePlayback() {
    if (!this.hasTrack || !this.video || this.video.paused) return;
    const quality = this.video.getVideoPlaybackQuality?.();
    const hasCounter = typeof quality?.totalVideoFrames === 'number';
    const count = hasCounter ? quality.totalVideoFrames : 0;
    const time = this.video.currentTime || 0;
    if (this.video.videoWidth > 0 && this.video.videoHeight > 0 &&
      (hasCounter ? count > this.lastFrameCount : time > Math.max(0, this.lastPlaybackTime))) {
      this.lastFrameAt = this.now();
      this.update({ state: 'playing', width: this.video.videoWidth, height: this.video.videoHeight,
        frames: hasCounter ? count : null });
    }
    this.lastFrameCount = count; this.lastPlaybackTime = time;
  }

  watch(generation) {
    this.timer = this.setTimeout(() => {
      if (!this.current(generation)) return;
      this.samplePlayback();
      if (this.now() - this.lastFrameAt >= 10000) this.fail(this.hasTrack ? 'Video frame timeout; stream stalled.' : 'Timed out waiting for camera frames.');
      else this.watch(generation);
    }, 1000);
  }

  fail(error) { this.stop('error'); this.update({ error }); }

  stop(state = 'stopped') {
    this.generation++;
    this.clearTimeout(this.timer); this.timer = null;
    const ws = this.ws; this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      if (ws.readyState === 1 && this.sessionId) {
        try { ws.send(JSON.stringify({ type: 'endSession', sessionId: this.sessionId })); } catch { /* Closing lost socket. */ }
      }
      try { ws.close(); } catch { /* Already closed. */ }
    }
    if (this.channel) {
      this.channel.onopen = this.channel.onmessage = this.channel.onclose = this.channel.onerror = null;
      try { this.channel.close(); } catch { /* Already closed. */ } this.channel = null;
    }
    if (this.pc) {
      this.pc.ontrack = this.pc.onicecandidate = this.pc.onconnectionstatechange = this.pc.ondatachannel = null;
      this.pc.close(); this.pc = null;
    }
    for (const track of this.tracks || []) { track.onended = null; track.stop(); }
    this.tracks = new Set();
    if (this.video) {
      if (this.playing) this.video.removeEventListener('playing', this.playing);
      for (const track of this.video.srcObject?.getTracks() || []) track.stop();
      this.video.pause(); this.video.srcObject = null;
    }
    this.playing = null; this.sessionId = null; this.starting = false; this.hasTrack = false; this.pendingIce = [];
    this.update({ state, error: null, width: null, height: null, frames: 0, metadata: null, producer: null });
  }
}
