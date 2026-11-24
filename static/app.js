/* ===== KirkChat — client WebRTC ===== */
"use strict";

const $ = (id) => document.getElementById(id);

// ---- Elements
const lobby      = $("lobby");
const chatView   = $("chat");
const startBtn   = $("startBtn");
const nextBtn    = $("nextBtn");
const stopBtn    = $("stopBtn");
const micBtn     = $("micBtn");
const camBtn     = $("camBtn");
const statusText = $("statusText");
const remoteCover= $("remoteCover");
const remoteVid  = $("remote");
const localVid   = $("local");
const onlineCount= $("onlineCount");
const messages   = $("messages");
const chatForm   = $("chatForm");
const chatText   = $("chatText");
const ageCheck   = $("ageCheck");
const permHint   = $("permHint");
const openRules  = $("openRules");

// ---- Etat
let ws = null;
let pc = null;
let localStream = null;
let chatChannel = null;
let role = null;          // "caller" | "callee"
let micOn = true;
let camOn = true;
let wsReconnectTimer = null;
let pendingCandidates = [];   // candidats ICE recus avant la description distante

const ICE = { iceServers: [{ urls: "stun:stun.l.google.com:19302" },
                           { urls: "stun:stun1.l.google.com:19302" }] };

// ---- Utilitaires UI
function setStatus(text){ statusText.textContent = text; }
function showCover(show){ remoteCover.classList.toggle("hidden", !show); }
function sysMsg(text){
  const d = document.createElement("div");
  d.className = "msg sys"; d.textContent = text;
  messages.appendChild(d); messages.scrollTop = messages.scrollHeight;
}
function addMsg(text, who){
  const d = document.createElement("div");
  d.className = "msg " + (who === "me" ? "me" : "them");
  d.textContent = text;
  messages.appendChild(d); messages.scrollTop = messages.scrollHeight;
}
function clearChat(){ messages.innerHTML = ""; }

// ---- Regles (popup simple)
openRules && openRules.addEventListener("click", (e)=>{
  e.preventDefault();
  alert("Règles de KirkChat :\n\n- Soyez respectueux.\n- Interdit : mineurs, nudité, violence, harcèlement, contenu illégal.\n- Ne partagez pas d'informations personnelles.\n- Tout manquement = bannissement.\n\nBon chat ! 🇺🇸");
});

// ============================================================ Demarrage
startBtn.addEventListener("click", startSession);

async function startSession(){
  if (!ageCheck.checked){
    permHint.textContent = "Cochez d'abord la case de confirmation (18 ans + règles).";
    permHint.classList.remove("hidden");
    return;
  }
  permHint.classList.add("hidden");
  startBtn.disabled = true;
  startBtn.textContent = "Activation de la caméra…";

  // 1) Media local
  try{
    localStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: {ideal:1280}, height:{ideal:720} },
      audio: { echoCancellation:true, noiseSuppression:true, autoGainControl:true }
    });
  }catch(err){
    permHint.innerHTML = "Impossible d'accéder à la caméra/micro.<br>"
      + "Sur téléphone : utilisez <strong>https</strong> ou <strong>localhost</strong> (voir README).<br>"
      + "<small>(" + (err.message||err.name) + ")</small>";
    permHint.classList.remove("hidden");
    startBtn.disabled = false;
    startBtn.textContent = "▶ Commencer le chat vidéo";
    return;
  }
  localVid.srcObject = localStream;
  await localVid.play().catch(()=>{});

  // 2) Passage a l'ecran de chat
  lobby.classList.add("hidden");
  chatView.classList.remove("hidden");
  setStatus("Connexion au serveur…");
  showCover(true);

  // 3) WebSocket
  connectWS();
}

// ============================================================ WebSocket
function wsUrl(){
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + "/ws";
}

function connectWS(){
  try{
    ws = new WebSocket(wsUrl());
  }catch(err){
    setStatus("Connexion impossible.");
    return;
  }
  ws.onopen = ()=>{
    setStatus("Recherche d'un inconnu…");
    send({ type:"ready" });
  };
  ws.onmessage = (ev)=> onMessage(ev.data);
  ws.onclose = ()=>{
    ws = null;
    if (!chatView.classList.contains("hidden")){
      setStatus("Connexion perdue — reconnexion…");
      scheduleReconnect();
    }
  };
  ws.onerror = ()=>{ try{ ws.close(); }catch(_){} };
}

function scheduleReconnect(){
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(()=>{ wsReconnectTimer=null; if(!ws) connectWS(); }, 2000);
}

function send(obj){
  if (ws && ws.readyState === WebSocket.OPEN){
    ws.send(JSON.stringify(obj));
  }
}

