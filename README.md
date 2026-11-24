# KirkChat 🇺🇸 — Chat vidéo aléatoire (thème Amérique)

Rencontrez des inconnus en vidéo, comme sur **ome.tv** ou **minichat.com**.
Un clic → votre caméra et votre micro → vous êtes mis en relation avec un
inconnu au hasard. Fonctionne sur **ordinateur** comme sur **téléphone**.

Le projet tient en **un serveur Python (stdlib uniquement, aucune dépendance)**
+ un front-end HTML/CSS/JS responsive au couleurs du drapeau américain.

---

## 1. Lancer en local (ordinateur)

Prérequis : Python 3.8+ installé (https://www.python.org/downloads/).

Deux options :

**Option A — un double-clic**
```
double-cliquez sur  start.bat
```
Le serveur démarre et le navigateur s'ouvre sur http://localhost:8000

**Option B — en ligne de commande**
```
cd kirkchat
python server.py
```
puis ouvrez http://localhost:8000 dans votre navigateur.

> Sur `localhost`, la caméra et le micro fonctionnent immédiatement
> (localhost est un contexte sécurisé pour les navigateurs).

### Tester le hasard
Ouvrez le site dans **deux onglets** (ou deux fenêtres de navigation privée).
Cliquez « Commencer » dans les deux : les deux onglets sont appairés ensemble.
Utilisez « Suivant » pour changer d'inconnu.

---

## 2. Accès depuis un téléphone (même Wi-Fi)

La caméra (getUserMedia) n'est autorisée **que sur une connexion sécurisée**
(`https://` ou `localhost`). Depuis un téléphone, l'URL `http://192.168.x.x:8000`
n'est **pas** sécurisée → la caméra sera bloquée.

Trois solutions, de la plus simple à la plus complète :

1. **Tunnel sécurisé (recommandé pour tester)** — installez
   [cloudflared](https://github.com/cloudflare/cloudflared) ou [ngrok](https://ngrok.com),
   puis :
   ```
   cloudflared tunnel --url http://localhost:8000
   # ou
   ngrok http 8000
   ```
   Vous obtenez une URL `https://xxxx.trycloudflare.com` ou `https://xxxx.ngrok.io`
   à ouvrir sur le téléphone. La caméra fonctionnera.

2. **Certificat auto-signé** — généré avec `openssl`, serveur en HTTPS.
   Le téléphone affichera un avertissement de sécurité à accepter manuellement.

3. **Hébergement réel en HTTPS** — voir section 4 (déploiement).

---

## 3. Comment ça marche (architecture)

```
 Navigateur A  <-- WebRTC (P2P vidéo/audio + datachannel tchat) -->  Navigateur B
      |                                                                  |
      +---------- WebSocket (signaling) ----------+                      |
                   serveur.py :8000                                     |
            - appairage aléatoire                                      |
            - relai SDP (offer/answer) + ICE                            |
            - compte des connectés                                      |
```

- **WebRTC** établit la vidéo/audio **directement entre les deux navigateurs**
  (pair-à-pair). Le serveur n'a jamais accès au flux vidéo.
- Le **serveur** ne sert qu'à : (a) mettre deux inconnus en relation,
  (b) relayer les messages de signalisation (SDP + ICE),
  (c) servir les fichiers du site.
- Le **tchat texte** utilise un `RTCDataChannel` (P2P) avec repli sur le serveur.
- **STUN** : `stun.l.google.com` (inclus) suffit pour la plupart des box.
  Pour les réseaux restrictifs (NAT symétrique), ajoutez un serveur **TURN**
  (voir section 4).

### Schéma de signalisation
| Étape | A (caller) | serveur | B (callee) |
|-------|------------|---------|------------|
| 1 | `ready` → | | ← `ready` |
| 2 | ← `matched:caller` | appairage | → `matched:callee` |
| 3 | `offer` (SDP) → | transfert | `offer` → |
| 4 | | | `answer` (SDP) → |
| 5 | ← `answer` | transfert | |
| 6 | `ice` ⇄ | transfert | ⇄ `ice` |
| 7 | — vidéo/audio P2P établie — |

---

## 4. Déploiement (vrai site public)

Pour que deux inconnus **sur des réseaux différents** se rencontrent :

1. **Hébergez le serveur** sur une machine publique (VPS, Railway, Fly.io,
   Render, etc.) en exposant le port 8000.
   ```
   python server.py   # écoute sur 0.0.0.0:8000
   ```
2. **HTTPS obligatoire** (caméra) : placez-le derrière un reverse proxy
   (Caddy, Nginx, Cloudflare) avec un certificat (Let's Encrypt).
   Exemple Caddyfile :
   ```
   kirkchat.exemple.com {
       reverse_proxy localhost:8000
   }
   ```
   Pensez à autoriser le **WebSocket** (Caddy le gère automatiquement).
3. **Serveur TURN** (recommandé) — pour les NAT strictes, ajoutez coturn et
   configurez `ICE.iceServers` dans `app.js` :
   ```js
   { urls: "turn:turn.exemple.com:3478", username:"…", credential:"…" }
   ```

---

## 5. Règles & sécurité

- Réservé aux **majeurs (18+)**. L'utilisateur coche la confirmation au départ.
- Contenus interdits : mineurs, nudité, violence, harcèlement, données
  personnelles, contenu illégal.
- Le serveur ne stocke **aucune** vidéo/audio (P2P) ni de logs de tchat.
- Pour une mise en production sérieuse, ajoutez une modération, un système de
  signalement, un rate-limiting et une rotation des identifiants.

---

## 6. Structure du projet

```
kirkchat/
├── server.py          # serveur HTTP + WebSocket (signaling + appairage)
├── start.bat          # lanceur Windows (python server.py + navigateur)
├── README.md
└── static/
    ├── index.html     # page unique (lobby + chat)
    ├── style.css      # thème Amérique (drapeau, rouge/blanc/bleu)
    ├── app.js         # client WebRTC + tchat + contrôles
    └── favicon.svg    # étoile themed
```

Bon chat ! 🇺🇸
