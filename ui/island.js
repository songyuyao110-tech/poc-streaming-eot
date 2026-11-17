// The island: one glass orb that is the agent's presence, and the button that starts and ends the call. A single fragment shader draws the pill, the light band
// inside it (four coloured lobes added like light, then tone-mapped with 1 - exp(-x)), the glow it throws outside,
// the thinking dots and the connecting shimmer. The shape is two springs (half-width, half-height); everything else
// eases. Falls back to Canvas2D without WebGL2, and slows to near-still under prefers-reduced-motion.
(function () {
  'use strict';

  // radius in CSS px, and how the light behaves, per state; the orb beats with whoever is speaking
  const STATES = {
    idle:       { w: 40, energy: .42, base: .10, speed: .35, src: null,    pal: 'warm' },
    connecting: { w: 34, energy: .10, base: .04, speed: .5,  src: null,    pal: 'cool', shimmer: 1 },
    listening:  { w: 50, energy: .80, base: .07, speed: .75, src: 'mic',   pal: 'cool' },
    talking:    { w: 58, energy: 1.0, base: .12, speed: 1.2, src: 'mic',   pal: 'cool' },
    thinking:   { w: 40, energy: 0,   base: 0,   speed: .6,  src: null,    pal: 'warm', dots: 1 },
    speaking:   { w: 62, energy: 1.0, base: .12, speed: 1.1, src: 'agent', pal: 'warm' },
    held:       { w: 58, energy: .85, base: .22, speed: .03, src: 'hold',  pal: 'warm', dim: 1 },
    yielded:    { w: 46, energy: .45, base: .05, speed: .6,  src: 'mic',   pal: 'cool' },
    ended:      { w: 22, energy: 0,   base: 0,   speed: .3,  src: null,    pal: 'warm' },
  };
  const COOL = [[.20, .78, 1.0], [.28, .42, 1.0], [.25, .95, .80], [.62, .45, 1.0]];

  const VERT = `#version 300 es
in vec2 a; void main(){ gl_Position = vec4(a, 0., 1.); }`;
  const FRAG = `#version 300 es
precision highp float;
uniform vec2 u_res; uniform float u_dpr, u_t, u_tt;
uniform vec2 u_half; uniform float u_level, u_energy, u_dots, u_shimmer, u_dim;
uniform vec3 u_c[4];
out vec4 o;

float sq(float x){ return x * x; }
float sd(vec2 p){ float r = min(u_half.x, u_half.y); vec2 q = abs(p) - u_half + r; return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - r; }

vec3 band(vec2 p){
  float x = p.x / u_half.x; vec3 col = vec3(0.);
  float amp = u_half.y * (.08 + .50 * u_level), thick = u_half.y * (.18 + .24 * u_level);
  for (int i = 0; i < 4; i++) {
    float f = float(i), ph = f * 1.7;
    float wave = sin(x * 2.2 + u_t * (1.1 + f * .23) + ph) * .6 + sin(x * 4.7 - u_t * (.8 + f * .31) + ph * 2.) * .4;
    float cx = sin(u_t * (.35 + f * .11) + ph * 1.3) * .6;
    float lobe = exp(-sq((x - cx) / (.5 + .35 * u_level)));
    float dy = (p.y - wave * amp) / thick;
    col += u_c[i] * exp(-dy * dy) * lobe;
  }
  col *= u_energy * (.30 + .85 * u_level);
  float l = dot(col, vec3(.3, .6, .1));
  return mix(col, vec3(l), u_dim * .65) * mix(1., .45, u_dim);
}

void main(){
  vec2 p = (gl_FragCoord.xy - .5 * u_res) / u_dpr;
  float d = sd(p);
  vec2 e = vec2(1., 0.);
  vec2 n = normalize(vec2(sd(p + e.xy) - sd(p - e.xy), sd(p + e.yx) - sd(p - e.yx)) + 1e-5);
  float inside = smoothstep(.75, -.75, d);

  // inside: the band, pulled outward near the rim so its colour wraps along the glass edge
  float rk = smoothstep(-u_half.y * .9, 0., d);
  vec3 light = band(p - n * rk * rk * u_half.y * .5) * (1. + rk * .6);
  for (int i = 0; i < 3; i++) {
    float r = length(p - vec2((float(i) - 1.) * 15., 0.));
    float pulse = .25 + .75 * pow(.5 + .5 * sin(u_tt * 4.2 - float(i) * .95), 2.);
    light += mix(vec3(1.), u_c[i], .35) * (smoothstep(4.4, 3.2, r) * pulse * 1.4 + exp(-r * r / 60.) * .3 * pulse) * u_dots;
  }
  float sx = fract(u_tt * .55) * 3.2 - 1.6;
  light += vec3(.5, .62, .85) * exp(-sq((p.x / u_half.x - sx) / .3)) * u_shimmer * .5;
  vec3 lit = 1. - exp(-light * 1.25);

  float top = smoothstep(-u_half.y, u_half.y, p.y);
  vec3 body = mix(vec3(.052, .058, .080), vec3(.012, .012, .018), top) + vec3(.05, .06, .09) * smoothstep(-10., 0., d) * .6;
  float rl = exp(-sq((d + 1.4) / 1.3));
  vec3 rim = vec3(.78, .84, 1.) * rl * (.12 + .30 * max(n.y, 0.) + .10 * max(-n.y, 0.)) + lit * rl * .6;
  vec3 inCol = body + lit + rim;

  // outside: the glow carries the colour of the nearest part of the band, over a soft shadow
  float od = max(d, 0.);
  vec3 g = band(p - n * (od + u_half.y * .55));
  float R = 12. + 22. * u_level;
  vec2 border = (.5 * u_res - abs(gl_FragCoord.xy - .5 * u_res)) / u_dpr;               // fade to nothing before the canvas edge
  float fade = smoothstep(0., 70., border.x) * smoothstep(0., 36., border.y);
  vec3 glow = ((1. - exp(-g * 1.2)) * exp(-od / R) * .50 + (1. - exp(-g)) * exp(-od / (R * 3.)) * .10) * fade;
  float sh = exp(-od / 16.) * .35 * fade;
  vec4 outCol = vec4(glow, clamp(max(max(glow.r, glow.g), glow.b) + sh, 0., 1.));

  o = mix(outCol, vec4(inCol, 1.), inside);
}`;

  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return [.54, .70, 1];
    const v = parseInt(m[1], 16);
    return [(v >> 16) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
  }
  function rotateHue(c, deg) {                                // rotate around the grey axis
    const a = deg * Math.PI / 180, cs = Math.cos(a), sn = Math.sin(a), k = (1 - cs) / 3, s = sn / Math.sqrt(3);
    const r = c[0] * (cs + k) + c[1] * (k - s) + c[2] * (k + s);
    const g = c[0] * (k + s) + c[1] * (cs + k) + c[2] * (k - s);
    const b = c[0] * (k - s) + c[1] * (k + s) + c[2] * (cs + k);
    return [r, g, b].map((x) => Math.min(1, Math.max(0, x)));
  }

  class Island {
    constructor(canvas) {
      this.canvas = canvas;
      this.state = 'idle';
      this.levels = { mic: 0, agent: 0 };                     // set from outside every frame, 0..1
      this.reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
      this.v = { w: 40, h: 40, vw: 0, vh: 0, level: 0, energy: 0, dots: 0, shimmer: 0, dim: 0, t: 0, tt: 0 };
      this.warm = this._warm('#8ab4ff');
      this.colors = this.warm.map((c) => c.slice());
      this.last = performance.now();
      this.onframe = null;
      this._initGL() || this._init2D();
      new ResizeObserver(() => this._resize()).observe(canvas);
      this._resize();
      const loop = (now) => { this._frame(now); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    setState(name) { if (STATES[name]) this.state = name; }
    setAccent(hex) { this.warm = this._warm(hex); }
    _warm(hex) { const c = hexToRgb(hex); return [c, rotateHue(c, -35), [.62, .45, 1], [1, .5, .72]]; }

    _initGL() {
      const gl = this.canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: false });
      if (!gl) return false;
      const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
      const prog = gl.createProgram();
      gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG)); gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { console.warn('island shader:', gl.getProgramInfoLog(prog)); return false; }
      gl.useProgram(prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(prog, 'a'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      this.gl = gl; this.u = {};
      for (const n of ['u_res', 'u_dpr', 'u_t', 'u_tt', 'u_half', 'u_level', 'u_energy', 'u_dots', 'u_shimmer', 'u_dim', 'u_c']) this.u[n] = gl.getUniformLocation(prog, n);
      this.canvas.addEventListener('webglcontextlost', (e) => { e.preventDefault(); this.gl = null; this._init2D(); });
      return true;
    }
    _init2D() { this.c2 = this.canvas.getContext('2d'); return !!this.c2; }

    _resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2), r = this.canvas.getBoundingClientRect();
      this.dpr = dpr; this.cw = r.width; this.ch = r.height;
      const w = Math.max(2, Math.round(r.width * dpr)), h = Math.max(2, Math.round(r.height * dpr));
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w; this.canvas.height = h;
        if (this.gl) this._drawGL(); else if (this.c2) this._draw2D();                       // resizing clears the canvas: repaint in the same frame
      }
    }

    _frame(now) {
      const dt = Math.min(.05, (now - this.last) / 1000); this.last = now;
      if (this.onframe) this.onframe(now, dt);
      const s = STATES[this.state], v = this.v;
      const fit = Math.min(1, (this.cw - 40) / 180, (this.ch - 30) / 120);                   // shrink on narrow screens
      const breathe = this.state === 'idle' && !this.reduced ? (this.hot ? 1.1 : 1) + .04 * Math.sin(now / 1000 * 1.6) : 1;
      const beat = this.reduced ? 1 : 1 + .2 * v.level;                                      // the pulse: louder is larger
      const tw = s.w * fit * breathe * beat, th = tw;
      if (this.reduced) { v.w += (tw - v.w) * Math.min(1, dt * 14); v.h += (th - v.h) * Math.min(1, dt * 14); }
      else {                                                                                 // a slightly springy shape
        const K = 150, D = 17;
        v.vw += (K * (tw - v.w) - D * v.vw) * dt; v.w += v.vw * dt;
        v.vh += (K * (th - v.h) - D * v.vh) * dt; v.h += v.vh * dt;
      }
      v.w = Math.max(8, v.w); v.h = Math.max(6, Math.min(v.h, v.w));

      let target = s.base;
      if (s.src === 'mic') target += this.levels.mic;
      else if (s.src === 'agent') target += this.levels.agent;
      else if (s.src === 'hold') target = Math.max(s.base, this._heldLevel || 0);
      if (this.state === 'idle') target += .04 * Math.sin(now / 1000 * 1.3);
      target = Math.min(1, Math.max(0, target));
      v.level += (target - v.level) * Math.min(1, dt * (target > v.level ? 18 : 6));         // fast attack, slow release
      if (this.state === 'speaking') this._heldLevel = v.level;
      const ease = (k, to, rate) => { v[k] += (to - v[k]) * Math.min(1, dt * rate); };
      ease('energy', s.energy, 5); ease('dots', s.dots || 0, 7); ease('shimmer', s.shimmer || 0, 5); ease('dim', s.dim || 0, 9);
      const pal = s.pal === 'cool' ? COOL : this.warm;
      for (let i = 0; i < 4; i++) for (let j = 0; j < 3; j++) this.colors[i][j] += (pal[i][j] - this.colors[i][j]) * Math.min(1, dt * 4);
      const slow = this.reduced ? .12 : 1;
      v.t += dt * s.speed * slow * (1 + v.level * .8); v.tt += dt * (this.reduced ? .35 : 1);
      this.gl ? this._drawGL() : this._draw2D();
    }

    _drawGL() {
      const gl = this.gl, u = this.u, v = this.v;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.uniform2f(u.u_res, this.canvas.width, this.canvas.height); gl.uniform1f(u.u_dpr, this.dpr);
      gl.uniform1f(u.u_t, v.t); gl.uniform1f(u.u_tt, v.tt); gl.uniform2f(u.u_half, v.w, v.h);
      gl.uniform1f(u.u_level, v.level); gl.uniform1f(u.u_energy, v.energy); gl.uniform1f(u.u_dots, v.dots);
      gl.uniform1f(u.u_shimmer, v.shimmer); gl.uniform1f(u.u_dim, v.dim);
      gl.uniform3fv(u.u_c, new Float32Array(this.colors.flat()));
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    _draw2D() {                                                                              // the same idea, plainer
      const c = this.c2, v = this.v; if (!c) return;
      c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0); c.clearRect(0, 0, this.cw, this.ch);
      const cx = this.cw / 2, cy = this.ch / 2, css = (k, a) => `rgba(${this.colors[k].map((x) => Math.round(x * 255)).join(',')},${a})`;
      const pill = () => { c.beginPath(); c.roundRect(cx - v.w, cy - v.h, v.w * 2, v.h * 2, v.h); };
      const a = v.energy * (.3 + 1.2 * v.level) * (1 - v.dim * .55);
      c.save(); c.shadowColor = css(0, Math.min(.8, a)); c.shadowBlur = 20 + 50 * v.level; pill(); c.fillStyle = '#0a0b10'; c.fill(); c.restore();
      c.save(); pill(); c.clip(); c.globalCompositeOperation = 'lighter';
      for (let i = 0; i < 4; i++) {
        const x = cx + Math.sin(v.t * (.35 + i * .11) + i * 2.2) * v.w * .6, y = cy + Math.sin(v.t * (1.1 + i * .23) + i * 1.7) * v.h * .5 * v.level;
        const r = v.w * (.45 + .3 * v.level), g = c.createRadialGradient(x, y, 0, x, y, r);
        g.addColorStop(0, css(i, Math.min(1, a))); g.addColorStop(1, css(i, 0));
        c.fillStyle = g; c.fillRect(cx - v.w, cy - v.h, v.w * 2, v.h * 2);
      }
      for (let i = 0; i < 3 && v.dots > .02; i++) {
        const p = .25 + .75 * Math.pow(.5 + .5 * Math.sin(v.tt * 4.2 - i * .95), 2);
        c.beginPath(); c.arc(cx + (i - 1) * 15, cy, 3.6, 0, 7); c.fillStyle = `rgba(255,255,255,${p * v.dots})`; c.fill();
      }
      c.restore();
      pill(); c.strokeStyle = 'rgba(200,215,255,.28)'; c.lineWidth = 1; c.stroke();
    }
  }

  window.JevIsland = Island;
})();
