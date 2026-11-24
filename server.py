#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KirkChat - Serveur de signaling + appairage pour chat video aleatoire (WebRTC).
Implementation 100% bibliotheque standard (aucune dependance externe).
- Sert les fichiers statiques (front-end) en HTTP sur le meme port.
- Accepte les upgrades WebSocket pour le signaling (offres/reponses/ICE)
  et l'appairage aleatoire des utilisateurs.

Lancement :  python server.py   puis ouvrir http://localhost:8000
"""

import asyncio
import base64
import hashlib
import itertools
import json
import logging
import mimetypes
import os
import socket
import struct
import urllib.parse

# ----------------------------------------------------------------------------- config
HOST = "0.0.0.0"          # 0.0.0.0 = accessible depuis le reseau local (telephone)
PORT = int(os.environ.get("PORT", 8000))  # Railway injecte PORT automatiquement
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ----------------------------------------------------------------------------- etat global
clients = {}              # cid -> {"writer","partner","ready"}
waiting = set()            # cids prets et en attente d'un partenaire
_lock = asyncio.Lock()
_ids = itertools.count(1)

# ----------------------------------------------------------------------------- utilitaires reseau
def next_id():
    return next(_ids)

def lan_ip():
    """Adresse IP locale pour tester depuis un telephone sur le meme Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ----------------------------------------------------------------------------- trames WebSocket (RFC 6455)
async def ws_send(writer, data, opcode=1):
    """Envoie une trame WebSocket (texte ou binaire). Serveur = non masque."""
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
    elif isinstance(data, str):
        data = data.encode("utf-8")
    header = bytearray()
    header.append(0x80 | opcode)          # FIN + opcode
    length = len(data)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    writer.write(bytes(header) + data)
    await writer.drain()

async def ws_recv(reader, writer):
    """Lit une trame WebSocket. Retourne (opcode, payload). gere ping/pong."""
    b1 = (await reader.readexactly(1))[0]
    b2 = (await reader.readexactly(1))[0]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(payload[i] ^ mask[i & 3] for i in range(len(payload)))
    if opcode == 9:                          # ping -> pong
        await ws_send(writer, payload, opcode=10)
        return (10, payload)
    return (opcode, payload)

# ----------------------------------------------------------------------------- envoi / transfert
async def send(cid, obj):
    c = clients.get(cid)
    if not c:
        return
    try:
        await ws_send(c["writer"], obj)
    except Exception:
        pass

async def forward(cid, obj):
    """Transfere un message au partenaire courant de cid."""
    async with _lock:
        partner = clients[cid]["partner"] if cid in clients else None
    if partner is not None:
        await send(partner, obj)

# ----------------------------------------------------------------------------- logique d'appairage
async def _try_match_locked(cid):
    """Tente d'appairer cid avec un autre utilisateur en attente. Renvoie le partenaire ou None.
    Doit etre appele sous _lock."""
    partner = None
    for w in list(waiting):
        if w != cid:
            partner = w
            break
    if partner is not None:
        waiting.discard(cid)
        waiting.discard(partner)
        clients[cid]["partner"] = partner
        clients[partner]["partner"] = cid
        clients[cid]["ready"] = False
        clients[partner]["ready"] = False
    return partner

async def on_ready(cid):
    old_pid = None
    async with _lock:
        old_pid = clients[cid].get("partner")
        if old_pid is not None and old_pid in clients:
            clients[old_pid]["partner"] = None
            clients[old_pid]["ready"] = True
            waiting.add(old_pid)
        clients[cid]["partner"] = None
        clients[cid]["ready"] = True
        waiting.add(cid)
        partner = await _try_match_locked(cid)
    if old_pid is not None and old_pid in clients:
        await send(old_pid, {"type": "partner_left"})
    if partner is not None:
        await send(cid, {"type": "matched", "role": "callee"})
        await send(partner, {"type": "matched", "role": "caller"})
    else:
        await send(cid, {"type": "searching"})

async def on_next(cid):
    old_pid = None
    partner = None
    async with _lock:
        old_pid = clients[cid].get("partner")
        if old_pid is not None and old_pid in clients:
            clients[old_pid]["partner"] = None
            clients[old_pid]["ready"] = True
            waiting.add(old_pid)
        clients[cid]["partner"] = None
        clients[cid]["ready"] = True
        waiting.add(cid)
        partner = await _try_match_locked(cid)
    if old_pid is not None and old_pid in clients:
        await send(old_pid, {"type": "partner_left"})
    if partner is not None:
        await send(cid, {"type": "matched", "role": "callee"})
        await send(partner, {"type": "matched", "role": "caller"})
    else:
        await send(cid, {"type": "searching"})

async def on_stop(cid):
    old_pid = None
    async with _lock:
        old_pid = clients[cid].get("partner")
        if old_pid is not None and old_pid in clients:
            clients[old_pid]["partner"] = None
            clients[old_pid]["ready"] = False
        clients[cid]["partner"] = None
        clients[cid]["ready"] = False
        waiting.discard(cid)
    if old_pid is not None and old_pid in clients:
        await send(old_pid, {"type": "partner_left"})

async def cleanup(cid):
    old_pid = None
    async with _lock:
        c = clients.pop(cid, None)
        waiting.discard(cid)
        if c is not None:
            old_pid = c.get("partner")
            if old_pid is not None and old_pid in clients:
                clients[old_pid]["partner"] = None
                clients[old_pid]["ready"] = True
                waiting.add(old_pid)
    if old_pid is not None and old_pid in clients:
        await send(old_pid, {"type": "partner_left"})

