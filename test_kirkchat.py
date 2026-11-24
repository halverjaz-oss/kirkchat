#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests de bout en bout pour KirkChat : HTTP + WebSocket (appairage + signaling).
A lancer avec le serveur en cours d'execution sur localhost:8000."""
import base64, hashlib, json, os, socket, struct, sys, time
import urllib.request, urllib.error

HOST, PORT = "127.0.0.1", 8000
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def http_get(path):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), len(e.read())
    except Exception as e:
        return "ERR", str(e), 0

# ---------- Mini client WebSocket (stdlib) ----------
class WS:
    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self.leftover = leftover
    def send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(data[i] ^ mask[i & 3] for i in range(len(data)))
        header = bytearray([0x81])
        l = len(data)
        if l < 126: header.append(0x80 | l)
        elif l < 65536: header.append(0x80 | 126); header += struct.pack(">H", l)
        else: header.append(0x80 | 127); header += struct.pack(">Q", l)
        self.sock.sendall(bytes(header) + mask + masked)
    def _raw(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk: raise ConnectionError("closed")
            data += chunk
        return data
    def recv(self, skip=("count",)):
        # trames serveur : non masquees
        if self.leftover:
            buf = self.leftover; self.leftover = b""
        else:
            buf = self._raw(2)
        b1, b2 = buf[0], buf[1]
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            need = 4 - len(buf)
            if need > 0: buf += self._raw(need)
            length = struct.unpack(">H", buf[2:4])[0]
        elif length == 127:
            need = 10 - len(buf)
            if need > 0: buf += self._raw(need)
            length = struct.unpack(">Q", buf[2:10])[0]
        payload = self._raw(length) if length else b""
        if opcode == 8:
            return None
        if opcode in (9, 10):
            return self.recv(skip)
        text = payload.decode("utf-8", "replace")
        try:
            obj = json.loads(text)
        except Exception:
            return {"type": "raw", "text": text}
        if obj.get("type") in skip:
            return self.recv(skip)
        return obj

def ws_connect():
    s = socket.create_connection((HOST, PORT), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET /ws HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (HOST, PORT, key)).encode()
    s.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, _, rest = buf.partition(b"\r\n\r\n")
    first = head.split(b"\r\n")[0]
    if b"101" not in first:
        raise RuntimeError("Handshake WS echoue: " + first.decode("latin-1"))
    # parse des en-tetes (valeur conservee, cle insensible a la casse)
    accept = b""
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"sec-websocket-accept":
            accept = v.strip()
            break
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if accept.decode() != expected:
        raise RuntimeError("Sec-WebSocket-Accept incorrect")
    s.settimeout(3)
    return WS(s, rest)

# ---------- Test runner ----------
PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "KO   ") + name)

def main():
    print("== Tests HTTP ==")
    st, ct, ln = http_get("/")
    check("GET / -> 200", st == 200)
    check("GET / Content-Type text/html", "text/html" in (ct or ""))
    check("GET / longueur > 1000", ln > 1000)
    st, ct, ln = http_get("/style.css")
    check("GET /style.css -> 200", st == 200)
    check("GET /style.css text/css", "text/css" in (ct or ""))
    st, ct, ln = http_get("/app.js")
    check("GET /app.js -> 200", st == 200)
    check("GET /app.js javascript", "javascript" in (ct or ""))
    st, ct, ln = http_get("/favicon.svg")
    check("GET /favicon.svg -> 200", st == 200)
    st, ct, ln = http_get("/missing.html")
    check("GET /missing.html -> 404", st == 404)
    st, ct, ln = http_get("/../server.py")
    check("Traversal bloque (403/404)", st in (403, 404))

    print("\n== Tests WebSocket : appairage + signaling ==")
    A = ws_connect()
    B = ws_connect()
    A.send({"type": "ready"})
    time.sleep(0.4)
    obj = A.recv()
    check("A seul -> searching", obj and obj.get("type") == "searching")
    B.send({"type": "ready"})
    objB = B.recv()
    check("B matche -> callee", objB and objB.get("type") == "matched" and objB.get("role") == "callee")
    objA = A.recv()
    check("A matche -> caller", objA and objA.get("type") == "matched" and objA.get("role") == "caller")
    A.send({"type": "offer", "sdp": {"type": "offer", "sdp": "FAKE_OFFER"}})
    obj = B.recv()
    check("offer transfere a B", obj and obj.get("type") == "offer")
    B.send({"type": "answer", "sdp": {"type": "answer", "sdp": "FAKE_ANSWER"}})
    obj = A.recv()
    check("answer transfere a A", obj and obj.get("type") == "answer")
    A.send({"type": "ice", "candidate": {"candidate": "FAKE_ICE"}})
    obj = B.recv()
    check("ICE transfere a B", obj and obj.get("type") == "ice")
    A.send({"type": "chat", "text": "coucou"})
    obj = B.recv()
    check("chat relaye a B", obj and obj.get("type") == "chat" and obj.get("text") == "coucou")
    A.send({"type": "next"})
    obj = B.recv()
    check("B recoit partner_left", obj and obj.get("type") == "partner_left")
    A.send({"type": "stop"})
    B.send({"type": "stop"})
    A.sock.close(); B.sock.close()

    print("\n== Resultat ==")
    print(f"Reussis : {len(PASS)}")
    print(f"Echoues : {len(FAIL)}")
    if FAIL:
        print("Echecs : " + ", ".join(FAIL))
        sys.exit(1)
    print("TOUS LES TESTS SONT PASSANTS.")

if __name__ == "__main__":
    main()
