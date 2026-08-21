/* Ruffle-proxy benchmark.
 *
 * We cannot benchmark Ruffle rendering Ninja Saga inside a container without a
 * logged-in session there, so instead we measure the two resources Ruffle actually
 * competes for, with an identical fixed workload in both environments:
 *
 *   1. GL identity   - which renderer is really in use (Metal/ANGLE vs SwiftShader).
 *   2. GL throughput - many small textured draw calls per frame. This mimics how
 *                      Ruffle rasterises SWF vector art: lots of little quads, not
 *                      a few big ones. Fixed workload, so host vs container is a
 *                      clean ratio.
 *   3. CPU throughput- tight typed-array integer work, a proxy for AVM bytecode
 *                      interpretation. A proxy only: real Ruffle runs WASM, this is
 *                      JIT'd JS. Treat the ratio as indicative, not exact.
 *
 * The bar that matters is NOT the host's 120fps. The SWF declares 24fps, so the
 * question is only ever "does the container clear 24?".
 */
window.runBench = async function runBench(opts) {
  const CANVAS_W = (opts && opts.w) || 960;      // match the real game canvas
  const CANVAS_H = (opts && opts.h) || 839;
  const QUADS    = (opts && opts.quads) || 1500; // draw calls per frame
  const SECONDS  = (opts && opts.seconds) || 3;

  const out = { canvas: `${CANVAS_W}x${CANVAS_H}`, quadsPerFrame: QUADS, sampleSeconds: SECONDS };

  // ---- set up an offscreen-ish WebGL canvas (not attached to layout) ----
  const cv = document.createElement('canvas');
  cv.width = CANVAS_W; cv.height = CANVAS_H;
  const gl = cv.getContext('webgl', { antialias: false, preserveDrawingBuffer: false });
  if (!gl) { out.error = 'no webgl'; return out; }

  // ---- GL identity: this is the single most diagnostic value here ----
  const dbg = gl.getExtension('WEBGL_debug_renderer_info');
  out.glRenderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '(masked)';
  out.glVendor   = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)   : '(masked)';
  out.glVersion  = gl.getParameter(gl.VERSION);

  // ---- minimal textured-quad pipeline ----
  const vs = `attribute vec2 p; uniform vec2 off; varying vec2 uv;
              void main(){ uv = p; gl_Position = vec4(p*0.02 + off, 0.0, 1.0); }`;
  const fs = `precision mediump float; uniform sampler2D t; varying vec2 uv;
              void main(){ gl_FragColor = texture2D(t, uv); }`;
  const mk = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src);
                              gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, mk(gl.VERTEX_SHADER, vs));
  gl.attachShader(prog, mk(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    out.error = 'link failed: ' + gl.getProgramInfoLog(prog); return out;
  }
  gl.useProgram(prog);

  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0,0, 1,0, 0,1, 1,1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  // small noise texture, like a sprite atlas tile
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  const px = new Uint8Array(32*32*4);
  for (let i = 0; i < px.length; i++) px[i] = (i * 37) & 255;
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 32, 32, 0, gl.RGBA, gl.UNSIGNED_BYTE, px);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  const offLoc = gl.getUniformLocation(prog, 'off');

  // ---- 1. GL throughput: UNPACED. ----
  // Deliberately NOT driven by requestAnimationFrame. rAF is capped at display
  // refresh (120Hz here) so an rAF-paced loop can never reveal headroom, and it is
  // suppressed entirely in a hidden tab - which silently turns the benchmark into a
  // throttling measurement. gl.finish() forces the driver to complete the batch, so
  // this is true max-throughput for the workload and is visibility-independent.
  let frames = 0;
  const t0 = performance.now();
  while (performance.now() - t0 < SECONDS * 1000) {
    gl.viewport(0, 0, CANVAS_W, CANVAS_H);
    gl.clear(gl.COLOR_BUFFER_BIT);
    for (let i = 0; i < QUADS; i++) {
      gl.uniform2f(offLoc, ((i * 7) % 100) / 50 - 1, ((i * 13) % 100) / 50 - 1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
    gl.finish();           // block until the GPU has actually drained the batch
    frames++;
  }
  const glSecs = (performance.now() - t0) / 1000;
  out.glFrames = frames;
  out.glFps = +(frames / glSecs).toFixed(2);
  out.glDrawCallsPerSec = Math.round(frames * QUADS / glSecs);

  // ---- 2. rAF ceiling with no work at all (display refresh / throttle state) ----
  // Diagnostic only: reports whether this context is being throttled. A hidden tab
  // returns ~0. Bounded by a setTimeout race so it can never hang the run.
  let rafN = 0; const r0 = performance.now();
  await new Promise(resolve => {
    let done = false;
    const stop = () => { if (!done) { done = true; resolve(); } };
    setTimeout(stop, 1500);                        // hard ceiling, survives suppression
    (function tick(){ rafN++;
      if (performance.now() - r0 >= 1000) stop(); else requestAnimationFrame(tick); })();
  });
  out.rafDeliveredFps = +(rafN / ((performance.now() - r0) / 1000)).toFixed(2);
  out.visibility = document.visibilityState;

  // ---- 3. CPU proxy ----
  const arr = new Int32Array(4096);
  let ops = 0; const c0 = performance.now();
  while (performance.now() - c0 < 1000) {
    for (let k = 0; k < 200; k++) {
      for (let i = 0; i < arr.length; i++) arr[i] = (arr[i] * 1103515245 + 12345) & 0x7fffffff;
      ops += arr.length;
    }
  }
  out.cpuOpsPerSec = Math.round(ops / ((performance.now() - c0) / 1000));

  out.verdict24fps = out.glFps >= 24 ? 'CLEARS 24fps' : 'BELOW 24fps';
  out.glHeadroomVs24 = +(out.glFps / 24).toFixed(2);
  return out;
};
