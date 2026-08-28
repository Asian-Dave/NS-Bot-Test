"""Minimal Chrome DevTools Protocol client - standard library only.

Why hand-roll this instead of using pydirectinput / mss:

  * pydirectinput cannot even be imported off Windows (`ctypes.windll` at import).
  * mss captures a *screen*, which forces the game window to stay frontmost and
    unoccluded, and on Retina introduces a dpr-2 scaling step.
  * CDP gives us both halves cleanly:
      - Page.captureScreenshot     -> composites the Ruffle WebGL canvas, exact px
      - Input.dispatchMouseEvent   -> arrives at the canvas with isTrusted=true
                                      (verified against the live game)

The same client works against Chromium inside the container and against a host
Chrome started with --remote-debugging-port, which is what keeps the Act backend
swappable if the container turns out too slow for combat.
"""

import base64, json, os, select, socket, struct, time, urllib.request


class CDPError(RuntimeError):
    pass


def find_page_target(host="127.0.0.1", port=9222, url_contains=None, timeout=30):
    """Poll the CDP HTTP endpoint until a matching page target appears."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            raw = urllib.request.urlopen(f"http://{host}:{port}/json", timeout=5).read()
            for t in json.loads(raw):
                if t.get("type") != "page":
                    continue
                if url_contains and url_contains not in t.get("url", ""):
                    continue
                return t
        except Exception as e:                      # browser may not be up yet
            last = e
        time.sleep(1)
    # Distinguish the three ways this fails - a bare timeout hides which one.
    detail = f"last error: {last}"
    try:
        raw = urllib.request.urlopen(f"http://{host}:{port}/json", timeout=5).read()
        targets = json.loads(raw)
        pages = [t for t in targets if t.get("type") == "page"]
        if not targets:
            detail = ("browser is listening but has NO targets - its window was "
                      "probably closed. Quit Chrome fully and relaunch.")
        elif not pages:
            detail = f"{len(targets)} target(s) but no type=page"
        else:
            detail = (f"{len(pages)} page(s) open, none matching "
                      f"{url_contains!r}. Open the game in that window.")
    except Exception:
        detail = f"nothing answering on {host}:{port} ({last})"
    raise CDPError(f"no page target after {timeout}s - {detail}")


class CDP:
    """One websocket connection to one page target."""

    def __init__(self, ws_url, timeout=60):
        self._id = 0
        # Events arrive interleaved with command replies on the same socket.
        # `call` used to DROP every message that was not its own reply, which
        # made CDP effectively request/response only - and silently so. Nothing
        # errored; the events simply never existed. That is why an interactive
        # in-page panel was not possible: `Runtime.bindingCalled`, the event a
        # button click produces, was being thrown away. They are buffered here
        # instead and handed over by `drain_events`.
        self._events = []
        self._max_events = 512
        self._timeout = timeout
        # Only these methods are kept. Buffering everything is not viable here:
        # `Runtime.enable` is required for bindings to fire at all, and this
        # game's console is famously noisy (CLAUDE.md: the `initButton` lines
        # that are not errors). Measured - enabling Page+Runtime filled a
        # 512-slot buffer with `Runtime.consoleAPICalled` alone in half a second,
        # which would evict the button press we actually care about.
        self._watch = None                           # None = keep everything
        u = ws_url.replace("ws://", "")
        hostport, path = u.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("handshake closed early")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise CDPError(f"handshake refused: {buf.split(chr(13).encode())[0]!r}")

    # ---- websocket framing (client frames must be masked, RFC6455) ----------
    def _send_text(self, payload: str):
        data = payload.encode()
        hdr = bytearray([0x81])                      # FIN + opcode 1 (text)
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 1 << 16:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise CDPError("socket closed")
            out += chunk
        return out

    def _recv_text(self):
        while True:
            b0, b1 = self._recv_exact(2)
            opcode = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(ln)
            if b1 & 0x80:                            # server frames are unmasked,
                m, payload = payload[:4], payload[4:]  # but tolerate it anyway
                payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise CDPError("server closed connection")
            if opcode == 0x9:                        # ping -> pong
                continue
            if opcode in (0x1, 0x2):
                return payload.decode("utf-8", "replace")

    # ---- CDP request/response ---------------------------------------------
    def _stash(self, msg):
        """Keep an event that arrived while we were waiting for a reply.

        Bounded, and it drops the OLDEST when full: an unread backlog is stale
        by definition, and losing the newest event would mean losing the button
        the operator just pressed.
        """
        if "method" not in msg:
            return
        if self._watch is not None and msg["method"] not in self._watch:
            return
        self._events.append(msg)
        if len(self._events) > self._max_events:
            del self._events[:len(self._events) - self._max_events]

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self._send_text(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self._recv_text())
            if msg.get("id") != mid:
                self._stash(msg)                     # an event, not our reply
                continue
            if "error" in msg:
                raise CDPError(f"{method}: {msg['error']}")
            return msg.get("result", {})

    def watch(self, *methods):
        """Buffer only these event methods. No arguments restores 'keep all'."""
        self._watch = set(methods) or None
        return self._watch

    def add_binding(self, name):
        """Expose `window.<name>(str)` in the page; calls arrive as events.

        This is the operator's channel INTO the bot. A button in the injected
        panel calls it, and the string turns up as a `Runtime.bindingCalled`
        event on this same socket - no HTTP server, no polling the DOM, and
        nothing listening on a port.
        """
        self.call("Runtime.enable")
        self.call("Runtime.addBinding", name=name)
        return name

    def poll_events(self, timeout=0.0):
        """Read whatever is already on the socket, without blocking on nothing.

        Needed because events are unsolicited: between two `call`s nobody is
        reading the socket, so an event sits in the kernel buffer until someone
        looks. A zero timeout makes this safe to call every cycle.

        WAITING IS DONE WITH `select`, NEVER WITH A SOCKET TIMEOUT. A timeout can
        expire in the MIDDLE of a websocket frame - after the 2-byte header has
        been consumed but before the payload - which leaves the stream
        permanently desynchronised: every later read returns garbage and the next
        `call()` waits forever for a reply it can no longer recognise. That is a
        hang with no error and no clue, and it is what the first version of this
        method actually did. `select` asks whether a read WOULD block; once it
        says yes, the frame is read to completion with the normal timeout.
        """
        end = time.time() + max(0.0, timeout)
        while True:
            remaining = max(0.0, end - time.time())
            try:
                ready, _, _ = select.select([self.sock], [], [], remaining)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                msg = json.loads(self._recv_text())
            except (CDPError, OSError, ValueError):
                break
            if "method" in msg:
                self._stash(msg)
        return len(self._events)

    def drain_events(self, method=None, poll=0.0):
        """Take buffered events, oldest first, optionally filtered by method."""
        self.poll_events(poll)
        if method is None:
            out, self._events = self._events, []
            return out
        out = [e for e in self._events if e.get("method") == method]
        self._events = [e for e in self._events if e.get("method") != method]
        return out

    # ---- the two operations the bot actually needs ------------------------
    def evaluate(self, expr, await_promise=True):
        r = self.call("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise CDPError(f"JS threw: {r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")

    def screenshot(self, path=None, clip=None, scale=1.0):
        """Composited page pixels, including the Ruffle WebGL canvas.

        `clip` is an optional (x, y, w, h) in CSS pixels. Passing it moves the
        crop server-side, so only the region of interest is encoded and sent
        instead of the whole page. Worth using on a hot polling loop — full-frame
        capture was measured at ~82 ms (~12 fps) over CDP, and most state gates
        only care about one small area.

        `scale` is the clip's output resolution multiplier. It defaults to 1,
        which yields ONE OUTPUT PIXEL PER CSS PIXEL — while an unclipped frame
        comes back at the device pixel ratio. On a Retina host that is a silent
        factor-of-two: geometry measured against full frames lands in the wrong
        place on a clipped one. Pass `scale=capture.dpr` to get a clip in the
        SAME pixel space as a full frame, so an offset is the only correction
        needed.
        """
        params = {"format": "png"}
        if clip:
            x, y, w, h = clip
            params["clip"] = {"x": float(x), "y": float(y),
                              "width": float(w), "height": float(h),
                              "scale": float(scale)}
        r = self.call("Page.captureScreenshot", **params)
        data = base64.b64decode(r["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def click(self, x, y, jitter=0):
        """Trusted click at viewport CSS coordinates."""
        if jitter:
            import random
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)
        for ev in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=ev, x=int(x), y=int(y),
                      button="left", clickCount=1)
        return int(x), int(y)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
