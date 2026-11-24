#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KirkChat - Serveur de signaling + appairage + moderation.
- /admin  : panel de moderation (localhost uniquement)
- /api/clients : liste JSON des connectes (localhost uniquement)
- /api/mod  : actions de moderation POST (localhost uniquement)
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
import time
import urllib.parse

# ----------------------------------------------------------------------------- config
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8000))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "kirkchat_admin_2024")

# ----------------------------------------------------------------------------- etat global
clients  = {}       # cid -> {writer, partner, ready, ip, joined, muted, banned, username}
waiting  = set()
banned_ips = set()  # IPs bannies definitivamente
_lock    = asyncio.Lock()
_ids     = itertools.count(1)

# ----------------------------------------------------------------------------- utilitaires
def next_id():
    return next(_ids)

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def is_local(peername):
    """Renvoie True si la connexion vient de localhost."""
    if not peername:
        return False
    ip = peername[0] if isinstance(peername, (list, tuple)) else str(peername)
    return ip in ("127.0.0.1", "::1", "localhost")

# ----------------------------------------------------------------------------- WebSocket (RFC 6455)
async def ws_send(writer, data, opcode=1):
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
    elif isinstance(data, str):
        data = data.encode("utf-8")
    header = bytearray()
    header.append(0x80 | opcode)
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
    if opcode == 9:
        await ws_send(writer, payload, opcode=10)
        return (10, payload)
    return (opcode, payload)

# ----------------------------------------------------------------------------- envoi
async def send(cid, obj):
    c = clients.get(cid)
    if not c:
        return
    try:
        await ws_send(c["writer"], obj)
    except Exception:
        pass

async def forward(cid, obj):
    async with _lock:
        partner = clients[cid]["partner"] if cid in clients else None
    if partner is not None:
        await send(partner, obj)

# ----------------------------------------------------------------------------- appairage
async def _try_match_locked(cid):
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
    # verifie si banni
    async with _lock:
        c = clients.get(cid)
        if c and c.get("banned"):
            return
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
    # verifie si mute pour le chat
    async with _lock:
        c = clients.get(cid, {})
        is_muted = c.get("muted", False)
        is_banned = c.get("banned", False)
    if is_banned:
        await send(cid, {"type": "banned", "reason": "Vous avez ete banni par un moderateur."})
        return
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
        if not is_muted:
            await forward(cid, {"type": "chat", "text": data.get("text"), "name": data.get("name")})
        else:
            await send(cid, {"type": "mod_notice", "text": "Vous etes mute par un moderateur."})
    elif t == "username":
        async with _lock:
            if cid in clients:
                clients[cid]["username"] = str(data.get("name", ""))[:32]

# ----------------------------------------------------------------------------- moderation
async def mod_kick(cid):
    """Deconnecte le client."""
    await send(cid, {"type": "kicked", "reason": "Expulse par un moderateur."})
    async with _lock:
        c = clients.get(cid)
    if c:
        try:
            c["writer"].close()
        except Exception:
            pass

async def mod_ban(cid):
    """Banni le client (IP + session)."""
    async with _lock:
        c = clients.get(cid)
        if c:
            c["banned"] = True
            ip = c.get("ip")
            if ip:
                banned_ips.add(ip)
    await send(cid, {"type": "banned", "reason": "Vous avez ete banni par un moderateur."})
    await asyncio.sleep(0.5)
    await mod_kick(cid)

async def mod_mute(cid, state=True):
    """Mute ou demute le client."""
    async with _lock:
        if cid in clients:
            clients[cid]["muted"] = state
    msg = "Vous avez ete mute." if state else "Vous avez ete demute."
    await send(cid, {"type": "mod_notice", "text": msg})

async def mod_next(cid):
    """Force le client a changer d'inconnu."""
    await on_next(cid)
    await send(cid, {"type": "mod_notice", "text": "Connexion changee par un moderateur."})

# ----------------------------------------------------------------------------- boucle WebSocket
async def ping_loop(writer):
    try:
        while True:
            await asyncio.sleep(25)
            await ws_send(writer, b"", opcode=9)
    except (ConnectionResetError, BrokenPipeError):
        pass
    except asyncio.CancelledError:
        pass

async def websocket_loop(reader, writer, headers, peername):
    key = headers.get("sec-websocket-key")
    if not key:
        writer.close()
        return
    # verifie IP bannie
    client_ip = peername[0] if peername else "?"
    if client_ip in banned_ips:
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
        clients[cid] = {
            "writer": writer,
            "partner": None,
            "ready": False,
            "ip": client_ip,
            "joined": time.time(),
            "muted": False,
            "banned": False,
            "username": "",
        }
    logging.info("Client connecte cid=%s ip=%s (total=%d)", cid, client_ip, len(clients))
    pinger = asyncio.create_task(ping_loop(writer))
    try:
        while True:
            opcode, payload = await ws_recv(reader, writer)
            if opcode == 8:
                break
            if opcode in (9, 10):
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
    reason = {200: "OK", 403: "Forbidden", 404: "Not Found",
              405: "Method Not Allowed", 401: "Unauthorized"}.get(status, "Status")
    head = ("HTTP/1.1 %d %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n" % (status, reason, ctype, len(body))).encode("latin-1")
    writer.write(head + body)
    await writer.drain()

