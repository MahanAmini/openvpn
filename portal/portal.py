#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, abort, jsonify, request

PORT = int(os.environ.get("PORTAL_PORT", "8089"))
HOST = os.environ.get("PORTAL_HOST", "0.0.0.0")
PANEL_URL = os.environ.get("PANEL_URL", "http://127.0.0.1:8088").rstrip("/")
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def read_secret():
    cands = []
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        cands.append(os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "portal.secret"))
    cands.append(os.environ.get("PORTAL_SECRET_FILE", "/opt/ovpn-panel/portal.secret"))
    for p in cands:
        try:
            with open(p) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            continue
    return ""


SECRET = read_secret()
if not SECRET:
    sys.exit("portal secret not found: start the (updated) panel once, then check PORTAL_SECRET_FILE / LoadCredential")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4096
HITS = {}


def client_ip():
    if TRUST_PROXY:
        xf = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if xf:
            return xf[:64]
    return request.remote_addr or "-"


def limited(key, limit, window=60):
    now = time.time()
    q = [t for t in HITS.get(key, []) if now - t < window]
    q.append(now)
    HITS[key] = q
    if len(HITS) > 20000:
        HITS.clear()
    return len(q) > limit


def panel(path, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(PANEL_URL + path, data=data)
    req.add_header("X-Portal-Secret", SECRET)
    req.add_header("X-Client-IP", client_ip())
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except Exception:
        return 503, {}, b'{"error":"panel_down"}'


@app.after_request
def secure(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    return resp


@app.errorhandler(404)
def not_found(_e):
    return Response("Not found", 404, mimetype="text/plain")


@app.errorhandler(413)
def too_big(_e):
    return jsonify(error="bad_request"), 413


@app.route("/u/<token>")
def user_page(token):
    if not TOKEN_RE.match(token):
        abort(404)
    return Response(PAGE, mimetype="text/html")


@app.route("/u/<token>/data")
def user_data(token):
    if not TOKEN_RE.match(token):
        abort(404)
    if limited(("d", client_ip()), 150):
        return jsonify(error="slow_down"), 429
    code, _h, body = panel(f"/internal/portal/{token}")
    return Response(body, 200 if code == 200 else (404 if code == 404 else 503), mimetype="application/json")


@app.route("/u/<token>/download", methods=["POST"])
def user_download(token):
    if not TOKEN_RE.match(token):
        abort(404)
    if limited(("p", client_ip()), 20):
        return jsonify(error="slow_down"), 429
    code, headers, body = panel(f"/internal/portal/{token}/ovpn", {"password": request.form.get("password", "")[:128], "p": request.form.get("p", "")[:8]})
    if code == 200:
        return Response(body, 200, mimetype=headers.get("Content-Type") or "application/x-openvpn-profile",
                        headers={"Content-Disposition": headers.get("Content-Disposition", 'attachment; filename="client.ovpn"')})
    try:
        out = json.loads(body)
    except ValueError:
        out, code = {"error": "panel_down"}, 503
    return jsonify(out), (code if code in (403, 404, 429) else 503)


PAGE = r"""<!doctype html>
<html lang="en" dir="ltr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><title>GassLight</title>
<style>
:root{--bg:#0a0e1f;--card:rgba(255,255,255,.055);--bd:rgba(255,255,255,.1);--tx:#eaeefb;--mut:#8f9bbd;
--p1:#6d7cff;--p2:#a45cff;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;--inp:rgba(255,255,255,.06);--dn:#22d3ee;--upc:#a45cff}
@media (prefers-color-scheme:light){:root{--bg:#eef1fb;--card:rgba(255,255,255,.8);--bd:rgba(20,30,80,.1);--tx:#1a2140;--mut:#64709a;--inp:rgba(20,30,80,.05)}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;color:var(--tx);background:var(--bg);font-family:Inter,Vazirmatn,"Segoe UI",Tahoma,sans-serif;line-height:1.6;
background-image:radial-gradient(600px 400px at 85% -5%,rgba(109,124,255,.35),transparent),radial-gradient(500px 400px at 5% 10%,rgba(164,92,255,.25),transparent);background-attachment:fixed}
.wrap{max-width:760px;margin:0 auto;padding:18px 14px 48px}
.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:800}
.brand em{font-style:normal;background:linear-gradient(135deg,var(--p1),var(--p2));-webkit-background-clip:text;background-clip:text;color:transparent}
.logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 8px 24px rgba(109,124,255,.4)}
.ic{flex:none;vertical-align:-3px}.mono{font-family:ui-monospace,Menlo,Consolas,monospace;direction:ltr;unicode-bidi:isolate}
.card{background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:18px;margin-bottom:14px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.card h2{margin:0 0 12px;font-size:15px;display:flex;align-items:center;gap:8px}
.hero{display:flex;align-items:center;gap:14px}.grow{flex:1;min-width:0}
.hero h1{margin:0;font-size:20px;word-break:break-all}
.av{width:54px;height:54px;border-radius:16px;display:grid;place-items:center;font-weight:700;font-size:22px;background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;flex:none}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.tag{display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:2px 9px;border-radius:99px;background:var(--inp);color:var(--mut);border:1px solid var(--bd)}
.pill{font-size:12px;padding:4px 12px;border-radius:99px;border:1px solid var(--bd);color:var(--mut);white-space:nowrap}
.pill.on{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.1)}
.warnbox{display:flex;gap:10px;align-items:flex-start;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.35);color:var(--warn);padding:12px 16px;border-radius:12px;margin-bottom:14px;font-size:14px}
.kv{display:flex;flex-direction:column;gap:9px;font-size:14px}
.kv div{display:flex;justify-content:space-between;align-items:center;gap:12px}
.kv .k{color:var(--mut);display:flex;align-items:center;gap:7px;white-space:nowrap}.kv .v{text-align:end;min-width:0;word-break:break-all}
.sub{display:block;font-size:11px;color:var(--mut)}
.bar{height:8px;border-radius:99px;background:var(--inp);overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--p1),var(--p2))}
.bar i.ok{background:linear-gradient(90deg,var(--ok),#22d3ee)}.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
.tgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.tg{padding:9px 10px;border-radius:12px;background:var(--inp);font-size:12px;color:var(--mut);min-width:0}
.tg b{display:block;font-size:15px;color:var(--tx);white-space:nowrap;font-variant-numeric:tabular-nums}
.dn{color:var(--dn)!important}.upc{color:var(--upc)!important}
.lim{font-size:12px;color:var(--mut);margin-top:10px}.lim .l{display:flex;justify-content:space-between;gap:8px}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
.row{display:flex;gap:8px}
input{width:100%;padding:12px 13px;border-radius:12px;border:1px solid var(--bd);background:var(--inp);color:var(--tx);font:inherit;font-size:16px;outline:none}
input:focus{border-color:var(--p1);box-shadow:0 0 0 3px rgba(109,124,255,.25)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:12px 18px;border:0;border-radius:12px;color:#fff;cursor:pointer;font:inherit;font-size:15px;background:linear-gradient(135deg,var(--p1),var(--p2));transition:.2s;white-space:nowrap}
.btn:hover{filter:brightness(1.1)}.btn:disabled{opacity:.6;cursor:wait}
.btn.ghost{background:var(--inp);color:var(--tx);border:1px solid var(--bd)}.btn.sm{padding:8px 14px;font-size:13px}
.btn.block{width:100%;margin-top:10px}
.msg{margin-top:10px;padding:10px 14px;border-radius:12px;font-size:14px;display:none}
.msg.err{display:block;background:rgba(251,113,133,.12);color:var(--bad);border:1px solid rgba(251,113,133,.35)}
.msg.ok{display:block;background:rgba(52,211,153,.12);color:var(--ok);border:1px solid rgba(52,211,153,.35)}
.muted{color:var(--mut);font-size:13px;margin:0 0 12px}
details{margin-top:12px;font-size:14px}summary{cursor:pointer;color:var(--mut)}
details ol{margin:10px 0 0;padding-inline-start:22px;color:var(--tx)}details li{margin-bottom:4px}
.foot{text-align:center;color:var(--mut);font-size:12px;margin-top:6px}
.center{text-align:center;padding:40px 10px;color:var(--mut)}
@media(max-width:520px){.tgrid{grid-template-columns:repeat(2,1fr)}.hero{flex-wrap:wrap}}
.row .btn{flex:none;padding:12px 16px}
.sup .muted{margin-bottom:14px}
.tgbtn{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:15px 18px;
border-radius:14px;color:#fff;text-decoration:none;font-weight:700;font-size:15px;
background:linear-gradient(110deg,#2aabee,#229ed9,#6d7cff,#a45cff,#2aabee);background-size:300% 100%;
animation:tgflow 6s linear infinite,tgpulse 2.4s ease-out infinite;transition:transform .2s}
.tgbtn::after{content:"";position:absolute;top:0;left:-60%;width:40%;height:100%;
background:linear-gradient(100deg,transparent,rgba(255,255,255,.45),transparent);transform:skewX(-20deg);animation:tgshine 3.2s ease-in-out infinite}
.tgbtn:hover{transform:translateY(-2px) scale(1.01)}.tgbtn:active{transform:scale(.98)}
.tgi{display:inline-flex;animation:tgfly 2.2s ease-in-out infinite}
@keyframes tgflow{to{background-position:300% 0}}
@keyframes tgshine{0%{left:-60%}55%,100%{left:130%}}
@keyframes tgpulse{0%{box-shadow:0 10px 28px rgba(42,171,238,.4),0 0 0 0 rgba(42,171,238,.55)}70%,100%{box-shadow:0 10px 28px rgba(42,171,238,.4),0 0 0 16px rgba(42,171,238,0)}}
@keyframes tgfly{0%,100%{transform:translate(0,0) rotate(0)}50%{transform:translate(3px,-3px) rotate(8deg)}}
@media (prefers-reduced-motion:reduce){.tgbtn,.tgbtn::after,.tgi{animation:none}}
/* ---- config picker ---- */
.fold{display:grid;grid-template-rows:1fr;transition:grid-template-rows .65s cubic-bezier(.7,0,.2,1),opacity .4s,filter .5s,transform .65s,visibility 0s}
.fold>.in{overflow:hidden;min-height:0;padding:4px;margin:-4px}
.fold.shut{grid-template-rows:0fr;opacity:0;filter:blur(8px);transform:translateY(-10px) scale(.97);pointer-events:none;visibility:hidden;transition-delay:0s,0s,0s,0s,.65s}
.fold.pk{grid-template-rows:0fr;opacity:0;visibility:hidden;pointer-events:none;transition-delay:0s,0s,0s,0s,.7s}
.fold.pk>.in{padding:14px;margin:-14px}
.fold.pk.open{grid-template-rows:1fr;opacity:1;visibility:visible;pointer-events:auto;transition-delay:0s}
.btn.okf{background:linear-gradient(135deg,var(--ok),#22d3ee);animation:okpulse .5s ease-out}
.pkh{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.pkh h3{margin:0;font-size:15px}.pkh p{margin:2px 0 0;font-size:12.5px;color:var(--mut)}
.pkic{position:relative;width:46px;height:46px;border-radius:14px;display:grid;place-items:center;color:#fff;flex:none;background:linear-gradient(135deg,var(--ok),var(--dn))}
.pkic::before{content:"";position:absolute;inset:0;border-radius:inherit;border:2px solid var(--ok);animation:pkring 2s ease-out infinite}
.pk.open .pkic{animation:pkunl .9s cubic-bezier(.2,1.5,.4,1) .15s backwards}
.cfgs{display:flex;flex-wrap:wrap;gap:12px;perspective:700px}
.cfgb{--cb:#121833;position:relative;flex:1 1 170px;min-width:0;display:flex;align-items:center;gap:12px;padding:15px 16px;border:0;border-radius:16px;
color:var(--tx);font:inherit;text-align:start;cursor:pointer;isolation:isolate;overflow:hidden;-webkit-tap-highlight-color:transparent;transition:transform .3s cubic-bezier(.2,1.4,.4,1),box-shadow .3s,color .3s}
@media (prefers-color-scheme:light){.cfgb{--cb:#f4f6ff}}
.pk.open .cfgb{animation:cfgin .8s cubic-bezier(.2,1.3,.4,1) backwards;animation-delay:calc(.35s + var(--i)*.13s)}
.cfgb>i{position:absolute;pointer-events:none}
.cfgb .bd{inset:0;z-index:-3;border-radius:inherit;background:linear-gradient(110deg,var(--p1),var(--p2),var(--dn),var(--p2),var(--p1));background-size:300% 100%;animation:tgflow 4s linear infinite}
.cfgb .bg{inset:1.5px;z-index:-2;border-radius:14.5px;background:var(--cb);transition:opacity .35s}
.cfgb .fl{inset:0;z-index:-1;background:linear-gradient(90deg,var(--p1),var(--p2));transform:scaleX(0);transform-origin:0 50%;transition:transform .5s ease,opacity .5s}
html[dir=rtl] .cfgb .fl{transform-origin:100% 50%}
.cfgb::after{content:"";position:absolute;top:0;left:-60%;width:40%;height:100%;pointer-events:none;background:linear-gradient(100deg,transparent,rgba(255,255,255,.3),transparent);
transform:skewX(-20deg);animation:tgshine 3.4s ease-in-out infinite;animation-delay:calc(var(--i)*.7s + 1.6s)}
.cfgb:hover{transform:translateY(-3px) scale(1.02);box-shadow:0 14px 32px rgba(109,124,255,.38);color:#fff}
.cfgb:hover .bg{opacity:.12}
.cfgb:active{transform:scale(.97)}
.cfgb:focus-visible{outline:2px solid var(--dn);outline-offset:3px}
.cfgb .ico{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;flex:none;background:var(--inp);color:var(--dn);transition:.35s cubic-bezier(.2,1.4,.4,1)}
.cfgb:hover .ico{background:rgba(255,255,255,.2);color:#fff;transform:rotate(-8deg) scale(1.1)}
.cfgb .tx{flex:1;min-width:0}
.cfgb .tx b{display:block;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;unicode-bidi:plaintext}
.cfgb small{display:block;font-size:11.5px;color:var(--mut);transition:color .3s}
.cfgb:hover small{color:rgba(255,255,255,.85)}
.cfgb .st{display:grid;place-items:center;flex:none;width:22px;height:22px}
.cfgb:hover .st svg{animation:pkdl .8s ease-in-out infinite}
.spn{width:18px;height:18px;border-radius:50%;border:2.5px solid rgba(255,255,255,.35);border-top-color:#fff;animation:pkspin .7s linear infinite}
.cfgb.busy{pointer-events:none;color:#fff}
.cfgb.busy .fl{animation:pkfill 1.1s cubic-bezier(.3,.6,.3,1) forwards}
.cfgb.busy small{color:rgba(255,255,255,.85)}
.cfgb.done{color:#fff}
.cfgb.done .fl{transform:scaleX(1);background:linear-gradient(90deg,var(--ok),#22d3ee)}
.cfgb.done .st svg{animation:pkpop .55s cubic-bezier(.2,1.8,.4,1)}
.cfgb.done small{color:rgba(255,255,255,.9)}
.pk.open .cfgb.fail{animation:pkshake .45s}
.cfgb.fail .bd{background:var(--bad);animation:none}
.pt{position:absolute;width:6px;height:6px;margin:-3px 0 0 -3px;border-radius:50%;background:hsl(var(--h) 90% 65%);pointer-events:none;z-index:5;animation:pkpt .75s cubic-bezier(.1,.8,.3,1) forwards}
@keyframes cfgin{from{opacity:0;transform:translateY(26px) scale(.7) rotateX(35deg);filter:blur(6px)}}
@keyframes pkunl{from{transform:scale(.4) rotate(-35deg);opacity:0}}
@keyframes pkring{0%{transform:scale(1);opacity:.8}100%{transform:scale(1.7);opacity:0}}
@keyframes pkspin{to{transform:rotate(360deg)}}
@keyframes pkfill{to{transform:scaleX(.92)}}
@keyframes pkpop{from{transform:scale(0) rotate(-40deg)}}
@keyframes pkshake{20%,60%{transform:translateX(-6px)}40%,80%{transform:translateX(6px)}}
@keyframes pkdl{0%,100%{transform:translateY(0)}50%{transform:translateY(3px)}}
@keyframes pkpt{from{transform:translate(0,0) scale(1)}to{transform:translate(var(--dx),var(--dy)) scale(0);opacity:0}}
@keyframes okpulse{0%{transform:scale(1)}50%{transform:scale(1.04);box-shadow:0 0 0 10px rgba(52,211,153,.25)}100%{transform:scale(1)}}
@media (prefers-reduced-motion:reduce){.cfgb,.cfgb>i,.cfgb::after,.pkic,.pkic::before,.pk.open .cfgb,.pk.open .pkic,.cfgb .st svg{animation:none!important}.fold{transition:none}}
</style></head><body>
<div class="wrap">
 <div class="top"><div class="brand"><div class="logo" id="logo"></div><span>Gass<em>Light</em></span></div>
  <button class="btn ghost sm" id="lang" type="button"></button></div>
 <div id="info"><div class="center" data-i="loading">Loading...</div></div>
 <div id="sup"></div>
 <div id="info2"></div>
 <div class="card" id="dl" hidden>
  <h2><span data-ic="lock"></span><span data-i="cfg_title">Config file</span></h2>
  <div class="fold" id="pwform"><div class="in">
  <p class="muted" data-i="cfg_desc">Type the password you received from your provider to unlock the download.</p>
  <label data-i="cfg_pw">Download password</label>
  <div class="row"><div class="grow" style="position:relative"><input id="pw" type="password" autocomplete="off" autocapitalize="none" spellcheck="false"></div>
   <button class="btn ghost" id="eye" type="button" aria-label="show"></button></div>
  <button class="btn block" id="go" type="button"><span data-ic="unlock"></span><span data-i="cfg_btn">Unlock</span></button>
  <div class="msg" id="msg" role="alert"></div>
  </div></div>
  <div class="fold pk" id="picker"><div class="in">
   <div class="pkh"><div class="pkic"><span data-ic="unlock"></span></div><div><h3 data-i="pk_title"></h3><p data-i="pk_desc"></p></div></div>
   <div class="cfgs" id="cfgs"></div>
   <div class="msg" id="pmsg" role="alert"></div>
   <button class="btn ghost sm" id="pkback" type="button" style="margin-top:14px"><span data-ic="lock"></span><span data-i="pk_back"></span></button>
  </div></div>
  <details><summary data-i="how">How to connect</summary>
   <ol><li data-i="how1"></li><li data-i="how2"></li><li data-i="how3"></li><li data-i="how4"></li></ol></details>
 </div>
 <div class="foot" id="upd"></div>
</div>
<script>
(function(){
var $=function(i){return document.getElementById(i)};
var TOKEN=(location.pathname.match(/^\/u\/([A-Za-z0-9_-]+)/)||[])[1]||'';
var P={
 shield:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
 lock:'<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
 download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
 eye:'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
 eyeoff:'<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>',
 server:'<rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
 clock:'<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
 cal:'<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
 zap:'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
 alert:'<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
 bars:'<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
 wifi:'<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
 send:'<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
 inf:'<path d="M18.178 8c5.096 0 5.096 8 0 8-5.095 0-7.133-8-12.356-8-5.096 0-5.096 8 0 8 5.223 0 7.26-8 12.356-8z"/>'
};
function ic(n,s){return '<svg class="ic" width="'+(s||16)+'" height="'+(s||16)+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+P[n]+'</svg>'}
var T={
en:{other:'فارسی',sup_title:'Contact support',sup_desc:'Press the button to reach support on Telegram for connection problems, renewals or purchases.',sup_btn:'Chat with support on Telegram',loading:'Loading...',cfg_title:'Config file',cfg_desc:'Type the password you received from your provider to unlock the download.',cfg_pw:'Download password',cfg_btn:'Unlock & download',
 how:'How to connect',how1:'Install the OpenVPN app (OpenVPN Connect for Android, iOS, Windows and macOS).',how2:'Unlock and download your config file (.ovpn) above.',how3:'Open the file with the app, or import it from the app.',how4:'Press connect. Keep the file private: it contains your personal key.',
 online:'Online',offline:'Offline',server:'Server',protocol:'Protocol',port:'Port',local_ip:'Local IP',conn:'Connection',last:'Last connection',since:'since',never:'Never connected',
 created:'Created',expires:'Expires',noexp:'No expiry',timeleft:'Time left',time:'Time',usage:'Usage',down:'Download',up:'Upload',total:'Total',today:'Today',dlim:'Daily limit',tlim:'Total limit',nolim:'No limit',
 blk_daily:'Your access is paused: the daily data limit was reached. It resets automatically at the start of the next day (server time).',blk_total:'Your access is paused: the total data limit was reached. Contact your provider.',
 left_dh:'{d}d {h}h left',left_hm:'{h}h {m}m left',left_m:'{m} min left',ago_now:'just now',ago_min:'{n} min ago',ago_h:'{n} h ago',ago_d:'{n} d ago',updated:'Updated',
 e_bad:'Wrong password. {left} attempts left.',e_bad0:'Wrong password. Too many attempts, try again later.',e_lock:'Too many attempts. Try again in {m} min.',e_nf:'This link is no longer valid.',e_down:'Service unavailable, please try again later.',e_empty:'Enter the password first.',ok:'Download started.',nf:'This link is not valid',nf2:'Ask your provider for a new link.'},
fa:{other:'English',sup_title:'ارتباط با پشتیبانی',sup_desc:'با زدن این دکمه می‌تونی از طریق تلگرام با پشتیبانی جهت مشکلات اتصال، تمدید یا خرید ارتباط برقرار کنی.',sup_btn:'ارتباط با پشتیبانی در تلگرام',loading:'در حال بارگذاری...',cfg_title:'فایل کانفیگ',cfg_desc:'رمزی که از ارائه‌دهنده گرفتی رو وارد کن تا دانلود باز بشه.',cfg_pw:'رمز دانلود',cfg_btn:'باز کردن قفل و دانلود',
 how:'روش اتصال',how1:'برنامه‌ی OpenVPN رو نصب کن (OpenVPN Connect برای اندروید، iOS، ویندوز و مک).',how2:'فایل کانفیگ (.ovpn) رو از بالا باز و دانلود کن.',how3:'فایل رو با برنامه باز کن یا داخل برنامه Import کن.',how4:'روی اتصال بزن. فایل رو پیش خودت نگه دار: کلید شخصی تو داخلشه.',
 online:'آنلاین',offline:'آفلاین',server:'سرور',protocol:'پروتکل',port:'پورت',local_ip:'آی‌پی لوکال',conn:'اتصال',last:'آخرین اتصال',since:'از',never:'هیچ‌وقت وصل نشده',
 created:'تاریخ ساخت',expires:'انقضا',noexp:'بدون انقضا',timeleft:'زمان باقی‌مانده',time:'زمان',usage:'مصرف',down:'دانلود',up:'آپلود',total:'مجموع',today:'امروز',dlim:'سقف روزانه',tlim:'سقف کلی',nolim:'بدون سقف',
 blk_daily:'دسترسی تو متوقف شده: به سقف حجم روزانه رسیدی. با شروع روز بعد (به وقت سرور) خودکار باز می‌شه.',blk_total:'دسترسی تو متوقف شده: به سقف حجم کلی رسیدی. با ارائه‌دهنده در تماس باش.',
 left_dh:'{d} روز و {h} ساعت مانده',left_hm:'{h} ساعت و {m} دقیقه مانده',left_m:'{m} دقیقه مانده',ago_now:'همین الان',ago_min:'{n} دقیقه پیش',ago_h:'{n} ساعت پیش',ago_d:'{n} روز پیش',updated:'به‌روزرسانی',
 e_bad:'رمز اشتباهه. {left} بار دیگه می‌تونی امتحان کنی.',e_bad0:'رمز اشتباهه. تعداد تلاش زیاد بود، بعداً دوباره امتحان کن.',e_lock:'تعداد تلاش زیاد بود. {m} دقیقه‌ی دیگه دوباره امتحان کن.',e_nf:'این لینک دیگه معتبر نیست.',e_down:'سرویس در دسترس نیست، بعداً دوباره امتحان کن.',e_empty:'اول رمز رو وارد کن.',ok:'دانلود شروع شد.',nf:'این لینک معتبر نیست',nf2:'یه لینک جدید از ارائه‌دهنده بگیر.'}};
var lang='en';try{lang=localStorage.getItem('gl_lang')||'en'}catch(e){}if(!T[lang])lang='en';
function t(k,o){var s=T[lang][k]!==undefined?T[lang][k]:(T.en[k]||k);if(o)for(var x in o)s=s.replace('{'+x+'}',o[x]);return s}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function fb(n){var u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?2:0)+'\u00a0'+u[i]}
function p2(n){return('0'+n).slice(-2)}
function ds(ts){var d=new Date(ts*1000);return d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate())}
function dt(ts){var d=new Date(ts*1000);return ds(ts)+' '+p2(d.getHours())+':'+p2(d.getMinutes())}
function ago(ts,now){var s=Math.max(0,now-ts);if(s<60)return t('ago_now');if(s<3600)return t('ago_min',{n:Math.floor(s/60)});if(s<86400)return t('ago_h',{n:Math.floor(s/3600)});return t('ago_d',{n:Math.floor(s/86400)})}
function left(sec){var d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60);if(d)return t('left_dh',{d:d,h:h});if(h)return t('left_hm',{h:h,m:m});return t('left_m',{m:Math.max(m,1)})}
function row(i,k,v){return '<div><span class="k">'+ic(i,15)+k+'</span><span class="v">'+v+'</span></div>'}
function lim(label,used,gb){
 if(!gb)return '<div class="lim"><div class="l"><span>'+label+'</span><span>'+t('nolim')+'</span></div></div>';
 var pct=Math.min(100,used/(gb*1073741824)*100),cls=pct>=100?'bad':(pct>=80?'warn':'ok');
 return '<div class="lim"><div class="l"><span>'+label+'</span><span>'+fb(used)+' / '+gb+' GB</span></div><div class="bar"><i class="'+cls+'" style="width:'+pct.toFixed(1)+'%"></i></div></div>'}
var D=null,supKey='';
function supCard(d){
 var id=d.support_tg||'';if(!/^[A-Za-z0-9_]{5,32}$/.test(id))id='';
 var key=lang+'|'+id;if(key===supKey)return;supKey=key;
 $('sup').innerHTML=id?'<div class="card sup"><h2>'+ic('send',18)+t('sup_title')+'</h2><p class="muted">'+t('sup_desc')+'</p>'+
  '<a class="tgbtn" target="_blank" rel="noopener noreferrer" href="https://t.me/'+id+'"><span class="tgi">'+ic('send',20)+'</span><span>'+t('sup_btn')+'</span></a></div>':'';
}
function render(){
 if(!D)return;var d=D,tot=d.up+d.down,h='';
 h+='<div class="card hero"><div class="av">'+esc(d.name.charAt(0).toUpperCase())+'</div><div class="grow"><h1>'+esc(d.name)+'</h1><div class="tags"><span class="tag">'+esc(d.proto.toUpperCase())+'</span><span class="tag">'+t('port')+' '+d.port+'</span>'+
  '</div></div><span class="pill '+(d.online?'on':'')+'">'+(d.online?t('online'):t('offline'))+'</span></div>';
 if(d.blocked)h+='<div class="warnbox">'+ic('alert',18)+'<span>'+t(d.blocked==='daily'?'blk_daily':'blk_total')+'</span></div>';
 $('info').innerHTML=h;supCard(d);h='';
 var last=d.online?('<b>'+t('online')+'</b> · '+t('since')+' '+dt(d.since||d.last)):(d.last?dt(d.last)+'<span class="sub">'+ago(d.last,d.now)+'</span>':t('never'));
 h+='<div class="card"><h2>'+ic('wifi',18)+t('conn')+'</h2><div class="kv">'+
  row('server',t('server'),'<span class="mono">'+esc(d.server||'—')+'</span>')+row('wifi',t('protocol'),esc(d.proto.toUpperCase())+' · '+t('port')+' <span class="mono">'+d.port+'</span>')+
  row('server',t('local_ip'),d.ip?'<span class="mono">'+esc(d.ip)+'</span>':'—')+row('clock',t('last'),last)+'</div></div>';
 var tm='<div class="kv">'+row('cal',t('created'),ds(d.created));
 if(d.expires){var l=d.expires-d.now,tl=Math.max(d.expires-d.created,1),pc=Math.max(0,Math.min(100,l/tl*100)),cls=l<86400?'bad':(l<259200?'warn':'ok');
  tm+=row('cal',t('expires'),dt(d.expires))+row('clock',t('timeleft'),left(Math.max(l,0)))+'</div><div class="bar"><i class="'+cls+'" style="width:'+pc.toFixed(1)+'%"></i></div>'}
 else tm+=row('inf',t('expires'),t('noexp'))+'</div>';
 h+='<div class="card"><h2>'+ic('clock',18)+t('time')+'</h2>'+tm+'</div>';
 h+='<div class="card"><h2>'+ic('bars',18)+t('usage')+'</h2><div class="tgrid"><div class="tg">'+t('down')+'<b class="dn">'+fb(d.down)+'</b></div><div class="tg">'+t('up')+'<b class="upc">'+fb(d.up)+'</b></div>'+
  '<div class="tg">'+t('total')+'<b>'+fb(tot)+'</b></div><div class="tg">'+t('today')+'<b>'+fb(d.day_up+d.day_down)+'</b></div></div>'+lim(t('dlim'),d.day_up+d.day_down,d.limit_daily)+lim(t('tlim'),tot,d.limit_total)+'</div>';
 $('info2').innerHTML=h;$('dl').hidden=false;
 var n=new Date(d.now*1000);$('upd').textContent=t('updated')+' '+p2(n.getHours())+':'+p2(n.getMinutes())+':'+p2(n.getSeconds());
}
function statics(){
 document.documentElement.lang=lang;document.documentElement.dir=lang==='fa'?'rtl':'ltr';
 document.querySelectorAll('[data-i]').forEach(function(e){e.textContent=t(e.dataset.i)});
 document.querySelectorAll('[data-ic]').forEach(function(e){e.innerHTML=ic(e.dataset.ic,e.dataset.ic==='download'?18:18)});
 $('lang').textContent=T[lang].other;$('logo').innerHTML=ic('shield',22);
 var shown=$('pw').type==='text';$('eye').innerHTML=ic(shown?'eyeoff':'eye',18);
}
function dead(){$('dl').hidden=true;$('sup').innerHTML='';$('info2').innerHTML='';supKey='';$('info').innerHTML='<div class="card center"><h2 style="justify-content:center">'+ic('alert',20)+t('nf')+'</h2><p>'+t('nf2')+'</p></div>';clearInterval(timer)}
var timer=null,failed=false;
function load(){
 fetch('/u/'+TOKEN+'/data',{cache:'no-store'}).then(function(r){
  if(r.status===404){dead();return null}return r.ok?r.json():null}).then(function(d){if(d){D=d;render()}}).catch(function(){});
}
$('lang').addEventListener('click',function(){lang=lang==='en'?'fa':'en';try{localStorage.setItem('gl_lang',lang)}catch(e){}statics();render();});
$('eye').addEventListener('click',function(){var p=$('pw');p.type=p.type==='password'?'text':'password';statics();});
function say(cls,txt){var m=$('msg');m.className='msg '+cls;m.textContent=txt}
function dl1(pw,p){
 var fd=new URLSearchParams();fd.set('password',pw);if(p)fd.set('p',p);
 return fetch('/u/'+TOKEN+'/download',{method:'POST',body:fd,cache:'no-store'}).then(function(r){
  if(r.ok){var name=((r.headers.get('Content-Disposition')||'').match(/filename="?([^";]+)"?/)||[])[1]||'client.ovpn';
   if(p){var sfx='_'+p,dot=name.lastIndexOf('.'),base=dot>0?name.slice(0,dot):name,ext=dot>0?name.slice(dot):'.ovpn';
    if(base.toLowerCase().slice(-sfx.length)!==sfx)name=base+sfx+ext}
   return r.blob().then(function(bl){var a=document.createElement('a');a.href=URL.createObjectURL(bl);a.download=name;document.body.appendChild(a);a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},1500);return null})}
  return r.json().catch(function(){return{}}).then(function(j){return{s:r.status,j:j}})})
}
T.en.cfg_btn='Unlock';T.fa.cfg_btn='باز کردن قفل';
T.en.pk_title='Pick a config';T.en.pk_desc='Tap one to download only that file.';T.en.pk_back='Lock again';
T.fa.pk_title='یکی از کانفیگ‌ها رو انتخاب کن';T.fa.pk_desc='روی هرکدوم بزنی فقط همون فایل دانلود می‌شه.';T.fa.pk_back='قفل کردن دوباره';
P.check='<polyline points="20 6 9 17 4 12"/>';
P.unlock='<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>';
P.file='<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/>';
var PW='',CACHE={},CL=[];
function pm(cls,txt){var m=$('pmsg');m.className='msg '+cls;m.textContent=txt}
function errOf(e){var j=e.j||{};
 if(e.s===403)return j.left>0?t('e_bad',{left:j.left}):t('e_bad0');
 if(e.s===429)return t('e_lock',{m:Math.max(1,Math.ceil((j.wait||60)/60))});
 if(e.s===404)return t('e_nf');return t('e_down')}
function getCfg(pw,p){
 var fd=new URLSearchParams();fd.set('password',pw);if(p)fd.set('p',p);
 return fetch('/u/'+TOKEN+'/download',{method:'POST',body:fd,cache:'no-store'}).then(function(r){
  if(r.ok){var name=((r.headers.get('Content-Disposition')||'').match(/filename="?([^";]+)"?/)||[])[1]||'client.ovpn';
   if(p){var sfx='_'+p,dot=name.lastIndexOf('.'),base=dot>0?name.slice(0,dot):name,ext=dot>0?name.slice(dot):'.ovpn';
    if(base.toLowerCase().slice(-sfx.length)!==sfx)name=base+sfx+ext}
   return r.blob().then(function(bl){return{ok:true,bl:bl,name:name}})}
  return r.json().catch(function(){return{}}).then(function(j){return{ok:false,s:r.status,j:j}})})
}
function save(c){var a=document.createElement('a');a.href=URL.createObjectURL(c.bl);a.download=c.name;document.body.appendChild(a);a.click();
 setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},1500)}
function cfgList(){var d=D||{},nm=String(d.name||'config'),pr=(d.proto==='both')?['udp','tcp']:[''];
 return pr.map(function(p){return{p:p,label:nm+(p?'_'+p:''),proto:String(p||d.proto||'ovpn').toUpperCase()}})}
function subs(){document.querySelectorAll('.cfgb').forEach(function(b){var c=CL[+b.dataset.k];
 if(c)b.querySelector('small').textContent=c.proto+' · '+t('port')+' '+((D&&D.port)||'')})}
function burst(b,e){var r=b.getBoundingClientRect(),x=e&&e.clientX?e.clientX-r.left:r.width/2,y=e&&e.clientY?e.clientY-r.top:r.height/2;
 for(var i=0;i<14;i++){var s=document.createElement('em'),a=Math.random()*6.283,d=30+Math.random()*55;
  s.className='pt';s.style.cssText='left:'+x+'px;top:'+y+'px;--dx:'+Math.round(Math.cos(a)*d)+'px;--dy:'+Math.round(Math.sin(a)*d)+'px;--h:'+Math.floor(180+Math.random()*130);
  b.appendChild(s);setTimeout((function(el){return function(){el.remove()}})(s),800)}}
function openPicker(){
 CL=cfgList();var h='';
 CL.forEach(function(c,i){h+='<button type="button" class="cfgb" data-k="'+i+'" style="--i:'+i+'"><i class="bd"></i><i class="bg"></i><i class="fl"></i>'+
  '<span class="ico">'+ic('file',22)+'</span><span class="tx"><b>'+esc(c.label)+'</b><small></small></span><span class="st">'+ic('download',18)+'</span></button>'});
 $('cfgs').innerHTML=h;subs();pm('','');
 $('go').classList.add('okf');
 setTimeout(function(){$('pwform').classList.add('shut');setTimeout(function(){$('picker').classList.add('open')},350)},450);
}
function lockAgain(){
 PW='';CACHE={};$('picker').classList.remove('open');
 setTimeout(function(){$('go').classList.remove('okf');$('pwform').classList.remove('shut');$('cfgs').innerHTML='';say('','');$('msg').className='msg'},500);
}
function pick(b,e){
 var c=CL[+b.dataset.k];if(!c)return;
 var st=b.querySelector('.st');burst(b,e);pm('','');b.classList.remove('done','fail');b.classList.add('busy');st.innerHTML='<span class="spn"></span>';
 var job=CACHE[c.p]?Promise.resolve(CACHE[c.p]):getCfg(PW,c.p);
 Promise.all([job,new Promise(function(r){setTimeout(r,800)})]).then(function(a){var r=a[0];
  b.classList.remove('busy');
  if(!r.ok){st.innerHTML=ic('download',18);b.classList.add('fail');pm('err',errOf(r));setTimeout(function(){b.classList.remove('fail')},1200);return}
  CACHE[c.p]=r;save(r);b.classList.add('done');st.innerHTML=ic('check',20);pm('ok',t('ok'));
  setTimeout(function(){b.classList.remove('done');st.innerHTML=ic('download',18)},2800);
 }).catch(function(){b.classList.remove('busy');st.innerHTML=ic('download',18);pm('err',t('e_down'))});
}
function go(){
 var pw=$('pw').value;if(!pw){say('err',t('e_empty'));return}
 var b=$('go');b.disabled=true;$('msg').className='msg';
 var first=cfgList()[0];
 getCfg(pw,first.p).then(function(c){
  if(!c.ok){say('err',errOf(c));return}
  PW=pw;CACHE={};CACHE[first.p]=c;$('pw').value='';openPicker();
 }).catch(function(){say('err',t('e_down'))}).then(function(){b.disabled=false});
}
$('cfgs').addEventListener('click',function(e){var b=e.target.closest?e.target.closest('.cfgb'):null;if(b&&!b.classList.contains('busy'))pick(b,e)});
$('pkback').addEventListener('click',lockAgain);
$('lang').addEventListener('click',subs);
$('go').addEventListener('click',go);$('pw').addEventListener('keydown',function(e){if(e.key==='Enter')go()});
statics();load();timer=setInterval(load,3000);
})();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