async function onMessage(raw){
  let data;
  try{ data = JSON.parse(raw); }catch(_){ return; }
  switch(data.type){

    case "searching":
      setStatus("Recherche d'un inconnu…");
      showCover(true);
      resetPeer();
      break;

    case "matched":
      role = data.role;
      setStatus("Inconnu trouvé ! Connexion en cours…");
      showCover(true);
      await setupPeer(role);
      break;

    case "offer":
      if (pc){
        try{ await pc.setRemoteDescription(data.sdp); flushCandidates(); }catch(_){}
      }
      break;

    case "answer":
      if (pc){
        try{ await pc.setRemoteDescription(data.sdp); flushCandidates(); }catch(_){}
      }
      break;

    case "ice":
      if (pc && data.candidate){ await applyCandidate(data.candidate); }
      break;

    case "chat":
      if (!chatChannel || chatChannel.readyState !== "open"){
        addMsg(data.text, "them");
      }
      break;

    case "partner_left":
      setStatus("L'inconnu est parti. Recherche…");
      showCover(true);
      resetPeer();
      send({ type:"ready" });
      break;

    case "count":
      if (typeof data.n === "number") onlineCount.textContent = data.n;
      break;
  }
}

// ============================================================ WebRTC
function resetPeer(){
  if (pc){ try{ pc.close(); }catch(_){} pc = null; }
  if (chatChannel){ try{ chatChannel.close(); }catch(_){} chatChannel = null; }
  pendingCandidates = [];
  remoteVid.srcObject = null;
}

async function applyCandidate(c){
  try{
    if (pc && pc.currentRemoteDescription){
      await pc.addIceCandidate(c).catch(()=>{});
    } else {
      pendingCandidates.push(c);
    }
  }catch(_){}
}
function flushCandidates(){
  const arr = pendingCandidates; pendingCandidates = [];
  for (const c of arr){ applyCandidate(c); }
}

async function setupPeer(myRole){
  resetPeer();
  pc = new RTCPeerConnection(ICE);

  // flux local
  for (const track of localStream.getTracks()) pc.addTrack(track, localStream);

  // flux distant
  pc.ontrack = (e)=>{
    remoteVid.srcObject = e.streams[0];
    showCover(false);
    setStatus("Connecté 🇺🇸");
  };

  // ICE
  pc.onicecandidate = (e)=>{
    if (e.candidate) send({ type:"ice", candidate: e.candidate.toJSON() });
  };
  pc.oniceconnectionstatechange = ()=>{
    const st = pc && pc.iceConnectionState;
    if (st === "connected") setStatus("Connecté 🇺🇸");
    if (st === "disconnected" || st === "failed"){
      setStatus("Connexion instable…");
    }
  };

  // canal de tchat (datachannel) - cree par l'appelant
  if (myRole === "caller"){
    chatChannel = pc.createDataChannel("chat", { ordered:true });
    bindChatChannel(chatChannel);
    try{
      const offer = await pc.createOffer({ offerToReceiveAudio:true, offerToReceiveVideo:true });
      await pc.setLocalDescription(offer);
      send({ type:"offer", sdp: pc.localDescription });
    }catch(_){ /* ignore */ }
  } else {
    pc.ondatachannel = (e)=>{ chatChannel = e.channel; bindChatChannel(chatChannel); };
  }
}

function bindChatChannel(ch){
  ch.onopen = ()=>{ sysMsg("Tchat ouvert — écrivez quelque chose !"); };
  ch.onclose = ()=>{};
  ch.onmessage = (e)=>{ addMsg(e.data, "them"); };
}

// ============================================================ Tchat texte
chatForm.addEventListener("submit", (e)=>{
  e.preventDefault();
  const text = chatText.value.trim();
  if (!text) return;
  addMsg(text, "me");
  // priorite au datachannel (P2P), sinon fallback via serveur
  if (chatChannel && chatChannel.readyState === "open"){
    try{ chatChannel.send(text); }catch(_){ send({ type:"chat", text:text }); }
  } else {
    send({ type:"chat", text:text });
  }
  chatText.value = "";
});

// ============================================================ Controles
nextBtn.addEventListener("click", ()=>{
  resetPeer();
  setStatus("Recherche d'un nouvel inconnu…");
  showCover(true);
  send({ type:"next" });
});

stopBtn.addEventListener("click", ()=>{
  stopAll();
});

micBtn.addEventListener("click", ()=>{
  micOn = !micOn;
  localStream.getAudioTracks().forEach(t => t.enabled = micOn);
  micBtn.textContent = micOn ? "🎤" : "🔇";
  micBtn.classList.toggle("off", !micOn);
});

camBtn.addEventListener("click", ()=>{
  camOn = !camOn;
  localStream.getVideoTracks().forEach(t => t.enabled = camOn);
  camBtn.textContent = camOn ? "📷" : "🚫";
  camBtn.classList.toggle("off", !camOn);
});

function stopAll(){
  send({ type:"stop" });
  resetPeer();
  if (localStream){ localStream.getTracks().forEach(t => t.stop()); localStream = null; }
  if (ws){ try{ ws.close(); }catch(_){} ws = null; }
  clearChat();
  chatView.classList.add("hidden");
  lobby.classList.remove("hidden");
  startBtn.disabled = false;
  startBtn.textContent = "▶ Commencer le chat vidéo";
  micOn = camOn = true;
  micBtn.textContent = "🎤"; camBtn.textContent = "📷";
  micBtn.classList.remove("off"); camBtn.classList.remove("off");
}

window.addEventListener("beforeunload", ()=>{
  if (ws && ws.readyState === WebSocket.OPEN){ try{ ws.close(); }catch(_){} }
});


