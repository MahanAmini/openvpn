#!/usr/bin/env python3
"""GassLight user portal — a small, separate web page for VPN users.

Each user gets a private link  http://SERVER:8089/u/<token>  (copied from the panel).
The page shows that user's own details (status, time left, usage, limits) and a locked
download button for the .ovpn file: the user must type the download password the admin
sent them.

This file holds no data and no secrets of its own. It only asks the panel (127.0.0.1)
through a small internal API, using a shared secret file that the panel creates.
It can run as an unprivileged user (see the systemd unit in the instructions).

Env (all optional):
  PORTAL_PORT        port to listen on                       (default 8089)
  PORTAL_HOST        address to bind                         (default 0.0.0.0)
  PANEL_URL          where the panel listens                 (default http://127.0.0.1:8088)
  PORTAL_SECRET_FILE shared secret file                      (default /opt/ovpn-panel/portal.secret)
  TRUST_PROXY=1      take the visitor IP from X-Forwarded-For (only behind your own reverse proxy)
"""
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
    if os.environ.get("CREDENTIALS_DIRECTORY"):          # systemd LoadCredential=
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
    code, headers, body = panel(f"/internal/portal/{token}/ovpn", {"password": request.form.get("password", "")[:128]})
    if code == 200:
        return Response(body, 200, mimetype="application/x-openvpn-profile",
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
</style></head><body>
<div class="wrap">
 <div class="top"><div class="brand"><div class="logo" id="logo"></div><span>Gass<em>Light</em></span></div>
  <button class="btn ghost sm" id="lang" type="button"></button></div>
 <div id="info"><div class="center" data-i="loading">Loading...</div></div>
 <div class="card" id="dl" hidden>
  <h2><span data-ic="lock"></span><span data-i="cfg_title">Config file</span></h2>
  <p class="muted" data-i="cfg_desc">Type the password you received from your provider to unlock the download.</p>
  <label data-i="cfg_pw">Download password</label>
  <div class="row"><div class="grow" style="position:relative"><input id="pw" type="password" autocomplete="off" autocapitalize="none" spellcheck="false"></div>
   <button class="btn ghost" id="eye" type="button" aria-label="show"></button></div>
  <button class="btn block" id="go" type="button"><span data-ic="download"></span><span data-i="cfg_btn">Unlock &amp; download</span></button>
  <div class="msg" id="msg" role="alert"></div>
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
 inf:'<path d="M18.178 8c5.096 0 5.096 8 0 8-5.095 0-7.133-8-12.356-8-5.096 0-5.096 8 0 8 5.223 0 7.26-8 12.356-8z"/>'
};
function ic(n,s){return '<svg class="ic" width="'+(s||16)+'" height="'+(s||16)+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+P[n]+'</svg>'}
var T={
en:{other:'فارسی',loading:'Loading...',cfg_title:'Config file',cfg_desc:'Type the password you received from your provider to unlock the download.',cfg_pw:'Download password',cfg_btn:'Unlock & download',
 how:'How to connect',how1:'Install the OpenVPN app (OpenVPN Connect for Android, iOS, Windows and macOS).',how2:'Unlock and download your config file (.ovpn) above.',how3:'Open the file with the app, or import it from the app.',how4:'Press connect. Keep the file private: it contains your personal key.',
 online:'Online',offline:'Offline',server:'Server',protocol:'Protocol',port:'Port',local_ip:'Local IP',speed:'Max speed',nospeed:'No speed limit',conn:'Connection',last:'Last connection',since:'since',never:'Never connected',
 created:'Created',expires:'Expires',noexp:'No expiry',timeleft:'Time left',time:'Time',usage:'Usage',down:'Download',up:'Upload',total:'Total',today:'Today',dlim:'Daily limit',tlim:'Total limit',nolim:'No limit',
 blk_daily:'Your access is paused: the daily data limit was reached. It resets automatically at the start of the next day (server time).',blk_total:'Your access is paused: the total data limit was reached. Contact your provider.',
 left_dh:'{d}d {h}h left',left_hm:'{h}h {m}m left',left_m:'{m} min left',ago_now:'just now',ago_min:'{n} min ago',ago_h:'{n} h ago',ago_d:'{n} d ago',updated:'Updated',
 e_bad:'Wrong password. {left} attempts left.',e_bad0:'Wrong password. Too many attempts, try again later.',e_lock:'Too many attempts. Try again in {m} min.',e_nf:'This link is no longer valid.',e_down:'Service unavailable, please try again later.',e_empty:'Enter the password first.',ok:'Download started.',nf:'This link is not valid',nf2:'Ask your provider for a new link.'},
fa:{other:'English',loading:'در حال بارگذاری...',cfg_title:'فایل کانفیگ',cfg_desc:'رمزی که از ارائه‌دهنده گرفتی رو وارد کن تا دانلود باز بشه.',cfg_pw:'رمز دانلود',cfg_btn:'باز کردن قفل و دانلود',
 how:'روش اتصال',how1:'برنامه‌ی OpenVPN رو نصب کن (OpenVPN Connect برای اندروید، iOS، ویندوز و مک).',how2:'فایل کانفیگ (.ovpn) رو از بالا باز و دانلود کن.',how3:'فایل رو با برنامه باز کن یا داخل برنامه Import کن.',how4:'روی اتصال بزن. فایل رو پیش خودت نگه دار: کلید شخصی تو داخلشه.',
 online:'آنلاین',offline:'آفلاین',server:'سرور',protocol:'پروتکل',port:'پورت',local_ip:'آی‌پی لوکال',speed:'حداکثر سرعت',nospeed:'بدون محدودیت سرعت',conn:'اتصال',last:'آخرین اتصال',since:'از',never:'هیچ‌وقت وصل نشده',
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
var D=null;
function render(){
 if(!D)return;var d=D,tot=d.up+d.down,h='';
 h+='<div class="card hero"><div class="av">'+esc(d.name.charAt(0).toUpperCase())+'</div><div class="grow"><h1>'+esc(d.name)+'</h1><div class="tags"><span class="tag">'+esc(d.proto.toUpperCase())+'</span><span class="tag">'+t('port')+' '+d.port+'</span>'+
  '<span class="tag">'+ic('zap',11)+' '+(d.speed?d.speed+' Mbps':t('nospeed'))+'</span></div></div><span class="pill '+(d.online?'on':'')+'">'+(d.online?t('online'):t('offline'))+'</span></div>';
 if(d.blocked)h+='<div class="warnbox">'+ic('alert',18)+'<span>'+t(d.blocked==='daily'?'blk_daily':'blk_total')+'</span></div>';
 var last=d.online?('<b>'+t('online')+'</b> · '+t('since')+' '+dt(d.since||d.last)):(d.last?dt(d.last)+'<span class="sub">'+ago(d.last,d.now)+'</span>':t('never'));
 h+='<div class="card"><h2>'+ic('wifi',18)+t('conn')+'</h2><div class="kv">'+
  row('server',t('server'),'<span class="mono">'+esc(d.server||'—')+'</span>')+row('wifi',t('protocol'),esc(d.proto.toUpperCase())+' · '+t('port')+' <span class="mono">'+d.port+'</span>')+
  row('server',t('local_ip'),d.ip?'<span class="mono">'+esc(d.ip)+'</span>':'—')+row('zap',t('speed'),d.speed?d.speed+' Mbps':t('nospeed'))+row('clock',t('last'),last)+'</div></div>';
 var tm='<div class="kv">'+row('cal',t('created'),ds(d.created));
 if(d.expires){var l=d.expires-d.now,tl=Math.max(d.expires-d.created,1),pc=Math.max(0,Math.min(100,l/tl*100)),cls=l<86400?'bad':(l<259200?'warn':'ok');
  tm+=row('cal',t('expires'),dt(d.expires))+row('clock',t('timeleft'),left(Math.max(l,0)))+'</div><div class="bar"><i class="'+cls+'" style="width:'+pc.toFixed(1)+'%"></i></div>'}
 else tm+=row('inf',t('expires'),t('noexp'))+'</div>';
 h+='<div class="card"><h2>'+ic('clock',18)+t('time')+'</h2>'+tm+'</div>';
 h+='<div class="card"><h2>'+ic('bars',18)+t('usage')+'</h2><div class="tgrid"><div class="tg">'+t('down')+'<b class="dn">'+fb(d.down)+'</b></div><div class="tg">'+t('up')+'<b class="upc">'+fb(d.up)+'</b></div>'+
  '<div class="tg">'+t('total')+'<b>'+fb(tot)+'</b></div><div class="tg">'+t('today')+'<b>'+fb(d.day_up+d.day_down)+'</b></div></div>'+lim(t('dlim'),d.day_up+d.day_down,d.limit_daily)+lim(t('tlim'),tot,d.limit_total)+'</div>';
 $('info').innerHTML=h;$('dl').hidden=false;
 var n=new Date(d.now*1000);$('upd').textContent=t('updated')+' '+p2(n.getHours())+':'+p2(n.getMinutes())+':'+p2(n.getSeconds());
}
function statics(){
 document.documentElement.lang=lang;document.documentElement.dir=lang==='fa'?'rtl':'ltr';
 document.querySelectorAll('[data-i]').forEach(function(e){e.textContent=t(e.dataset.i)});
 document.querySelectorAll('[data-ic]').forEach(function(e){e.innerHTML=ic(e.dataset.ic,e.dataset.ic==='download'?18:18)});
 $('lang').textContent=T[lang].other;$('logo').innerHTML=ic('shield',22);
 var shown=$('pw').type==='text';$('eye').innerHTML=ic(shown?'eyeoff':'eye',18);
}
function dead(){$('dl').hidden=true;$('info').innerHTML='<div class="card center"><h2 style="justify-content:center">'+ic('alert',20)+t('nf')+'</h2><p>'+t('nf2')+'</p></div>';clearInterval(timer)}
var timer=null,failed=false;
function load(){
 fetch('/u/'+TOKEN+'/data',{cache:'no-store'}).then(function(r){
  if(r.status===404){dead();return null}return r.ok?r.json():null}).then(function(d){if(d){D=d;render()}}).catch(function(){});
}
$('lang').addEventListener('click',function(){lang=lang==='en'?'fa':'en';try{localStorage.setItem('gl_lang',lang)}catch(e){}statics();render();});
$('eye').addEventListener('click',function(){var p=$('pw');p.type=p.type==='password'?'text':'password';statics();});
function say(cls,txt){var m=$('msg');m.className='msg '+cls;m.textContent=txt}
function go(){
 var pw=$('pw').value;if(!pw){say('err',t('e_empty'));return}
 var b=$('go');b.disabled=true;say('','');$('msg').className='msg';
 var fd=new URLSearchParams();fd.set('password',pw);
 fetch('/u/'+TOKEN+'/download',{method:'POST',body:fd,cache:'no-store'}).then(function(r){
  if(r.ok){var name=((r.headers.get('Content-Disposition')||'').match(/filename="?([^";]+)"?/)||[])[1]||'client.ovpn';
   return r.blob().then(function(bl){var a=document.createElement('a');a.href=URL.createObjectURL(bl);a.download=name;document.body.appendChild(a);a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},1500);$('pw').value='';say('ok',t('ok'))})}
  return r.json().catch(function(){return{}}).then(function(j){
   if(r.status===403)say('err',j.left>0?t('e_bad',{left:j.left}):t('e_bad0'));
   else if(r.status===429)say('err',t('e_lock',{m:Math.max(1,Math.ceil((j.wait||60)/60))}));
   else if(r.status===404)say('err',t('e_nf'));else say('err',t('e_down'))})
 }).catch(function(){say('err',t('e_down'))}).then(function(){b.disabled=false});
}
$('go').addEventListener('click',go);$('pw').addEventListener('keydown',function(e){if(e.key==='Enter')go()});
statics();load();timer=setInterval(load,3000);
})();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
