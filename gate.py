"""Admin gate for the published report.

GitHub Pages is a public static site, so hiding tabs with CSS/JS alone would not hide anything —
anyone could read the HTML source. Instead the full report is encrypted (AES-256-GCM, key from the
admin password via PBKDF2-SHA256) and only the Suggestion tab is left in plain text. The browser
decrypts the full report with WebCrypto after the admin types the password.

Password comes from the KENO_ADMIN_PASSWORD environment variable (a GitHub Actions secret).
"""
import base64
import hashlib
import json
import os

ITERATIONS = 250_000


def password():
    return os.environ.get("KENO_ADMIN_PASSWORD", "").strip()


def mode():
    """'full'   — no gate (running on your own PC without a password set)
       'locked' — public Suggestion page + encrypted full report
       'public' — on GitHub with no password secret: Suggestion page only, admin tabs not published"""
    if password():
        return "locked"
    return "public" if os.environ.get("GITHUB_ACTIONS") else "full"


def encrypt(text, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, ITERATIONS, 32)
    ct = AESGCM(key).encrypt(iv, text.encode("utf-8"), None)  # ciphertext || 16-byte tag, as WebCrypto expects
    b = lambda x: base64.b64encode(x).decode("ascii")
    return {"s": b(salt), "i": b(iv), "c": b(ct), "n": ITERATIONS}


LOGOUT_BTN = ("<button class='admbtn' onclick=\"try{localStorage.removeItem('keno-admin')}catch(e){}"
              "location.href=location.pathname\">Đăng xuất</button>")

GATE_CSS = r"""
.admbtn{background:var(--panel);color:var(--muted);border:1px solid var(--line);border-radius:14px;padding:3px 12px;font:inherit;font-size:12px;cursor:pointer;white-space:nowrap}
.admbtn:hover{color:var(--green);border-color:var(--green)}
#login{display:none;gap:6px;align-items:center;flex-wrap:wrap;margin:6px auto 8px;font-size:12.5px}#login.on{display:flex}
#login input{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:5px 8px;font:inherit;min-width:180px}
#login .err{color:var(--red)}
html.auto main{visibility:hidden}
"""

GATE_JS = r"""<script>
(function(){
 var H=document.documentElement,box=document.getElementById('login'),pw=document.getElementById('pw'),msg=document.getElementById('lmsg'),
     el=document.getElementById('locked'),P=el?JSON.parse(el.textContent):null;
 function st(v){try{if(v===undefined)return localStorage.getItem('keno-admin');if(v===null)localStorage.removeItem('keno-admin');else localStorage.setItem('keno-admin',v)}catch(e){return null}}
 function b64(s){var r=atob(s),u=new Uint8Array(r.length);for(var i=0;i<r.length;i++)u[i]=r.charCodeAt(i);return u}
 async function open(p){
  var raw=await crypto.subtle.importKey('raw',new TextEncoder().encode(p),'PBKDF2',false,['deriveKey']);
  var key=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(P.s),iterations:P.n,hash:'SHA-256'},raw,{name:'AES-GCM',length:256},false,['decrypt']);
  var buf=await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(P.i)},key,b64(P.c));
  return new TextDecoder().decode(buf)}
 async function go(p,quiet){
  try{var html=await open(p)}catch(e){H.classList.remove('auto');if(quiet){st(null)}else{msg.textContent='Sai mật khẩu';msg.className='err'}return}
  if(document.getElementById('remember').checked||quiet)st(p);
  document.open();document.write(html);document.close()}
 var btn=document.getElementById('adm');
 if(!P){if(btn)btn.style.display='none';return}
 btn.onclick=function(){box.classList.toggle('on');if(box.classList.contains('on'))pw.focus()};
 box.onsubmit=function(ev){ev.preventDefault();msg.textContent='Đang mở…';msg.className='muted';go(pw.value,false)};
 var saved=st();if(saved)go(saved,true);
})();
</script>"""


def public_page(full_page, sug_section, extra_body, updated, locked_payload):
    """Suggestion-only page. full_page supplies the <head>/CSS so both pages look the same."""
    head = full_page.split("</style></head><body>", 1)[0]
    auto = " class=\"auto\"" if locked_payload else ""
    head = head.replace('<html lang="vi">', f'<html lang="vi"{auto}>', 1)
    # auto: hide the page briefly while a remembered admin password is tried
    auto_js = ("<script>try{if(!localStorage.getItem('keno-admin'))document.documentElement.classList.remove('auto')}"
               "catch(e){document.documentElement.classList.remove('auto')}</script>") if locked_payload else ""
    payload = (f"<script type=\"application/json\" id=\"locked\">{json.dumps(locked_payload)}</script>"
               if locked_payload else "")
    return f"""{head}{GATE_CSS}</style>{auto_js}</head><body>
<header class="top">
<div class="brand"><h1>KENOAUTO · HOT SPOT</h1><span style="display:flex;gap:8px;align-items:center"><span id="dev" class="dev"></span><button id="adm" class="admbtn">🔒 Admin</button></span></div>
<form id="login" autocomplete="on"><input type="text" name="username" value="admin" autocomplete="username" hidden>
<input id="pw" type="password" autocomplete="current-password" placeholder="Mật khẩu admin">
<button class="admbtn" type="submit">Mở</button><label class="muted"><input id="remember" type="checkbox" checked> Nhớ trên máy này</label><span id="lmsg"></span></form>
<div class="muted" style="font-size:12px;margin-bottom:6px">Cập nhật {updated}</div>
<nav class="tabs" id="tabs"><button data-t="sug">🎯 Gợi ý</button></nav></header>
<main>
<section class="pane" id="t-sug">{sug_section}</section>
{extra_body}
{payload}
{GATE_JS}
</main></body></html>"""