async def serve_http(writer, method, path, peername, body=b""):
    local = is_local(peername)

    # ---- API moderation (localhost uniquement)
    if path.startswith("/api/"):
        if not local:
            await http_respond(writer, 403, b"Forbidden", "text/plain")
            return
        if path == "/api/clients" and method == "GET":
            async with _lock:
                data = [
                    {
                        "cid": cid,
                        "ip": c["ip"],
                        "username": c["username"],
                        "partner": c["partner"],
                        "muted": c["muted"],
                        "banned": c["banned"],
                        "joined": int(c["joined"]),
                        "waiting": cid in waiting,
                    }
                    for cid, c in clients.items()
                ]
            payload = json.dumps(data).encode("utf-8")
            await http_respond(writer, 200, payload, "application/json")
            return
        if path == "/api/mod" and method == "POST":
            try:
                req = json.loads(body.decode("utf-8"))
            except Exception:
                await http_respond(writer, 400, b"Bad JSON", "text/plain")
                return
            action = req.get("action")
            cid = req.get("cid")
            if cid is not None:
                cid = int(cid)
            if action == "kick" and cid:
                await mod_kick(cid)
            elif action == "ban" and cid:
                await mod_ban(cid)
            elif action == "mute" and cid:
                await mod_mute(cid, True)
            elif action == "unmute" and cid:
                await mod_mute(cid, False)
            elif action == "next" and cid:
                await mod_next(cid)
            elif action == "ban_ip":
                ip = req.get("ip")
                if ip:
                    banned_ips.add(ip)
            elif action == "unban_ip":
                ip = req.get("ip")
                if ip:
                    banned_ips.discard(ip)
            elif action == "broadcast":
                text = req.get("text", "")
                async with _lock:
                    ids = list(clients.keys())
                for c in ids:
                    await send(c, {"type": "mod_notice", "text": text})
            await http_respond(writer, 200, b'{"ok":true}', "application/json")
            return
        if path == "/api/banned" and method == "GET":
            payload = json.dumps(list(banned_ips)).encode()
            await http_respond(writer, 200, payload, "application/json")
            return
        await http_respond(writer, 404, b"Not found", "text/plain")
        return

    # ---- Page admin (localhost uniquement)
    if path.rstrip("/") == "/admin":
        if not local:
            await http_respond(writer, 403,
                b"<h1>Acces refuse</h1><p>Le panel admin est accessible uniquement depuis localhost.</p>",
                "text/html; charset=utf-8")
            return
        full = os.path.join(STATIC_DIR, "admin.html")
        if os.path.isfile(full):
            with open(full, "rb") as f:
                body_f = f.read()
            await http_respond(writer, 200, body_f, "text/html; charset=utf-8")
        else:
            await http_respond(writer, 404, b"admin.html introuvable", "text/plain")
        return

    # ---- Fichiers statiques
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
        body_f = f.read()
    await http_respond(writer, 200, body_f, ctype)

# ----------------------------------------------------------------------------- dispatcher
async def handle_client(reader, writer):
    peername = None
    try:
        peername = writer.get_extra_info("peername")
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
        content_length = 0
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").strip().partition(":")
            k = k.strip().lower()
            headers[k] = v.strip()
            if k == "content-length":
                try:
                    content_length = int(v.strip())
                except Exception:
                    pass
        body = b""
        if content_length > 0:
            body = await reader.readexactly(min(content_length, 65536))
        if "websocket" in headers.get("upgrade", "").lower():
            await websocket_loop(reader, writer, headers, peername)
        else:
            await serve_http(writer, method, path, peername, body)
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

# ----------------------------------------------------------------------------- broadcast
async def broadcast_loop():
    while True:
        await asyncio.sleep(5)
        payload = {"type": "count", "n": len(clients)}
        async with _lock:
            ids = list(clients.keys())
        for cid in ids:
            await send(cid, payload)

# ----------------------------------------------------------------------------- main
async def main():
    mimetypes.init()
    asyncio.create_task(broadcast_loop())
    server = await asyncio.start_server(handle_client, HOST, PORT)
    ip = lan_ip()
    print("=" * 60)
    print("  KirkChat - chat video aleatoire (theme Amerique)")
    print("=" * 60)
    print("  Site      : http://localhost:%d" % PORT)
    print("  Admin     : http://localhost:%d/admin" % PORT)
    print("  Telephone : http://%s:%d  (meme Wi-Fi)" % (ip, PORT))
    print("=" * 60)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKirkChat arrete.")