async def handle_ws_message(cid, data):
    t = data.get("type")
    if t == "ready":
        await on_ready(cid)
    elif t == "offer":
        await forward(cid, {"type": "offer", "sdp": data.get("sdp")})
    elif t == "answer":
        await forward(cid, {"type": "answer", "sdp": data.get("sdp")})
    elif t == "ice":
        await forward(cid, {"type": "ice", "candidate": data.get("candidate")})
    elif t == "next":
        await on_next(cid)
    elif t == "stop":
        await on_stop(cid)
    elif t == "chat":
        await forward(cid, {"type": "chat", "text": data.get("text"), "name": data.get("name")})

# ----------------------------------------------------------------------------- boucle WebSocket
async def ping_loop(writer):
    try:
        while True:
            await asyncio.sleep(25)
            await ws_send(writer, b"", opcode=9)   # ping
    except (ConnectionResetError, BrokenPipeError):
        pass
    except asyncio.CancelledError:
        pass

async def websocket_loop(reader, writer, headers):
    key = headers.get("sec-websocket-key")
    if not key:
        writer.close()
        return
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    resp = ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode("latin-1")
    writer.write(resp)
    await writer.drain()

    cid = next_id()
    async with _lock:
        clients[cid] = {"writer": writer, "partner": None, "ready": False}
    logging.info("Client connecte  cid=%s (total=%d)", cid, len(clients))
    pinger = asyncio.create_task(ping_loop(writer))
    try:
        while True:
            opcode, payload = await ws_recv(reader, writer)
            if opcode == 8:        # close
                break
            if opcode in (9, 10):   # ping/pong
                continue
            if opcode == 1 and payload:
                try:
                    data = json.loads(payload.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                await handle_ws_message(cid, data)
    except asyncio.IncompleteReadError:
        pass
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        pinger.cancel()
        await cleanup(cid)
        logging.info("Client deconnecte cid=%s (total=%d)", cid, len(clients))
        try:
            writer.close()
        except Exception:
            pass

# ----------------------------------------------------------------------------- HTTP statique
async def http_respond(writer, status, body, ctype):
    reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "Status")
    head = ("HTTP/1.1 %d %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "\r\n" % (status, reason, ctype, len(body))).encode("latin-1")
    writer.write(head + body)
    await writer.drain()

async def serve_http(writer, method, path):
    if method != "GET":
        await http_respond(writer, 405, b"Method Not Allowed\n", "text/plain; charset=utf-8")
        return
    decoded = urllib.parse.unquote(path.split("?", 1)[0])
    if decoded == "/":
        decoded = "/index.html"
    rel = decoded.lstrip("/")
    full = os.path.normpath(os.path.join(STATIC_DIR, rel))
    if os.path.commonpath([STATIC_DIR, full]) != STATIC_DIR:
        await http_respond(writer, 403, b"Forbidden\n", "text/plain; charset=utf-8")
        return
    if os.path.isdir(full):
        full = os.path.join(full, "index.html")
    if not os.path.isfile(full):
        await http_respond(writer, 404, b"404 Not Found\n", "text/plain; charset=utf-8")
        return
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if full.endswith((".html", ".js", ".css", ".svg")):
        ctype += "; charset=utf-8"
    with open(full, "rb") as f:
        body = f.read()
    await http_respond(writer, 200, body, ctype)

# ----------------------------------------------------------------------------- dispatcher
async def handle_client(reader, writer):
    try:
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            return
        parts = request_line.decode("latin-1").strip().split()
        if len(parts) < 3:
            writer.close()
            return
        method, path = parts[0], parts[1]
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").strip().partition(":")
            headers[k.strip().lower()] = v.strip()
        if "websocket" in headers.get("upgrade", "").lower():
            await websocket_loop(reader, writer, headers)
        else:
            await serve_http(writer, method, path)
    except (ConnectionResetError, BrokenPipeError):
        pass
    except asyncio.IncompleteReadError:
        pass
    except Exception:
        logging.exception("Erreur handle_client")
    finally:
        try:
            writer.close()
        except Exception:
            pass

# ----------------------------------------------------------------------------- main
async def broadcast_loop():
    """Envoie periodiquement le nombre de connectes a tous les clients."""
    while True:
        await asyncio.sleep(5)
        payload = {"type": "count", "n": len(clients)}
        async with _lock:
            ids = list(clients.keys())
        for cid in ids:
            await send(cid, payload)

async def main():
    mimetypes.init()
    asyncio.create_task(broadcast_loop())
    server = await asyncio.start_server(handle_client, HOST, PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    ip = lan_ip()
    print("=" * 60)
    print("  KirkChat - chat video aleatoire (theme Amerique)")
    print("=" * 60)
    print("  Ordi (ce PC)  : http://localhost:%d" % PORT)
    print("  Telephone     : http://%s:%d  (meme Wi-Fi)" % (ip, PORT))
    print("-" * 60)
    print("  NOTE camera : le navigateur exige une connexion securisee")
    print("  (https ou localhost). Voir README pour l'acces telephone.")
    print("=" * 60)
    logging.info("KirkChat en ecoute sur %s", addrs)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKirkChat arrete.")
